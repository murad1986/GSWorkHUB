#!/usr/bin/env python3
"""Календарь как ситуативный контекст и внешний источник событий.

Для живого экрана события читаются без записи: это превращает общий сигнал в
конкретный. Отдельная команда приёма кладёт новую версию календарного события в
``raw/inbox``. Так календарь виден не только пока доступна сеть, но запланированная
встреча всё ещё не притворяется состоявшейся встречей в ``raw/meetings``.

Повтор одной версии отбрасывается. Перенос, отмена или правка — новая версия
того же внешнего объекта и новое событие raw; предыдущая запись не меняется.

Доступа может не быть, и это не «встреч нет». Отсутствие доступа возвращается
явной причиной: склад обязан называть незнание, а не подменять его тишиной.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

import intake as intake_module
from store import Note

TIMEOUT = 3
LOCAL_TZ = ZoneInfo("Europe/Moscow")
STOP_WORDS = {"встреча", "звонок", "созвон", "с", "и", "по", "на", "у", "в",
              "meeting", "call", "sync", "созвон-", "обсуждение", "разбор"}


@dataclass
class Event:
    start: dt.datetime
    end: dt.datetime | None
    title: str
    attendees: list[str] = field(default_factory=list)
    all_day: bool = False
    container: str = ""      # к чему относится в складе, если удалось сопоставить
    people: list[str] = field(default_factory=list)
    source_id: str = ""
    ical_uid: str = ""
    updated: str = ""
    status: str = "confirmed"
    location: str = ""
    description: str = ""
    recurring_id: str = ""
    original_start: dt.datetime | None = None
    sync_path: str = ""
    sync_task: str = ""
    sync_revision: str = ""

    @property
    def when(self) -> str:
        return "весь день" if self.all_day else f"{self.start:%H:%M}"

    @property
    def minutes(self) -> int | None:
        if self.all_day or not self.end:
            return None
        return max(0, int((self.end - self.start).total_seconds() // 60))


class CalendarUnavailable(RuntimeError):
    """Календарь не прочитан. Причина называется, тишиной не подменяется."""


SYNC_FLAG = "workhub"
SYNC_PATH = "workhub_path"
SYNC_TASK = "workhub_ticktick"
SYNC_REVISION = "workhub_revision"


def _gws(method: str, params: dict, body: dict | None = None,
         *, timeout: int = TIMEOUT) -> dict:
    command = [
        "gws", "calendar", "events", method,
        "--params", json.dumps(params), "--format", "json",
    ]
    if body is not None:
        command += ["--json", json.dumps(body, ensure_ascii=False)]
    if method == "delete":
        command += ["--output", os.devnull]
    try:
        proc = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise CalendarUnavailable(f"не ответил за {timeout:g} сек") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise CalendarUnavailable(f"команда gws не выполнена: {exc}") from exc
    try:
        payload = json.loads(proc.stdout) if proc.stdout.strip() else {}
    except json.JSONDecodeError as exc:
        raise CalendarUnavailable("ответ календаря не разобран") from exc
    if proc.returncode or "error" in payload:
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            reason = f"{error.get('code', '')} {error.get('message', 'отказ календаря')}"
        else:
            reason = proc.stderr.strip() or "отказ календаря"
        raise CalendarUnavailable(reason.strip())
    return payload if isinstance(payload, dict) else {}


def _parse_stamp(value: dict) -> tuple[dt.datetime, bool]:
    if "dateTime" in value:
        parsed = dt.datetime.fromisoformat(str(value["dateTime"]).replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(LOCAL_TZ).replace(tzinfo=None)
        return parsed, False
    return dt.datetime.fromisoformat(value["date"]), True


def _parse_events(payload: dict, *, include_cancelled: bool) -> list[Event]:
    events: list[Event] = []
    for item in payload.get("items", []):
        status = str(item.get("status") or "confirmed")
        if status == "cancelled" and not include_cancelled:
            continue
        start_value = item.get("start") or item.get("originalStartTime") or {}
        try:
            start, all_day = _parse_stamp(start_value)
        except (KeyError, ValueError, TypeError):
            updated = str(item.get("updated") or "")
            if not updated:
                continue
            start = dt.datetime.fromisoformat(updated.replace("Z", "+00:00")).replace(
                tzinfo=None)
            all_day = False
        end = None
        if item.get("end"):
            end, _ = _parse_stamp(item["end"])
        original_start = None
        if item.get("originalStartTime"):
            try:
                original_start, _ = _parse_stamp(item["originalStartTime"])
            except (KeyError, ValueError, TypeError):
                original_start = None
        private = ((item.get("extendedProperties") or {}).get("private") or {})
        events.append(Event(
            start=start, end=end, all_day=all_day,
            title=(item.get("summary") or "без названия").strip(),
            attendees=[a.get("email", "") for a in item.get("attendees", [])],
            source_id=str(item.get("id") or ""),
            ical_uid=str(item.get("iCalUID") or ""),
            updated=str(item.get("updated") or ""),
            status=status,
            location=str(item.get("location") or ""),
            description=str(item.get("description") or ""),
            recurring_id=str(item.get("recurringEventId") or ""),
            original_start=original_start,
            sync_path=str(private.get(SYNC_PATH) or ""),
            sync_task=str(private.get(SYNC_TASK) or ""),
            sync_revision=str(private.get(SYNC_REVISION) or ""),
        ))
    return events


def fetch(day: dt.date, days: int = 1, *, timeout: int = TIMEOUT,
          include_cancelled: bool = False) -> list[Event]:
    """События основного календаря за окно начиная с дня."""
    edge = day + dt.timedelta(days=days)
    params = {
        "calendarId": "primary",
        "timeMin": f"{day:%Y-%m-%d}T00:00:00+03:00",
        "timeMax": f"{edge:%Y-%m-%d}T00:00:00+03:00",
        "singleEvents": True,
        "orderBy": "startTime",
        "maxResults": 50,
    }
    if include_cancelled:
        params["showDeleted"] = True
    return _parse_events(
        _gws("list", params, timeout=timeout),
        include_cancelled=include_cancelled,
    )


def fetch_timeblocks(*, timeout: int = TIMEOUT) -> list[Event]:
    """Все блоки, созданные складом, включая удалённые человеком."""
    params = {
        "calendarId": "primary",
        "privateExtendedProperty": f"{SYNC_FLAG}=1",
        "singleEvents": True,
        "showDeleted": True,
        "maxResults": 2500,
    }
    return _parse_events(
        _gws("list", params, timeout=timeout), include_cancelled=True)


def fetch_event(event_id_value: str, *, timeout: int = TIMEOUT) -> Event | None:
    """Дочитывает известный блок, даже если удаление стёрло служебные поля."""
    try:
        payload = _gws("get", {
            "calendarId": "primary", "eventId": event_id_value,
        }, timeout=timeout)
    except CalendarUnavailable as exc:
        if str(exc).startswith(("404 ", "410 ")):
            return None
        raise
    parsed = _parse_events({"items": [payload]}, include_cancelled=True)
    return parsed[0] if parsed else None


def _fallback_revision(event: Event) -> str:
    payload = {
        "start": event.start.isoformat(),
        "end": event.end.isoformat() if event.end else "",
        "title": event.title,
        "attendees": sorted(event.attendees),
        "status": event.status,
        "location": event.location,
        "description": event.description,
    }
    return hashlib.sha1(json.dumps(
        payload, ensure_ascii=False, sort_keys=True
    ).encode("utf-8")).hexdigest()[:16]


def timeblock_revision(path: str, task_id: str, title: str,
                       start: dt.datetime, end: dt.datetime,
                       status: str = "confirmed") -> str:
    payload = {
        "path": path,
        "task": task_id,
        "title": title,
        "start": start.isoformat(timespec="minutes"),
        "end": end.isoformat(timespec="minutes"),
        "status": status,
    }
    return hashlib.sha1(json.dumps(
        payload, ensure_ascii=False, sort_keys=True
    ).encode("utf-8")).hexdigest()[:16]


def event_revision(event: Event) -> str:
    end = event.end or event.start
    return timeblock_revision(
        event.sync_path, event.sync_task, event.title,
        event.start, end, event.status,
    )


def event_id(path: str) -> str:
    """Предсказуемый id не даёт создать дубль после потери ответа API."""
    # Google принимает только base32hex: цифры и a-v. SHA-1 hex уже входит в
    # этот алфавит; префикс ``a1`` тоже. Буква w в прежнем ``wh`` отвергалась.
    return "a1" + hashlib.sha1(path.encode("utf-8")).hexdigest()


def event_window(event: Event) -> tuple[dt.datetime, dt.datetime] | None:
    if event.status == "cancelled" or event.all_day:
        return None
    end = event.end or event.start + dt.timedelta(minutes=25)
    return event.start, end


def _calendar_stamp(value: dt.datetime) -> str:
    return value.replace(tzinfo=LOCAL_TZ).isoformat(timespec="seconds")


def timeblock_body(note: Note, task_id: str, start: dt.datetime,
                   end: dt.datetime, *, include_id: bool = False) -> dict:
    revision = timeblock_revision(note.rel, task_id, note.title, start, end)
    body: dict[str, object] = {
        "summary": note.title,
        "description": (
            f"Задача TickTick: {task_id}\n"
            f"Из склада: {note.rel}\n"
            "Переносите этот блок свободно: новое время вернётся в задачу."
        ),
        "start": {"dateTime": _calendar_stamp(start), "timeZone": "Europe/Moscow"},
        "end": {"dateTime": _calendar_stamp(end), "timeZone": "Europe/Moscow"},
        "transparency": "opaque",
        "extendedProperties": {"private": {
            SYNC_FLAG: "1",
            SYNC_PATH: note.rel,
            SYNC_TASK: task_id,
            SYNC_REVISION: revision,
        }},
    }
    if include_id:
        body["id"] = event_id(note.rel)
    return body


def create_timeblock(note: Note, task_id: str, start: dt.datetime,
                     end: dt.datetime) -> Event:
    body = timeblock_body(note, task_id, start, end, include_id=True)
    payload = _gws("insert", {
        "calendarId": "primary", "sendUpdates": "none",
    }, body, timeout=30)
    event = _parse_events({"items": [payload]}, include_cancelled=True)
    if event:
        return event[0]
    return Event(
        start=start, end=end, title=note.title,
        source_id=str(body["id"]), sync_path=note.rel, sync_task=task_id,
        sync_revision=timeblock_revision(note.rel, task_id, note.title, start, end),
    )


def update_timeblock(event: Event, note: Note, task_id: str,
                     start: dt.datetime, end: dt.datetime) -> Event:
    payload = _gws("patch", {
        "calendarId": "primary", "eventId": event.source_id,
        "sendUpdates": "none",
    }, timeblock_body(note, task_id, start, end), timeout=30)
    parsed = _parse_events({"items": [payload]}, include_cancelled=True)
    if parsed:
        return parsed[0]
    return Event(
        start=start, end=end, title=note.title, source_id=event.source_id,
        sync_path=note.rel, sync_task=task_id,
        sync_revision=timeblock_revision(note.rel, task_id, note.title, start, end),
    )


def delete_timeblock(event_id_value: str) -> None:
    _gws("delete", {
        "calendarId": "primary", "eventId": event_id_value,
        "sendUpdates": "none",
    }, timeout=30)


def capture_record(event: Event) -> intake_module.Capture:
    """Дословный снимок расписания; это source, а не состоявшаяся meeting."""
    ical_identity = event.ical_uid
    if event.ical_uid and (event.recurring_id or event.original_start):
        occurrence = event.original_start or event.start
        ical_identity = f"{event.ical_uid}#{occurrence.isoformat()}"
    external_id = event.source_id or ical_identity
    if not external_id:
        external_id = hashlib.sha1(
            f"{event.start.isoformat()}\0{event.title}".encode()
        ).hexdigest()
    revision = event.updated or _fallback_revision(event)
    lines = [
        f"Статус: {event.status}",
        f"Начало: {event.start.isoformat(timespec='minutes')}",
        f"Конец: {event.end.isoformat(timespec='minutes') if event.end else 'не указан'}",
        f"Весь день: {'да' if event.all_day else 'нет'}",
    ]
    if event.attendees:
        lines.append("Участники: " + ", ".join(event.attendees))
    if event.location:
        lines.append("Место: " + event.location)
    if event.description:
        lines += ["", "## Описание календаря", "", event.description]
    aliases = tuple(alias for alias in (ical_identity,) if alias != external_id)
    fields: dict[str, object] = {
        "calendar_status": event.status,
        "scheduled_start": event.start.isoformat(timespec="minutes"),
        "scheduled_end": (event.end.isoformat(timespec="minutes")
                          if event.end else ""),
    }
    if event.sync_path:
        fields["workhub_path"] = event.sync_path
    if event.sync_task:
        fields["ticktick"] = event.sync_task
    return intake_module.Capture(
        source="google-calendar",
        external_id=external_id,
        revision=revision,
        aliases=aliases,
        date=event.start.date().isoformat(),
        title=f"Календарь · {event.title}",
        body="\n".join(lines),
        fields=fields,
    )


def worth_capturing(event: Event) -> bool:
    """Стоит ли класть событие в приём как сырьё.

    Повторяющееся событие без участников и без связи со складом — это ритм
    жизни человека, а не рабочее событие: пятничная молитва, спорт, семейный
    ужин. В дне оно видно через `today`, и этого достаточно; в приёме оно
    только копится — по записи в неделю, вечно.

    Проверено 9 августа: еженедельная Джума, идущая с июля 2025, положила в
    приём две записи за один обмен и клала бы по одной каждую неделю.
    Разовое событие без участников остаётся входом: оно случается один раз и
    обычно означает договорённость.
    """
    if (event.sync_path and event.sync_revision
            and event.sync_revision == event_revision(event)):
        return False          # собственная запись, человек её не менял
    if not event.recurring_id:
        return True
    return bool(event.attendees or event.container)


def capture(root: Path, events: list[Event]) -> tuple[list[Path], int]:
    worthy = [event for event in events if worth_capturing(event)]
    return intake_module.save_many(root, [capture_record(event) for event in worthy])


def _words(value: str) -> set[str]:
    return {w for w in re.findall(r"\w+", value.lower())
            if len(w) > 2 and w not in STOP_WORDS}


def match(events: list[Event], notes: list[Note]) -> list[Event]:
    """Связывает встречу с контейнером и людьми по названию.

    Сопоставление намеренно грубое и объяснимое: совпадение слов названия с
    именем контейнера, его заголовком, названием организации или именем и
    прозвищами человека. Догадки без совпадения слов не делаются — лучше
    встреча без контекста, чем контекст не той встречи.
    """
    holders = [n for n in notes if n.type in {"client", "program"}]
    people = [n for n in notes if n.type == "person"]

    def keys(note: Note) -> set[str]:
        # Префикс — короткое имя контейнера, которым и пользуются в календаре:
        # встреча называется «Альфа · разбор», а не полным именем клиента.
        parts = [note.title or "", str(note.data.get("name") or ""),
                 str(note.data.get("org") or ""), str(note.data.get("prefix") or ""),
                 Path(note.rel).parent.name]
        aliases = note.data.get("aliases") or []
        if isinstance(aliases, list):
            parts += [str(a) for a in aliases]
        return {w for part in parts for w in _words(part)}

    for event in events:
        seen = _words(event.title)
        if not seen:
            continue
        best, best_hits = "", 0
        for holder in holders:
            hits = len(seen & keys(holder))
            if hits > best_hits:
                best, best_hits = str(Path(holder.rel).parent), hits
        for person in people:
            if seen & keys(person):
                event.people.append(person.rel)
                if not best:
                    container = str(person.data.get("container") or "")
                    if container:
                        best = container
        event.container = best
    return events


@dataclass
class Context:
    event: Event
    mine: list[Note] = field(default_factory=list)      # я обещал
    theirs: list[Note] = field(default_factory=list)    # мне обещали
    questions: list[Note] = field(default_factory=list)  # открытые вопросы
    last_meeting: Note | None = None

    @property
    def empty(self) -> bool:
        return not (self.mine or self.theirs or self.questions)


def context(event: Event, notes: list[Note], raw: list[Note] | None = None,
            limit: int = 3) -> Context:
    """Что склад знает к этой встрече: чем закончилась прошлая и что висит.

    Порядок обязателен: сначала мои обещания. Встреча, на которую приходят с
    невыполненным обещанием, идёт иначе, и знать об этом надо до неё, а не после.
    """
    out = Context(event)
    if not event.container:
        return out
    inside = [n for n in notes if n.rel.startswith(event.container + "/")]
    for note in inside:
        if note.type == "commitment" and note.status in {"open", "in-progress", "waiting"}:
            if note.data.get("direction") == "inbound":
                out.theirs.append(note)
            else:
                out.mine.append(note)
        elif note.type == "question" and note.status == "open":
            out.questions.append(note)

    def due_key(note: Note):
        return (note.date_field("due") or dt.date.max, note.title or "")

    out.mine.sort(key=due_key)
    out.theirs.sort(key=due_key)
    out.mine, out.theirs = out.mine[:limit], out.theirs[:limit]
    out.questions = out.questions[:limit]

    meetings = [n for n in (raw or [])
                if n.type == "meeting"
                and str(n.data.get("container") or "") == event.container]
    if meetings:
        out.last_meeting = max(meetings, key=lambda n: n.date_field("date") or dt.date.min)
    return out


def render(items: list[Context], *, today: dt.date) -> list[str]:
    """Строки для экрана дня: время, с кем, и что висит именно по нему."""
    rows: list[str] = []
    for one in items:
        event = one.event
        where = f" · {Path(event.container).name}" if event.container else ""
        rows.append(f"{event.when} · {event.title}{where}")
        if not event.container:
            rows.append("    складу эта встреча незнакома — контекста нет")
            continue
        for note in one.mine:
            due = note.date_field("due")
            mark = ""
            if due and due < today:
                mark = f", просрочено на {(today - due).days} дн."
            elif due:
                mark = f", срок {due:%d.%m}"
            rows.append(f"    я обещал: {note.title}{mark}")
        for note in one.theirs:
            rows.append(f"    он обещал: {note.title}")
        for note in one.questions:
            rows.append(f"    открыт вопрос: {note.title}")
        if one.last_meeting:
            when = one.last_meeting.date_field("date")
            rows.append(f"    прошлая встреча {when:%d.%m}" if when else "")
        if one.empty:
            rows.append("    ничего не висит — встреча без хвостов")
    return [row for row in rows if row]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="принять события Google Calendar в raw/inbox")
    parser.add_argument("--root", type=Path,
                        default=Path(__file__).resolve().parents[1])
    parser.add_argument("--from", dest="start", type=dt.date.fromisoformat,
                        default=dt.date.today())
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        events = fetch(args.start, max(1, args.days), include_cancelled=True)
        records = [capture_record(event) for event in events]
        if args.dry_run:
            known = intake_module.known_signatures(args.root.resolve(),
                                                   "google-calendar")
            fresh = [record for record in records if not (record.signatures & known)]
            print(f"Календарь: пришло бы {len(fresh)}, повторов {len(records) - len(fresh)}.")
            return 0
        saved, skipped = intake_module.save_many(args.root.resolve(), records)
    except (CalendarUnavailable, intake_module.IntakeError, OSError) as exc:
        print(f"Календарь не принят: {exc}. Что в нём — неизвестно.")
        return 2
    print(f"Календарь: принято {len(saved)}, повторов отброшено {skipped}.")
    for path in saved:
        print(f"  · {path.relative_to(args.root.resolve())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
