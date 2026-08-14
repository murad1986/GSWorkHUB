#!/usr/bin/env python3
"""Живой короткий вход: что делать сейчас.

Каждый запуск читает `raw/` и `work/`. Пересчитанные виды в `wiki/` намеренно не
читаются: вчерашний снимок не должен управлять сегодняшним выбором.
"""

from __future__ import annotations

import argparse
import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

import activity
import agenda as agenda_module
import capacity as capacity_module
import tracker
import welcome
import yaml
from store import Note, load


@dataclass
class TodayLine:
    text: str
    kind: str
    target: str


@dataclass
class TodayScreen:
    header: str
    lines: list[TodayLine]
    prompt: str
    hidden: int = 0
    agenda: list[str] = field(default_factory=list)   # что сегодня в календаре


def config(root: Path) -> dict:
    path = root / "config" / "attention.yml"
    if not path.exists():
        return {
            "limit": 7,
            "due_soon_days": 3,
            "tracker": {"wip_limit": 3, "candidates": 3},
            "capacity": {"full": 3, "half": 2, "low": 1},
        }
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _container(note: Note) -> str:
    return note.container or str(note.data.get("container") or "Без контейнера")


def _age(note: Note, today: dt.date) -> str:
    started = note.date_field("started")
    if not started:
        return "дата начала неизвестна"
    days = max(0, (today - started).days)
    return "начато сегодня" if days == 0 else f"в работе {days} дн."


def _records(count: int) -> str:
    tail = count % 100
    if 11 <= tail <= 14:
        word = "записей"
    elif count % 10 == 1:
        word = "запись"
    elif count % 10 in {2, 3, 4}:
        word = "записи"
    else:
        word = "записей"
    return f"{count} {word}"


def _capacity(root: Path, chosen: str, today: dt.date) -> tuple[str, str]:
    if chosen != "auto":
        return chosen, ""
    measured = capacity_module.measure(today=today)
    if measured.level == "unknown":
        return "unknown", measured.reason
    return measured.level, measured.reason


def agenda_lines(root: Path, today: dt.date, notes: list[Note]) -> list[str]:
    """Что сегодня в календаре и что по этому висит в складе.

    Календарь не добавляет работы — он делает уже поднятое конкретным: не
    «просрочено обещание», а «через два часа встреча с тем, кому оно обещано».
    Поэтому строки идут отдельным блоком и в лимит рабочих строк не входят.

    Нет доступа — так и говорим. Пустой календарь и нечитаемый календарь для
    человека выглядят одинаково, а значат противоположное.
    """
    try:
        events = agenda_module.match(agenda_module.fetch(today), notes)
    except agenda_module.CalendarUnavailable as exc:
        return [f"Календарь не прочитан: {exc}. Что в нём — неизвестно."]
    if not events:
        return []
    raw_notes = load(root, "raw").notes
    contexts = [agenda_module.context(event, notes, raw_notes) for event in events]
    return agenda_module.render(contexts, today=today)


def build(root: Path, today: dt.date, capacity: str = "auto") -> TodayScreen:
    conf = config(root)
    tracker_conf = conf.get("tracker") or {}
    limit = int(conf.get("limit", 7))
    work = load(root, "work")
    notes = work.notes
    commitments = [note for note in notes if note.type == "commitment"]
    in_progress = [note for note in commitments if note.status == "in-progress"]
    waiting = [note for note in commitments if note.status == "waiting"]
    active_wip = [note for note in commitments if tracker.counts_as_wip(note)]
    wip_limit = int(tracker_conf.get("wip_limit", 3))
    free = max(0, wip_limit - len(active_wip))
    level, capacity_reason = _capacity(root, capacity, today)
    capacity_count = int((conf.get("capacity") or {}).get(level, 0))

    candidates = [
        note for note in commitments
        if note.status == "open"
        and (tracker.level_of(note) == "now"
             or (tracker.parse_due(note)
                 and (tracker.parse_due(note) - today).days
                 <= int(conf.get("due_soon_days", 3))))
    ]
    candidates.sort(key=lambda note: (-tracker.candidate_score(note, today, conf)[0],
                                      note.title))

    items: list[TodayLine] = []
    pending, broken_work = welcome.unprocessed(root)
    if pending:
        oldest = next((match.group(0) for path in pending
                       if (match := welcome.DATE.search(path.name))), "")
        tail = f", старейшая {oldest}" if oldest else ""
        first = str(pending[0].relative_to(root))
        items.append(TodayLine(
            f"Разобрать вход · {_records(len(pending))}{tail} — первой {first}",
            "ждёт разбора",
            first,
        ))

    if work.unreadable:
        rel, why = work.unreadable[0]
        items.append(TodayLine(
            f"Восстановить чтение · {len(work.unreadable)} файлов work выпали "
            f"из расчёта — первый {rel}: {why}",
            "целостность",
            rel,
        ))
    elif broken_work:
        items.append(TodayLine(
            f"Восстановить чтение · {broken_work} файлов work не прочитаны при "
            "проверке входа",
            "целостность",
            "work",
        ))

    for note in sorted(in_progress, key=lambda one: (one.date_field("started")
                                                      or dt.date.min, one.title)):
        items.append(TodayLine(
            f"Продолжить · {_container(note)} · {note.title} — {_age(note, today)}",
            "в работе",
            note.rel,
        ))

    for note in sorted(waiting, key=lambda one: one.title):
        verb = "Проверить ответ" if note.data.get("direction") == "inbound" \
            else "Проверить ожидание"
        items.append(TodayLine(
            f"{verb} · {_container(note)} · {note.title}",
            "ждёт",
            note.rel,
        ))

    take_count = min(free, capacity_count, int(tracker_conf.get("candidates", 3)))
    for note in candidates[:take_count]:
        _, why = tracker.candidate_score(note, today, conf)
        items.append(TodayLine(
            f"Взять? · {_container(note)} · {note.title} — {why}",
            "кандидат",
            note.rel,
        ))

    # Заголовок не является рабочей строкой. Из лимита резервируется одна строка
    # на явный вопрос/ограничение: список без следующего действия не замыкает цикл.
    shown = items[:max(0, limit - 1)]
    hidden = max(0, len(items) - len(shown))
    if free == 0:
        prompt = "Новое не берём: сначала завершить или отложить одно из начатых."
    elif level == "unknown":
        prompt = ("Новое не предлагаю: ёмкость неизвестна"
                  + (f" — {capacity_reason}" if capacity_reason else "")
                  + ". Задай CAP=full|half|low.")
    elif any(line.kind == "кандидат" for line in shown):
        prompt = "Что берём? Без твоего ответа ни одно дело не начнётся."
    elif in_progress:
        prompt = "Новых кандидатов не показываю: продолжаем уже начатое."
    else:
        prompt = "Кандидатов на свободное место нет."
    if hidden:
        prompt += f" Ещё {hidden} строк скрыто лимитом."

    capacity_name = {"full": "полная", "half": "половина", "low": "низкая",
                     "unknown": "неизвестна"}.get(level, level)
    header = (f"Сегодня {today:%d.%m.%Y} · ёмкость {capacity_name} · "
              f"в работе {len(active_wip)} из {wip_limit}")
    return TodayScreen(header, shown, prompt, hidden,
                       agenda=agenda_lines(root, today, notes))


def render(screen: TodayScreen) -> str:
    rows = [screen.header]
    if screen.agenda:
        rows.append("")
        rows.extend(screen.agenda)
        rows.append("")
    rows.extend(f"{index}. {line.text}" for index, line in enumerate(screen.lines, 1))
    rows.append(screen.prompt)
    return "\n".join(rows)


def record(root: Path, screen: TodayScreen, *, now: dt.datetime | None = None) -> None:
    activity.append_many(
        root,
        [["представлен", line.kind, line.target]
         for line in screen.lines if line.target not in {"work", "raw"}],
        now=now,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="живой короткий вход")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--today", type=dt.date.fromisoformat, default=dt.date.today())
    parser.add_argument("--capacity", choices=["full", "half", "low", "auto"],
                        default="auto")
    parser.add_argument("--dry-run", action="store_true",
                        help="показать без записи события «представлен»")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    screen = build(root, args.today, args.capacity)
    print(render(screen))
    if not args.dry_run:
        try:
            record(root, screen)
        except RuntimeError as exc:
            print(f"\nПоказ состоялся, но не записан: {exc}")
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
