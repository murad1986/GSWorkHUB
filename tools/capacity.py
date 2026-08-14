#!/usr/bin/env python3
"""Ёмкость дня из данных о восстановлении.

Граница с health-store: **только чтение, только агрегат.** В склад не попадает ни
одного медицинского показателя — наружу отдаётся «ресурс высокий / средний /
низкий» и дата данных. Диагнозы, давление и анализы остаются там, где им место.

Предохранитель важнее самой оценки: если данные старше порога, ёмкость **не
определяется**. Устаревшие данные, поданные как свежие, — первый пункт в
docs/known-failure-modes.md, и здесь он был бы особенно дорог: система советовала
бы нагрузку по состоянию четырёхмесячной давности.

Мера — эвристика, не медицина: вариабельность ритма и пульс покоя сравниваются с
личной базой самого человека, а не с нормой из справочника. Ниже своей базы —
меньше ресурса. Так делают носимые трекеры; это ориентир, а не диагноз.

**Где лежат данные.** Хранилище показателей — отдельный проект, а не часть
склада. Его корень задаётся переменной окружения `HEALTH_STORE_ROOT`. Переменной
нет или каталога нет — ёмкость честно отвечает «неизвестна»; это рабочее
состояние, а не поломка, и день тогда планируется без поправки на восстановление.
Весь доступ изолирован в этом файле: сменить источник — правка одного места.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import statistics
import subprocess
from dataclasses import dataclass
from pathlib import Path

HEALTH_ROOT = Path(os.environ.get(
    "HEALTH_STORE_ROOT", str(Path.home() / "Projects/health-store")))
HEALTH = HEALTH_ROOT / "data_inputs/apple_health/parquet"
HEALTH_EXPORT = HEALTH_ROOT / "data_inputs/apple_health/health_auto_export"
# Автосинхронизация Health Auto Export: приложение кладёт дневные файлы в iCloud.
# Имя папки задаёт сам человек в приложении, поэтому ищем по образцу файлов, а не
# по названию каталога — иначе переименование автоматизации тихо сломает ёмкость.
ICLOUD_SYNC = Path(os.environ.get(
    "HEALTH_EXPORT_DIR",
    str(Path.home() / "Library/Mobile Documents"
        / "iCloud~com~ifunography~HealthExport/Documents")))
HEALTH_PY = HEALTH_ROOT / "venv/bin/python"
STALE_AFTER_DAYS = 14
BASELINE_DAYS = 60
RECENT_DAYS = 7
MIN_POINTS = 5          # меньше — сравнивать с личной базой не с чем


@dataclass
class Capacity:
    level: str          # full | half | low | unknown
    reason: str
    as_of: dt.date | None = None
    stale_days: int | None = None

    def render(self) -> str:
        if self.level == "unknown":
            return f"ёмкость не определена: {self.reason}"
        tail = f" (данные от {self.as_of:%d.%m})" if self.as_of else ""
        return f"ресурс {self.level}: {self.reason}{tail}"


READER = (
    "import sys, pyarrow.parquet as pq;"
    "t = pq.read_table(sys.argv[1], columns=['timestamp','value']);"
    "s = t.column('timestamp').to_pylist(); v = t.column('value').to_pylist();"
    "print('\\n'.join(f'{str(a)[:10]},{b}' for a, b in zip(s, v) if a and b is not None))"
)


class SourceUnavailable(RuntimeError):
    """Данные не прочитаны. Отличается от «данных нет»: причина называется явно."""


def _series(name: str, source: Path, runner: Path = HEALTH_PY) -> list[tuple[dt.date, float]]:
    """Пара «дата — значение». Читается окружением самого health-store, а не нашим.

    Так мы не тащим его зависимости к себе и остаёмся на шаг от «спросить health-store
    командой» вместо «залезть в его файлы своими библиотеками».
    """
    path = source / f"{name}.parquet"
    if not path.exists():
        return []
    if not runner.exists():
        raise SourceUnavailable(f"нет окружения health-store ({runner})")
    result = subprocess.run([str(runner), "-c", READER, str(path)],
                            capture_output=True, text=True, check=False)
    if result.returncode:
        raise SourceUnavailable((result.stderr.strip().splitlines() or ["ошибка чтения"])[-1])
    out: list[tuple[dt.date, float]] = []
    for line in result.stdout.splitlines():
        day, _, value = line.partition(",")
        try:
            out.append((dt.date.fromisoformat(day), float(value)))
        except ValueError:
            continue
    return out


# Выгрузка Health Auto Export: json, и в нём есть сон, которого не было в parquet
EXPORT_KEYS = {
    "hrv": ("heart_rate_variability", "qty"),
    "rhr": ("resting_heart_rate", "qty"),
    "sleep": ("sleep_analysis", "totalSleep"),
}


def _export_files(*folders: Path) -> list[Path]:
    """Все выгрузки Health Auto Export: разовые и дневные из автосинхронизации."""
    files: list[Path] = []
    for folder in folders:
        if not folder.exists():
            continue
        files += sorted(folder.glob("HealthAutoExport-*.json"))
        files += sorted(folder.glob("*/HealthAutoExport-*.json"))
    return files


def _from_export(*folders: Path) -> dict[str, list[tuple[dt.date, float]]] | None:
    """Серии из всех выгрузок сразу: история из разовой, свежее из дневных.

    Дневной файл автосинхронизации содержит один день, разовая выгрузка — месяцы.
    Склеиваем: по дате берём последнее встреченное значение, файлы идут по порядку,
    поэтому свежие данные вытесняют старые за тот же день.
    """
    files = _export_files(*folders)
    if not files:
        return None
    merged: dict[str, dict[dt.date, float]] = {key: {} for key in EXPORT_KEYS}
    broken: list[str] = []
    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            # Файл пропускаем, но факт запоминаем: тихий пропуск повреждённого
            # входа — отдельный известный отказ, праздновать его нельзя
            broken.append(path.name)
            continue
        try:
            raw_metrics = payload["data"]["metrics"]
            metrics = {m["name"]: m for m in raw_metrics if isinstance(m, dict)}
        except (KeyError, TypeError):
            # Валидный JSON неправильной формы — тоже непрочитанный файл
            broken.append(path.name)
            continue
        for key, (name, field) in EXPORT_KEYS.items():
            for point in (metrics.get(name) or {}).get("data", []):
                value = point.get(field)
                stamp = str(point.get("date") or "")[:10]
                if value is None or not stamp:
                    continue
                try:
                    merged[key][dt.date.fromisoformat(stamp)] = float(value)
                except ValueError:
                    continue
    result = {key: sorted(values.items()) for key, values in merged.items()}
    result["__broken__"] = broken  # type: ignore[assignment]
    return result


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def assess(hrv: list[tuple[dt.date, float]], rhr: list[tuple[dt.date, float]],
           today: dt.date, stale_after: int = STALE_AFTER_DAYS,
           sleep: list[tuple[dt.date, float]] | None = None) -> Capacity:
    sleep = sleep or []
    if not hrv and not rhr and not sleep:
        return Capacity("unknown", "источник данных о восстановлении недоступен")

    last = max([day for day, _ in hrv + rhr + sleep])
    stale = (today - last).days
    if stale > stale_after:
        return Capacity("unknown",
                        f"выгрузка устарела на {stale} дн. (последняя {last:%d.%m.%Y}) — "
                        "по такому состоянию советовать нагрузку нельзя",
                        as_of=last, stale_days=stale)

    def window(series: list[tuple[dt.date, float]], days: int) -> list[float]:
        edge = last - dt.timedelta(days=days)
        return [v for day, v in series if day > edge]

    hrv_recent, hrv_base = _median(window(hrv, RECENT_DAYS)), _median(window(hrv, BASELINE_DAYS))
    rhr_recent, rhr_base = _median(window(rhr, RECENT_DAYS)), _median(window(rhr, BASELINE_DAYS))

    if hrv_recent is None or hrv_base is None:
        return Capacity("unknown", "мало точек для сравнения с личной базой", as_of=last)
    if len({day for day, _ in hrv if day > last - dt.timedelta(days=BASELINE_DAYS)}) < MIN_POINTS:
        return Capacity("unknown",
                        f"в личной базе меньше {MIN_POINTS} измерений — "
                        "одна-две точки дают ложную «норму»", as_of=last)

    sleep_recent, sleep_base = _median(window(sleep, RECENT_DAYS)), _median(window(sleep, BASELINE_DAYS))

    worse: list[str] = []
    if hrv_recent < hrv_base * 0.9:
        worse.append(f"вариабельность {hrv_recent:.0f} против обычных {hrv_base:.0f} мс")
    if rhr_recent is not None and rhr_base is not None and rhr_recent > rhr_base + 3:
        worse.append(f"пульс покоя {rhr_recent:.0f} против обычных {rhr_base:.0f}")
    if sleep_recent is not None and sleep_base is not None and sleep_recent < sleep_base - 0.75:
        worse.append(f"сон {sleep_recent:.1f} ч против обычных {sleep_base:.1f}")

    if len(worse) >= 2:
        return Capacity("low", "хуже обычного: " + "; ".join(worse), as_of=last, stale_days=stale)
    if worse:
        return Capacity("half", "хуже обычного: " + worse[0], as_of=last, stale_days=stale)
    detail = f"сон {sleep_recent:.1f} ч, вариабельность {hrv_recent:.0f} мс" if sleep_recent else \
             f"вариабельность {hrv_recent:.0f} мс"
    return Capacity("full", f"на своём обычном уровне ({detail})", as_of=last, stale_days=stale)


def measure(source: Path = HEALTH, today: dt.date | None = None,
            stale_after: int = STALE_AFTER_DAYS) -> Capacity:
    today = today or dt.date.today()
    # Свежая выгрузка приоритетнее: в ней есть сон, которого нет в parquet
    export = _from_export(HEALTH_EXPORT, ICLOUD_SYNC)
    broken: list[str] = []
    if export:
        broken = export.pop("__broken__", [])  # type: ignore[assignment]
        if any(export.values()):
            result = assess(export["hrv"], export["rhr"], today, stale_after, export["sleep"])
            if broken:
                result.reason += f"; не прочитано файлов: {len(broken)}"
            return result
    if broken:
        # Все выгрузки битые — молчаливого отката к parquet быть не должно
        return Capacity("unknown", f"все выгрузки не читаются ({len(broken)}): "
                                   + ", ".join(broken[:3]))
    try:
        hrv = _series("HRV_SDNN", source)
        rhr = _series("RestingHeartRate", source)
    except SourceUnavailable as exc:
        # «Нечем прочитать» не то же самое, что «данных нет» — путать нельзя
        return Capacity("unknown", f"источник не прочитан: {exc}")
    return assess(hrv, rhr, today, stale_after)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ёмкость дня по данным о восстановлении")
    parser.add_argument("--source", type=Path, default=HEALTH)
    parser.add_argument("--today", type=dt.date.fromisoformat, default=dt.date.today())
    args = parser.parse_args(argv)
    print(measure(args.source, args.today).render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
