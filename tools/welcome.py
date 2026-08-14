#!/usr/bin/env python3
"""Приветствие при входе: состояние склада одним экраном.

Зачем. Каждая сессия начиналась одинаково — выяснить ветку, незакоммиченное,
свежесть экрана внимания. Делалось это по-разному, а иногда не делалось вовсе:
разговор продолжался по памяти о прошлом разговоре. Память расходится со
складом на первой же правке, и расхождение обнаруживается не сразу, а через
несколько действий, сделанных по неверному состоянию.

Инструмент только читает. Ничего не пересобирает и не пишет в журнал: вход
обязан быть дешёвым и не менять того, что показывает. Если экран внимания
устарел или не читается — он говорит об этом вслух, а не показывает ноль
сигналов. Тихого третьего варианта нет, как и в store.py.

Первым после заголовка идёт неразобранное сырьё — решение человека 30 июля:
сессия открывается незакрытым, с предложением разобрать, и только затем всё
остальное. Это не ошибка и не упрёк: вход сохраняется сразу, разбор — по
готовности, а приветствие лишь напоминает, что он ещё не случился.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import subprocess
from pathlib import Path

import yaml
from store import DATE, parse

SIGNALS_SHOWN = 3
DIRTY_SHOWN = 4
INTAKE_SHOWN = 3
MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня",
          "июля", "августа", "сентября", "октября", "ноября", "декабря")
NUMBERED = re.compile(r"^\s*\d+\.\s+(.*\S)\s*$")


def local_now() -> dt.datetime:
    """Местное время без зоны: склад везде пишет время без зоны (`generated_at`
    в attention.md), и сравнивать его с временем с зоной нельзя."""
    return dt.datetime.now(dt.timezone.utc).astimezone().replace(tzinfo=None)


def ru_date(moment: dt.date | dt.datetime, *, with_time: bool = False) -> str:
    text = f"{moment.day} {MONTHS[moment.month - 1]}"
    if with_time and isinstance(moment, dt.datetime):
        text += f" {moment:%H:%M}"
    return text


def run_git(root: Path, *args: str) -> str | None:
    """Вывод git или None, если git недоступен. Отказ — не «всё чисто»."""
    try:
        done = subprocess.run(("git", "-C", str(root), *args), check=False,
                              capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout.rstrip("\n")


def git_lines(root: Path) -> list[str]:
    branch = run_git(root, "branch", "--show-current")
    if branch is None:
        return ["Склад вне git — сверить состояние нечем"]
    status = run_git(root, "status", "--short") or ""
    dirty = [line[3:] for line in status.splitlines() if line.strip()]
    head = f"Ветка {branch or 'без имени'}"
    if dirty:
        shown = ", ".join(dirty[:DIRTY_SHOWN])
        if len(dirty) > DIRTY_SHOWN:
            shown += f" и ещё {len(dirty) - DIRTY_SHOWN}"
        head += f" · незакоммичено: {shown}"
    else:
        head += " · незакоммиченного нет"
    lines = [head]
    last = run_git(root, "log", "-1", "--format=%ad|%s", "--date=format:%d.%m")
    if last and "|" in last:
        when, subject = last.split("|", 1)
        lines.append(f"Последнее движение {when} — «{subject}»")
    return lines


def newest_work(root: Path) -> dt.datetime | None:
    """Когда склад последний раз менялся. Архив не в счёт — он не попадает в экран."""
    newest: float | None = None
    base = root / "work"
    if not base.exists():
        return None
    for path in base.rglob("*.md"):
        if str(path.relative_to(root)).startswith("work/archive/"):
            continue
        try:
            stamp = path.stat().st_mtime
        except OSError:
            continue
        if newest is None or stamp > newest:
            newest = stamp
    if newest is None:
        return None
    return dt.datetime.fromtimestamp(newest, tz=dt.timezone.utc).astimezone().replace(tzinfo=None)


def as_datetime(value: object) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, dt.date):
        return dt.datetime.combine(value, dt.time())
    if isinstance(value, str):
        try:
            return dt.datetime.fromisoformat(value.strip()).replace(tzinfo=None)
        except ValueError:
            return None
    return None


def intake_zone(root: Path) -> str:
    """Зона приёма из config/attention.yml — то же место, куда смотрит экран."""
    try:
        config = yaml.safe_load((root / "config/attention.yml").read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        config = None
    intake = (config or {}).get("intake") or {}
    return str(intake.get("zone") or "raw/inbox").rstrip("/")


def unprocessed(root: Path) -> tuple[list[Path], int]:
    """Неразобранные файлы и число позиций work/, которые не удалось прочитать."""
    base = root / intake_zone(root)
    if not base.exists():
        return [], 0
    incoming = [path for path in sorted(base.glob("*.md")) if path.name != "index.md"]
    if not incoming:
        return [], 0
    corpus: list[str] = []
    broken = 0
    for path in (root / "work").rglob("*.md"):
        if str(path.relative_to(root)).startswith("work/archive/"):
            continue
        try:
            corpus.append(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            broken += 1
    text = "\n".join(corpus)
    return [path for path in incoming if path.name not in text], broken


def unprocessed_lines(root: Path) -> list[str]:
    """Неразобранное сырьё — первым, с предложением разобрать.

    Признак разобранного — ссылка из work/: полнота полей в шапке ничего не
    говорит о том, дошло ли извлечение до позиций. Проверено 30 июля: три записи
    type: source с безупречными шапками не были видны ни одному сигналу, потому
    что прежняя мерка смотрела на поля, а не на след разбора.
    """
    pending, broken = unprocessed(root)
    if not pending:
        return []
    oldest = next((match.group(0) for path in pending
                   if (match := DATE.search(path.name))), None)
    tail = ""
    if oldest:
        day = dt.date.fromisoformat(oldest)
        tail = f", старейшая от {ru_date(day)}"
    lines = [f"Неразобрано: {len(pending)}{tail} — разобрать?"]
    lines.extend(f"  · {path.relative_to(root)}" for path in pending[:INTAKE_SHOWN])
    if len(pending) > INTAKE_SHOWN:
        lines.append(f"  · ещё {len(pending) - INTAKE_SHOWN}")
    if broken:
        lines.append(f"  {broken} файлов work не прочитаны — список может быть неполон")
    return lines


def attention_lines(root: Path) -> list[str]:
    path = root / "wiki/attention.md"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ["Экрана внимания нет — собрать: make attention"]
    except OSError as exc:
        return [(f"Экран внимания не прочитан ({type(exc).__name__}) — "
                 "нельзя считать, что сигналов нет")]
    data, why = parse(text)
    if data is None:
        return [f"Экран внимания не разобран: {why} — пересобрать: make attention"]

    signals = [match.group(1) for line in text.splitlines()
               if (match := NUMBERED.match(line))]
    generated = as_datetime(data.get("generated_at"))
    stamp = (f"собран {ru_date(generated, with_time=True)}" if generated
             else "время сборки неизвестно")
    lines = [f"Требует внимания: {len(signals)} · {stamp}"]
    lines.extend(f"  · {signal}" for signal in signals[:SIGNALS_SHOWN])
    if len(signals) > SIGNALS_SHOWN:
        lines.append(f"  · ещё {len(signals) - SIGNALS_SHOWN} — wiki/attention.md")

    changed = newest_work(root)
    if generated and changed and changed > generated:
        lines.append(f"  Склад менялся после сборки ({ru_date(changed, with_time=True)}) "
                     "— пересобрать: make attention")
    return lines


def gate_env(root: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(root / "tools")
    return env


def gate_lines(root: Path) -> list[str]:
    """Одна строка на гейт: итог, а не простыня. Красное — с указанием, что делать."""
    results: list[str] = []
    gates = (("lint", "lint.py", "ЛИНТ:"), ("verify", "verify.py", "VERIFY:"))
    for name, script, marker in gates:
        try:
            done = subprocess.run(("python3", str(root / "tools" / script)), check=False,
                                  capture_output=True, text=True, timeout=180,
                                  cwd=str(root), env=gate_env(root))
        except (OSError, subprocess.SubprocessError) as exc:
            results.append(f"{name} не запустился ({type(exc).__name__}) — проверить: make gates")
            continue
        # Итоговая строка гейта тянет за собой разбивку в скобках — в приветствии
        # она не нужна: важно «сошлось или нет», подробности даёт сам гейт.
        summary = next((line.split(" (")[0] for line in reversed(done.stdout.splitlines())
                        if line.startswith(marker)), "")
        if done.returncode == 0:
            results.append(summary or f"{name}: прошёл")
        else:
            results.append(f"{summary or name + ': не прошёл'} — разбирать: make gates")
    return ["Гейты: " + " · ".join(results)] if results else []


def render(root: Path, now: dt.datetime, *, gates: bool = True) -> list[str]:
    lines = [f"Склад GSWorkHUB · {ru_date(now, with_time=True)}"]
    # Незакрытое — прежде всего остального: так решил человек 30 июля.
    lines.extend(unprocessed_lines(root))
    lines.extend(git_lines(root))
    if gates:
        lines.extend(gate_lines(root))
    lines.extend(attention_lines(root))
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description="состояние склада одним экраном")
    parser.add_argument("--root", default=".", help="корень склада")
    parser.add_argument("--no-gates", action="store_true",
                        help="без прогона гейтов — быстрее, но состояние проверок неизвестно")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    for line in render(root, local_now(), gates=not args.no_gates):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
