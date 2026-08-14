#!/usr/bin/env python3
"""Автономная сверка календаря, TickTick и склада.

Источники независимы: отказ календаря не мешает принять TickTick и наоборот.
Каждый успешный проход сохраняет только новые версии в ``raw/inbox`` и
создаёт и обновляет задачи, применяет явные действия человека и сводит выбранное
время с отдельными блоками календаря. Обычные встречи наружу не меняются.
"""

from __future__ import annotations

import argparse
import datetime as dt
import time
from dataclasses import dataclass
from pathlib import Path

import agenda
import check_env
import intake
import store
import ticktick
import workflow


@dataclass(frozen=True)
class Result:
    source: str
    saved: int = 0
    skipped: int = 0
    repaired: int = 0
    pushed: int = 0
    updated: int = 0
    applied: int = 0
    created: int = 0
    removed: int = 0
    withdrawn: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


def calendar_once(root: Path, start: dt.date, days: int) -> Result:
    try:
        # Холодный запуск gws с чтением ключа нередко занимает больше трёх
        # секунд. Постоянный процесс может подождать: его окно — пять минут.
        events = agenda.fetch(
            start, max(1, days), timeout=30, include_cancelled=True)
        saved, skipped = agenda.capture(root, events)
    except (agenda.CalendarUnavailable, intake.IntakeError, OSError) as exc:
        return Result("Календарь", error=str(exc))
    return Result("Календарь", saved=len(saved), skipped=skipped)


def ticktick_once(root: Path, today: dt.date) -> Result:
    try:
        project = ticktick.project_id(root)
        tasks = ticktick.fetch_tasks(project)
        loaded = store.load(root, "work")
        if loaded.unreadable:
            raise ticktick.TickTickUnavailable(
                loaded.complain() or "work/ прочитан не полностью")
        ticktick.fetch_linked(project, loaded.notes, tasks)
        repaired = ticktick.repair_links(root, tasks)
        if repaired:
            loaded = store.load(root, "work")
            if loaded.unreadable:
                raise ticktick.TickTickUnavailable(
                    loaded.complain() or "work/ прочитан не полностью")
        back = ticktick.incoming(loaded.notes, tasks, today)
        batch = ticktick.capture_external_batch(root, tasks, back)
        applied = ticktick.apply_incoming(
            root, back, tasks, batch.evidence, today)
        loaded = store.load(root, "work")
        if loaded.unreadable:
            raise ticktick.TickTickUnavailable(
                loaded.complain() or "work/ прочитан не полностью")
        withdrawn = ticktick.withdraw_waiting(root, project, loaded.notes, tasks)
        if withdrawn:
            loaded = store.load(root, "work")
            if loaded.unreadable:
                raise ticktick.TickTickUnavailable(
                    loaded.complain() or "work/ прочитан не полностью")
        updated = ticktick.sync_existing(project, loaded.notes, tasks)
        pushed = ticktick.push(
            root, project,
            ticktick.outgoing(loaded.notes, today, tasks=tasks),
        )
    except (ticktick.TickTickUnavailable, intake.IntakeError,
            workflow.WorkflowError, OSError) as exc:
        return Result("TickTick", error=str(exc))
    return Result(
        "TickTick", saved=len(batch.saved), skipped=batch.skipped,
        repaired=len(repaired), pushed=len(pushed), updated=len(updated),
        applied=len(applied), withdrawn=len(withdrawn),
    )


def _stored_window(note: store.Note) -> tuple[dt.datetime, dt.datetime] | None:
    start = ticktick.parse_datetime(note.data.get("scheduled_start"))
    end = ticktick.parse_datetime(note.data.get("scheduled_end"))
    if start is None:
        return None
    return start, end or start + dt.timedelta(minutes=25)


def _updated_moment(value: object) -> dt.datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return parsed


def _write_schedule(note: store.Note, start: dt.datetime, end: dt.datetime,
                    event_id: str) -> None:
    changes = {
        "scheduled_start": start.isoformat(timespec="minutes"),
        "scheduled_end": end.isoformat(timespec="minutes"),
        "calendar_event": event_id,
        "due": end.date().isoformat(),
    }
    if any(str(note.data.get(name) or "") != value for name, value in changes.items()):
        workflow.update_frontmatter(note.path, changes)


def _clear_schedule(note: store.Note) -> None:
    if any(note.data.get(name) for name in (
            "scheduled_start", "scheduled_end", "calendar_event")):
        workflow.update_frontmatter(note.path, {
            "scheduled_start": "null",
            "scheduled_end": "null",
            "calendar_event": "null",
        })


def schedule_once(root: Path, today: dt.date) -> Result:
    """Сводит явное время TickTick и календарные блоки без догадок о времени."""
    try:
        project = ticktick.project_id(root)
        tasks = ticktick.fetch_tasks(project)
        events = agenda.fetch_timeblocks(timeout=30)
        loaded = store.load(root, "work")
        if loaded.unreadable:
            raise ticktick.TickTickUnavailable(
                loaded.complain() or "work/ прочитан не полностью")
        event_ids = {event.source_id for event in events}
        for note in loaded.notes:
            remembered = str(note.data.get("calendar_event") or "")
            if not remembered or remembered in event_ids:
                continue
            event = agenda.fetch_event(remembered, timeout=30)
            if event is not None:
                events.append(event)
                event_ids.add(event.source_id)
        known_paths = loaded.by_rel
        remembered_ids = {
            str(note.data.get("calendar_event") or "") for note in loaded.notes
            if note.data.get("calendar_event")
        }
        relevant_events = [
            event for event in events
            if event.sync_path in known_paths or event.source_id in remembered_ids
        ]
        block_saved, block_skipped = agenda.capture(root, relevant_events)
        by_path = {event.sync_path: event for event in events if event.sync_path}
        by_id = {event.source_id: event for event in events if event.source_id}
        created = updated = removed = 0
        for note in loaded.notes:
            task_id = str(note.data.get("ticktick") or "")
            task = tasks.get(task_id)
            if note.type != "commitment" or not task_id or task is None:
                continue
            remembered = str(note.data.get("calendar_event") or "")
            event = by_path.get(note.rel) or by_id.get(remembered)
            task_window = ticktick.task_window(task)
            saved_window = _stored_window(note)
            event_window = agenda.event_window(event) if event else None

            if event and event.status == "cancelled":
                day = note.date_field("due")
                tasks[task_id] = ticktick.clear_schedule(project, task, day)
                _clear_schedule(note)
                removed += 1
                continue
            if event is None and remembered:
                day = note.date_field("due")
                if task_window:
                    tasks[task_id] = ticktick.clear_schedule(project, task, day)
                _clear_schedule(note)
                removed += 1
                continue
            if task_window is None:
                if event and event_window:
                    agenda.delete_timeblock(event.source_id)
                    _clear_schedule(note)
                    removed += 1
                continue
            if event is None:
                event = agenda.create_timeblock(
                    note, task_id, task_window[0], task_window[1])
                _write_schedule(note, task_window[0], task_window[1], event.source_id)
                created += 1
                continue
            if event_window is None:
                continue

            task_changed = saved_window is not None and task_window != saved_window
            calendar_changed = saved_window is not None and event_window != saved_window
            winner = task_window
            calendar_wins = False
            if saved_window is None:
                calendar_wins = event_window != task_window
            elif calendar_changed and not task_changed:
                calendar_wins = True
            elif calendar_changed and task_changed:
                calendar_at = _updated_moment(event.updated)
                task_at = _updated_moment(
                    task.get("modifiedTime") or task.get("updatedTime"))
                calendar_wins = task_at is None or (
                    calendar_at is not None and calendar_at >= task_at)
            if calendar_wins:
                winner = event_window
                tasks[task_id] = ticktick.set_schedule(
                    project, task, winner[0], winner[1])
                updated += 1

            expected_revision = agenda.timeblock_revision(
                note.rel, task_id, note.title, winner[0], winner[1])
            if (event_window != winner or event.title != note.title
                    or event.sync_revision != expected_revision):
                event = agenda.update_timeblock(
                    event, note, task_id, winner[0], winner[1])
                updated += 1
            _write_schedule(note, winner[0], winner[1], event.source_id)
    except (agenda.CalendarUnavailable, ticktick.TickTickUnavailable,
            intake.IntakeError, workflow.WorkflowError, OSError) as exc:
        return Result("Расписание", error=str(exc))
    return Result(
        "Расписание", saved=len(block_saved), skipped=block_skipped,
        created=created, updated=updated, removed=removed)


def reconcile(root: Path, today: dt.date, days: int) -> list[Result]:
    """Оба источника опрашиваются всегда, даже если первый недоступен."""
    return [
        calendar_once(root, today, days),
        ticktick_once(root, today),
        schedule_once(root, today),
    ]


def render(results: list[Result], when: dt.datetime | None = None) -> str:
    lines = []
    if when:
        lines.append(f"Сверка {when:%Y-%m-%d %H:%M}")
    for result in results:
        if result.error:
            lines.append(f"{result.source} не прочитан: {result.error}. Состояние неизвестно.")
            continue
        detail = f"принято {result.saved}, повторов {result.skipped}"
        if result.repaired:
            detail += f", восстановлено связей {result.repaired}"
        if result.pushed:
            detail += f", отправлено задач {result.pushed}"
        if result.updated:
            detail += f", обновлено {result.updated}"
        if result.withdrawn:
            detail += f", снято с трекера {result.withdrawn}"
        if result.applied:
            detail += f", применено изменений {result.applied}"
        if result.created:
            detail += f", создано блоков {result.created}"
        if result.removed:
            detail += f", снято блоков {result.removed}"
        lines.append(f"{result.source}: {detail}.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="сверить календарь, TickTick и склад")
    parser.add_argument("--root", type=Path,
                        default=Path(__file__).resolve().parents[1])
    parser.add_argument("--today", type=dt.date.fromisoformat,
                        help="фиксированная дата для проверки; в постоянном режиме по умолчанию меняется сама")
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval", type=int, default=300,
                        help="секунд между проходами в постоянном режиме")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    check_env.load_secrets(root / "config" / "secrets.env")
    last_code = 0
    try:
        while True:
            today = args.today or dt.date.today()
            results = reconcile(root, today, args.days)
            print(render(results, dt.datetime.now()), flush=True)
            last_code = 0 if all(result.ok for result in results) else 2
            if not args.watch:
                return last_code
            time.sleep(max(30, args.interval))
    except KeyboardInterrupt:
        return last_code


if __name__ == "__main__":
    raise SystemExit(main())
