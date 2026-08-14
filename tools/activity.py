#!/usr/bin/env python3
"""Короткий журнал наблюдаемого поведения системы.

В журнале четыре разных факта и их нельзя подменять друг другом:

- вид вычислен;
- регулярный заход состоялся (касание);
- строка действительно представлена человеку;
- человек явно отреагировал.

Касание отвечает только за то, что система вышла на связь; читал ли человек и
ответил ли — другие события, и по касанию они не выводятся.

`raw/log.md` — зарезервированный дописываемый журнал. Этот модуль — единственная
точка чтения и записи: он добавляет строки в конец, но никогда не переписывает
уже произошедшее.
"""

from __future__ import annotations

import argparse
import datetime as dt
from dataclasses import dataclass
from pathlib import Path

TOUCHES = {"morning": "утро", "evening": "вечер"}

# Словарь событий журнала. Это данные, а не украшение: журнал стал общей шиной
# всех механик — самоанализа, советов, разрешения ссылок, касаний, — и событие,
# которого нет в словаре, молча выпало бы из одних читателей и осталось в
# других. Новое событие сначала объявляется здесь, потом пишется.
#   поля: сколько частей после имени события обязательны (минимум).
EVENTS = {
    "экран собран": 1,        # сигналов N
    "касание": 1,             # утро | вечер
    "представлен": 2,         # вид · цель
    "реакция": 2,             # действие · цель [· причина…]
    "совет": 3,               # старое: роль · цель · текст; новое: + тип · контекст · основание · текст
    "ссылка разрешена": 4,    # исход · предложено · фактически · уверенность [· реплика]
    "telegram получено": 4,   # update · сообщение · вид · путь сырья
    "telegram отправлено": 2, # сообщение · связанные объекты… · текст JSON
    "telegram обработано": 2, # update · исход
    "вмешательство": 7,       # ключ · вид · цель · уровень · канал · обязательность · исход
    "голос расшифрован": 2,   # расшифровка · модель [· подсказки]
    "голос ожидает расшифровки": 2,  # оригинал · причина
    "присутствие": 1,         # ноутбук
    "источник принят": 2,     # что · путь
    "источники приняты": 1,
    "разобрано": 2,           # во что · путь
    "решение подтверждено": 2,
    "опубликовано": 2,
    "отправлено": 2,
    "повтор не отброшен": 2,
    "повторы удалены": 2,
    "схема расширена": 2,
    "эхо убрано": 2,                  # что именно · где
    "поставлено в календарь": 2,      # что и когда · путь записи
    "удалено во внешней системе": 2,  # что и по чьему решению · путь записи
    # Исторические имена: новые писатели их не используют, но уже произошедшее
    # остаётся читаемым и не превращается задним числом в ошибку словаря.
    "показано": 2,            # историческое: старая телеметрия, не пишется
    "экран внимания собран": 1,
    "событие записано": 2,
    "обещание переведено в «ждёт»": 2,
    "подтверждено": 2,
    "принято": 2,
    "поправка": 2,
}


@dataclass(frozen=True)
class Entry:
    """Одна строка журнала, разобранная единственным способом."""
    stamp: dt.datetime
    event: str
    parts: tuple[str, ...]
    order: int

    def part(self, index: int, default: str = "") -> str:
        return self.parts[index] if index < len(self.parts) else default


def parse_line(line: str, order: int = 0) -> Entry | None:
    """Разбирает строку журнала; мусор возвращается как None, а не падение."""
    if not line.startswith("- "):
        return None
    parts = line[2:].split(" · ")
    if len(parts) < 2:
        return None
    try:
        stamp = dt.datetime.fromisoformat(parts[0])
    except ValueError:
        return None
    return Entry(stamp, parts[1], tuple(parts[2:]), order)


def parse_lines(lines: list[str], *, events: set[str] | None = None,
                future_ok: bool = False,
                now: dt.datetime | None = None) -> list[Entry]:
    """Строки журнала одним разбором — также для подставных журналов.

    Будущие метки времени отбрасываются: журнал описывает произошедшее, и
    строка из будущего — это либо ошибка часов, либо подделка выборки.
    """
    edge = now or dt.datetime.now().replace(microsecond=0)
    out: list[Entry] = []
    for order, line in enumerate(lines):
        entry = parse_line(line, order)
        if entry is None:
            continue
        if events is not None and entry.event not in events:
            continue
        if not future_ok and entry.stamp > edge:
            continue
        out.append(entry)
    return out


def parse_text(text: str, *, events: set[str] | None = None,
               future_ok: bool = False,
               now: dt.datetime | None = None) -> list[Entry]:
    return parse_lines(text.splitlines(), events=events, future_ok=future_ok,
                       now=now)


def read(root: Path, *, events: set[str] | None = None,
         future_ok: bool = False,
         now: dt.datetime | None = None) -> list[Entry]:
    """Все записи `raw/log.md`; файловый доступ к журналу живёт только здесь."""
    log = root / "raw" / "log.md"
    if not log.is_file():
        return []
    return parse_text(log.read_text(encoding="utf-8"), events=events,
                      future_ok=future_ok, now=now)


def unknown_events(root: Path) -> list[str]:
    """События вне словаря — для линтера: опечатка не должна молчать."""
    seen: list[str] = []
    for entry in read(root, future_ok=True):
        if entry.event not in EVENTS and entry.event not in seen:
            seen.append(entry.event)
    return seen


def clean(value: object) -> str:
    """Один элемент журнала не может разорвать строку или притвориться двумя."""
    return " ".join(str(value).replace("·", " ").split())


def append(root: Path, fields: list[object], *, now: dt.datetime | None = None) -> None:
    append_many(root, [fields], now=now)


def append_once(root: Path, fields: list[object], *, key: int = 1,
                now: dt.datetime | None = None) -> bool:
    """Записывает состояние, если оно изменилось; повтор того же отбрасывает.

    Сравнение идёт с последней записью того же вида про тот же объект — его
    место в строке задаёт `key`. Возврат состояния туда и обратно остаётся
    видимым, а повторение одного и того же в журнал не попадает. Возвращает
    True, если строка записана.
    """
    row = [clean(value) for value in fields]
    if len(row) < 2:
        raise ValueError("повтор узнаётся по виду и объекту — нужно минимум два поля")
    event, parts = row[0], tuple(row[1:])
    if not 0 <= key < len(parts):
        raise ValueError(f"объект вне строки: key={key}, полей {len(parts)}")
    previous: Entry | None = None
    for entry in read(root, events={event}, future_ok=True):
        if entry.part(key) == parts[key]:
            previous = entry
    if previous is not None and previous.parts == parts:
        return False
    append(root, fields, now=now)
    return True


def append_many(root: Path, rows: list[list[object]], *,
                now: dt.datetime | None = None) -> None:
    if not rows:
        return
    log = root / "raw" / "log.md"
    if not log.is_file():
        raise RuntimeError("raw/log.md отсутствует — событие не записано")
    moment = now or dt.datetime.now().replace(microsecond=0)
    stamp = f"{moment:%Y-%m-%dT%H:%M:%S}"
    lines = [f"- {stamp} · " + " · ".join(clean(value) for value in row)
             for row in rows]
    needs_break = log.stat().st_size > 0
    if needs_break:
        with log.open("rb") as stream:
            stream.seek(-1, 2)
            needs_break = stream.read(1) != b"\n"
    with log.open("a", encoding="utf-8") as stream:
        if needs_break:
            stream.write("\n")
        stream.write("\n".join(lines) + "\n")


def touch(root: Path, kind: str, *, now: dt.datetime | None = None) -> str:
    """Отмечает состоявшийся регулярный заход — утренний или вечерний."""
    if kind not in TOUCHES:
        raise ValueError(f"неизвестное касание: {kind}")
    append(root, ["касание", TOUCHES[kind]], now=now)
    return TOUCHES[kind]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="отметить состоявшийся заход")
    parser.add_argument("kind", choices=sorted(TOUCHES))
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    print(f"касание: {touch(args.root.resolve(), args.kind)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
