#!/usr/bin/env python3
"""TickTick — канал действия, а не второй склад.

Разделение ответственности жёсткое и односторонее:

- **склад знает, почему обязательство существует** — из какой встречи взялось,
  кому обещано, чему служит, какой у него источник;
- **TickTick знает, что человеку сделать сейчас** — заголовок, срок, ссылка
  обратно в склад.

Поэтому формулировку, срок и владельца правит только склад, а обратно
принимается только факт о статусе. В тот день, когда TickTick позволят править
текст обязательства или заводить его без источника, он станет вторым `work/` —
и правда о делах разъедется на две несогласуемые копии.

**Связь держится полем `ticktick` у обязательства.** Без него повторный запуск
завёл бы вторую задачу на то же дело — ту же ошибку уже совершал коннектор
расшифровок (двадцать четыре копии одного разговора).

**Что возвращается и что из этого разрешение.** Отметка «выполнено» отвечает на
вопрос «сделал ли», но не на вопрос «что вышло»: `finish` в складе требует
ссылку на результат, и её говорит человек. Перенос срока читается как отложенное
с днём возврата. Исчезновение задачи не значит ничего определённого — сделал и
не отметил, передумал, было не нужно, смахнул случайно, — поэтому оно поднимает
вопрос и ничего не меняет.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

import intake as intake_module
import store
import yaml

API = "https://api.ticktick.com/open/v1"
OAUTH_TOKEN = "https://ticktick.com/oauth/token"
CONFIG = Path.home() / ".config/ticktick-mcp/config.json"
TIMEOUT = 30
LIVE = {"open", "in-progress", "waiting"}
# Канал действия показывает дела, а не ожидания: ждать нечего делать, а красная
# дата у чужого ответа читается как собственная просрочка (решение 13.08.2026).
ACTIONABLE = {"open", "in-progress"}
LOCAL_TZ = ZoneInfo("Europe/Moscow")
_SESSION_TOKEN = ""
_SESSION_EXPIRES = 0.0


class TickTickUnavailable(RuntimeError):
    """Доступа нет. Причина называется; тишиной не подменяется."""


@dataclass
class Outgoing:
    note: store.Note
    reason: str          # почему отправляем: «новая» или «изменилась формулировка»


@dataclass
class Incoming:
    note: store.Note
    event: str           # выполнено | перенесено | исчезло
    detail: str = ""
    proposal: str = ""


@dataclass
class Plan:
    push: list[Outgoing] = field(default_factory=list)
    back: list[Incoming] = field(default_factory=list)
    project: str = ""
    tasks: dict[str, dict] = field(default_factory=dict)


@dataclass
class CaptureBatch:
    saved: list[Path] = field(default_factory=list)
    skipped: int = 0
    evidence: dict[str, str] = field(default_factory=dict)


def _configured_auth() -> dict:
    api_token = os.environ.get("TICKTICK_API_TOKEN", "").strip()
    if api_token:
        return {"api_token": api_token}
    value = os.environ.get("TICKTICK_ACCESS_TOKEN", "").strip()
    expires = os.environ.get("TICKTICK_EXPIRES_AT", "").strip()
    if value:
        return {
            "access_token": value,
            "expires_at": expires,
            "refresh_token": os.environ.get("TICKTICK_REFRESH_TOKEN", "").strip(),
            "client_id": os.environ.get("TICKTICK_CLIENT_ID", "").strip(),
            "client_secret": os.environ.get("TICKTICK_CLIENT_SECRET", "").strip(),
        }
    if not CONFIG.is_file():
        raise TickTickUnavailable(
            f"нет TICKTICK_API_TOKEN, TICKTICK_ACCESS_TOKEN и файла доступа {CONFIG}")
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TickTickUnavailable(f"файл доступа не прочитан: {exc}") from exc


def _refresh(data: dict) -> str:
    global _SESSION_TOKEN, _SESSION_EXPIRES
    refresh = str(data.get("refresh_token") or "").strip()
    client_id = str(data.get("client_id") or "").strip()
    client_secret = str(data.get("client_secret") or "").strip()
    if not (refresh and client_id and client_secret):
        raise TickTickUnavailable(
            "ключ доступа истёк — нужен TICKTICK_API_TOKEN либо refresh-данные")
    form = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh,
    }).encode("utf-8")
    basic = base64.b64encode(
        f"{client_id}:{client_secret}".encode()
    ).decode("ascii")
    request = urllib.request.Request(
        OAUTH_TOKEN, method="POST", data=form,
        headers={"Authorization": f"Basic {basic}",
                 "Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise TickTickUnavailable(
            f"обновление доступа TickTick отклонено: {exc.code} {exc.reason}") from exc
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        raise TickTickUnavailable(f"доступ TickTick не обновлён: {exc}") from exc
    value = str(payload.get("access_token") or "").strip()
    if not value:
        raise TickTickUnavailable("обновление TickTick не вернуло access_token")
    try:
        lifetime = max(60, int(payload.get("expires_in") or 0))
    except (TypeError, ValueError):
        lifetime = 0
    _SESSION_TOKEN = value
    _SESSION_EXPIRES = dt.datetime.now().timestamp() + lifetime if lifetime else 0.0
    return value


def token(*, force_refresh: bool = False) -> str:
    global _SESSION_TOKEN
    data = _configured_auth()
    api_token = str(data.get("api_token") or "").strip()
    if api_token:
        return api_token
    now = dt.datetime.now().timestamp()
    if _SESSION_TOKEN and not force_refresh and (
            not _SESSION_EXPIRES or now + 300 < _SESSION_EXPIRES):
        return _SESSION_TOKEN
    value = str(data.get("access_token") or "").strip()
    if not value:
        raise TickTickUnavailable("в доступе TickTick нет ключа")
    expires = data.get("expires_at")
    expired = False
    if expires:
        try:
            expired = float(expires) < now + 300
        except (TypeError, ValueError) as exc:
            raise TickTickUnavailable("срок ключа TickTick задан не числом") from exc
    if force_refresh or expired:
        return _refresh(data)
    return value


def call(path: str, *, method: str = "GET", body: dict | None = None) -> dict | list:
    access = token()
    for attempt in range(2):
        request = urllib.request.Request(
            f"{API}{path}", method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {access}",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                raw = response.read().decode("utf-8").strip()
            return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and attempt == 0:
                access = token(force_refresh=True)
                continue
            raise TickTickUnavailable(f"{exc.code} {exc.reason} на {path}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise TickTickUnavailable(f"сеть недоступна: {exc}") from exc
    raise TickTickUnavailable("TickTick не принял обновлённый доступ")


def project_id(root: Path) -> str:
    conf = yaml.safe_load((root / "config" / "attention.yml").read_text(
        encoding="utf-8")) or {}
    named = str((conf.get("ticktick") or {}).get("project") or "").strip()
    projects = call("/project")
    if not isinstance(projects, list) or not projects:
        raise TickTickUnavailable("в TickTick нет ни одного списка")
    for one in projects:
        if named and one.get("name") == named:
            return str(one["id"])
    if named:
        raise TickTickUnavailable(f"список «{named}» не найден в TickTick")
    return str(projects[0]["id"])


MARK = "Из склада: "


def already_there(rel: str, tasks: dict[str, dict]) -> str:
    """Есть ли в приложении задача на это же дело — независимо от поля в складе.

    Поля `ticktick` мало: оно записывается после создания задачи, и если между
    созданием и записью что-то пошло не так — второй запуск заводит копию. Так и
    вышло 8 августа: восемь дел получили по две задачи. Поэтому перед созданием
    смотрим на само приложение — метку «Из склада: <путь>» в теле задачи.
    """
    needle = f"{MARK}{rel}"
    for task_id, task in tasks.items():
        if needle in str(task.get("content") or ""):
            return task_id
    return ""


def outgoing(notes: list[store.Note], today: dt.date, horizon: int = 7,
             tasks: dict[str, dict] | None = None) -> list[Outgoing]:
    """Что человеку делать сейчас: обязательства уровня «сейчас» и близкие сроки.

    «Дальше» и «когда-нибудь» в канал действия не попадают: список, куда сложено
    всё, перестаёт быть списком дел. Ожидание чужого ответа — тоже не дело:
    возвращается оно командой `resume`, и тогда задача заводится заново.
    """
    out = []
    for note in notes:
        if note.type != "commitment" or note.status not in ACTIONABLE:
            continue
        due = note.date_field("due")
        soon = due is not None and (due - today).days <= horizon
        if str(note.data.get("level") or "") != "now" and not soon:
            continue
        if note.date_field("review") and (note.date_field("review") or today) > today:
            continue          # отложено человеком — в канал действия не тащим
        if note.data.get("ticktick"):
            continue
        if tasks is not None and already_there(note.rel, tasks):
            continue          # задача уже заведена, связь просто не записалась
        out.append(Outgoing(note, "новая"))
    return out


def incoming(notes: list[store.Note], tasks: dict[str, dict],
             today: dt.date) -> list[Incoming]:
    """Что вернулось из приложения — и что из этого требует вопроса человеку."""
    back: list[Incoming] = []
    for note in notes:
        external = str(note.data.get("ticktick") or "")
        if not external or note.status not in LIVE:
            continue
        task = tasks.get(external)
        if task is None:
            back.append(Incoming(
                note, "исчезло",
                "задачи нет в списке",
                "спросить: сделал, передумал, не нужно или удалил случайно — "
                "и закрыть по ответу"))
            continue
        if str(task.get("status", 0)) == "2" or task.get("completedTime"):
            back.append(Incoming(
                note, "выполнено", "отмечена сделанной",
                "спросить, где результат, и закрыть finish со ссылкой"))
            continue
        window = task_window(task)
        scheduled = parse_datetime(note.data.get("scheduled_start"))
        if window and scheduled and window[0] != scheduled:
            back.append(Incoming(
                note, "перенесено время",
                f"в приложении {window[0]:%Y-%m-%d %H:%M}, в складе {scheduled:%Y-%m-%d %H:%M}",
                "перенести связанный календарный блок"))
            continue
        if window and scheduled is None:
            back.append(Incoming(
                note, "назначено время", f"в приложении {window[0]:%Y-%m-%d %H:%M}",
                "создать связанный календарный блок"))
            continue
        if window is None and scheduled is not None:
            back.append(Incoming(
                note, "время снято", "в приложении осталась дата без времени",
                "снять связанный календарный блок, обязательство оставить"))
            continue
        due = note.date_field("due")
        # Приложение отдаёт срок в UTC: «17 августа» приходит как
        # 2026-08-16T21:00:00+0000. Срез строки дал бы 16-е, запись в карточку —
        # 17-е, и сверка видела бы вечный перенос сама на себя.
        moved_at = parse_datetime(task.get("dueDate"))
        moved = f"{moved_at:%Y-%m-%d}" if moved_at else ""
        if moved and (due is None or moved != due.isoformat()):
            back.append(Incoming(
                note, "перенесено", f"срок в приложении {moved}, в складе {due}",
                f"отложить с днём возврата: UNTIL={moved}"))
        elif not moved and due is not None:
            back.append(Incoming(
                note, "срок снят", f"в складе срок {due}, в приложении не указан",
                "снять срок, обязательство оставить открытым"))
    return back


def fetch_tasks(project: str) -> dict[str, dict]:
    data = call(f"/project/{project}/data")
    tasks = data.get("tasks", []) if isinstance(data, dict) else []
    return {str(task["id"]): task for task in tasks}


def fetch_linked(project: str, notes: list[store.Note],
                 tasks: dict[str, dict]) -> dict[str, dict]:
    """Завершённая задача может исчезнуть из списка, но доступна по своему id."""
    for note in notes:
        task_id = str(note.data.get("ticktick") or "")
        if not task_id or task_id in tasks:
            continue
        try:
            task = call(f"/project/{project}/task/{task_id}")
        except TickTickUnavailable as exc:
            if str(exc).startswith("404 "):
                continue
            raise
        if isinstance(task, dict) and task.get("id"):
            tasks[task_id] = task
    return tasks


def parse_datetime(value: object) -> dt.datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL_TZ)
    return parsed.astimezone(LOCAL_TZ).replace(tzinfo=None)


def stamp(value: dt.datetime) -> str:
    aware = value.replace(tzinfo=LOCAL_TZ)
    return aware.strftime("%Y-%m-%dT%H:%M:%S%z")


def task_window(task: dict, default_minutes: int = 25) -> tuple[dt.datetime, dt.datetime] | None:
    """Явное время задачи. Дата без времени не занимает календарь."""
    if task.get("isAllDay") is not False:
        return None
    start = parse_datetime(task.get("startDate")) or parse_datetime(task.get("dueDate"))
    if start is None:
        return None
    due = parse_datetime(task.get("dueDate"))
    end = due if due and due > start else start + dt.timedelta(minutes=default_minutes)
    return start, end


def content_for(note: store.Note) -> str:
    return (f"{MARK}{note.rel}\n"
            f"Владелец: {note.data.get('owner') or 'не указан'}\n"
            f"Почему: {note.data.get('origin') or 'источник не указан'}\n"
            "Формулировку и срок правит склад, время — календарь.")


def _all_day_due(day: dt.date) -> str:
    return f"{day:%Y-%m-%d}T09:00:00+0300"


def _task_body(task: dict, project: str, changes: dict) -> dict:
    """Open API ждёт идентичность и название даже при частичном изменении."""
    allowed = (
        "id", "projectId", "title", "content", "desc", "isAllDay", "startDate",
        "dueDate", "timeZone", "reminders", "priority", "repeatFlag", "sortOrder",
        "items",
    )
    body = {name: task[name] for name in allowed if name in task}
    body.update(changes)
    body["id"] = str(task.get("id") or body.get("id") or "")
    body["projectId"] = project
    return body


def update_task(project: str, task: dict, changes: dict) -> dict:
    task_id = str(task.get("id") or "")
    if not task_id:
        raise TickTickUnavailable("связанная задача пришла без id")
    body = _task_body(task, project, changes)
    changed = call(f"/task/{task_id}", method="POST", body=body)
    merged = dict(task)
    merged.update(changes)
    if isinstance(changed, dict):
        merged.update(changed)
    return merged


def set_schedule(project: str, task: dict, start: dt.datetime,
                 end: dt.datetime) -> dict:
    return update_task(project, task, {
        "isAllDay": False,
        "startDate": stamp(start),
        "dueDate": stamp(end),
        "timeZone": "Europe/Moscow",
    })


def clear_schedule(project: str, task: dict, day: dt.date | None) -> dict:
    changes: dict[str, object] = {
        "isAllDay": True,
        "startDate": None,
        "timeZone": "Europe/Moscow",
        "dueDate": _all_day_due(day) if day else None,
    }
    return update_task(project, task, changes)


def withdraw_waiting(root: Path, project: str, notes: list[store.Note],
                     tasks: dict[str, dict]) -> list[str]:
    """Ушедшее в ожидание снимается с трекера, связь стирается.

    Задача в приложении — копия: содержание, срок и источник живут в складе,
    поэтому снятие ничего не теряет. Возврат командой `resume` заводит задачу
    заново следующим проходом.
    """
    import workflow

    withdrawn: list[str] = []
    for note in notes:
        if note.type != "commitment" or note.status != "waiting":
            continue
        task_id = str(note.data.get("ticktick") or "")
        if not task_id:
            continue
        if tasks.get(task_id) is not None:
            call(f"/project/{project}/task/{task_id}", method="DELETE")
            tasks.pop(task_id, None)
        workflow.drop_frontmatter(root / note.rel, ["ticktick"])
        withdrawn.append(note.rel)
    return withdrawn


def sync_existing(project: str, notes: list[store.Note],
                  tasks: dict[str, dict]) -> list[str]:
    """Склад владеет текстом и сроком уже связанной задачи."""
    changed: list[str] = []
    for note in notes:
        task_id = str(note.data.get("ticktick") or "")
        task = tasks.get(task_id)
        if not task_id or task is None:
            continue
        if note.status in {"resolved", "cancelled"}:
            if str(task.get("status", 0)) != "2" and not task.get("completedTime"):
                call(f"/project/{project}/task/{task_id}/complete", method="POST")
                changed.append(note.rel)
            continue
        if note.status not in LIVE:
            continue
        updates: dict[str, object] = {}
        title = note.title or Path(note.rel).stem
        content = content_for(note)
        if str(task.get("title") or "") != title:
            updates["title"] = title
        if str(task.get("content") or "") != content:
            updates["content"] = content

        due = note.date_field("due")
        window = task_window(task)
        if window and due and window[0].date() != due:
            duration = window[1] - window[0]
            moved = dt.datetime.combine(due, window[0].time())
            updates.update({
                "isAllDay": False,
                "startDate": stamp(moved),
                "dueDate": stamp(moved + duration),
                "timeZone": "Europe/Moscow",
            })
        elif not window:
            external_due = parse_datetime(task.get("dueDate"))
            if due and (external_due is None or external_due.date() != due
                        or task.get("isAllDay") is not True):
                updates.update({"dueDate": _all_day_due(due), "isAllDay": True})
            elif due is None and external_due is not None:
                updates.update({"dueDate": None, "startDate": None, "isAllDay": True})
        if not updates:
            continue
        tasks[task_id] = update_task(project, task, updates)
        changed.append(note.rel)
    return changed


def _task_revision(task: dict) -> str:
    """API не всегда даёт modifiedTime; значимое состояние всё равно стабильно."""
    for field_name in ("modifiedTime", "updatedTime", "etag"):
        value = str(task.get(field_name) or "").strip()
        if value:
            return value
    relevant = {
        "title": task.get("title"),
        "content": task.get("content"),
        "status": task.get("status"),
        "dueDate": task.get("dueDate"),
        "completedTime": task.get("completedTime"),
        "projectId": task.get("projectId"),
    }
    return hashlib.sha1(json.dumps(
        relevant, ensure_ascii=False, sort_keys=True
    ).encode("utf-8")).hexdigest()[:16]


def capture_record(task: dict, *, event: str = "новая задача",
                   proposal: str = "") -> intake_module.Capture:
    task_id = str(task.get("id") or "").strip()
    if not task_id:
        raise TickTickUnavailable("задача TickTick пришла без id")
    title = str(task.get("title") or "без названия").strip()
    due = str(task.get("dueDate") or "")
    completed = str(task.get("completedTime") or "")
    lines = [
        f"Изменение: {event}",
        f"Задача: {title}",
        f"Статус: {task.get('status', 0)}",
        f"Срок: {due or 'не указан'}",
    ]
    content = str(task.get("content") or "").strip()
    if content:
        lines += ["", "## Текст TickTick", "", content]
    if proposal:
        lines += ["", "## Что требует решения", "", proposal]
    stamp = (str(task.get("modifiedTime") or task.get("createdTime") or "")[:10]
             or dt.date.today().isoformat())
    try:
        day = dt.date.fromisoformat(stamp)
    except ValueError:
        day = dt.date.today()
    return intake_module.Capture(
        source="ticktick",
        external_id=task_id,
        revision=_task_revision(task),
        date=day.isoformat(),
        title=f"TickTick · {title}",
        body="\n".join(lines),
        fields={
            "ticktick_event": event,
            "ticktick_status": task.get("status", 0),
            "ticktick_due": due,
            "ticktick_completed": completed,
        },
    )


def capture_external_batch(root: Path, tasks: dict[str, dict],
                           back: list[Incoming]) -> CaptureBatch:
    """Сохраняет внешний факт и возвращает доказательство по задаче."""
    loaded = store.load(root, "work")
    if loaded.unreadable:
        raise TickTickUnavailable(loaded.complain() or "work/ прочитан не полностью")
    notes = loaded.notes
    linked = {str(note.data.get("ticktick") or ""): note for note in notes
              if note.data.get("ticktick")}
    changes = {str(item.note.data.get("ticktick") or ""): item for item in back}
    captures: list[tuple[str, intake_module.Capture]] = []
    for task_id, task in tasks.items():
        if MARK in str(task.get("content") or "") and task_id not in linked:
            # Связь восстанавливает repair_links(). До этого задача не считается
            # входом с телефона и не создаёт ложный новый источник.
            continue
        change = changes.get(task_id)
        note = linked.get(task_id)
        if note is not None:
            # Эхо собственного действия входом не является. 8 августа обмен
            # положил в приём восемь записей о задачах, которые склад сам и
            # завёл: статус не менялся, заголовок тот же, текст записи —
            # «состояние связанной задачи». Новостью это не было ни в одной
            # строке.
            #
            # Но переименование задачи в телефоне — настоящая новость, и
            # терять её нельзя. Поэтому молчим ровно в одном случае: ничего не
            # произошло и заголовок совпадает с позицией слово в слово.
            same_title = str(task.get("title") or "").strip() == (note.title or "").strip()
            if change is None and same_title:
                continue
            event = change.event if change else "заголовок изменён в приложении"
            proposal = change.proposal if change else ""
            captures.append((task_id, capture_record(
                task, event=event, proposal=proposal)))
        else:
            captures.append((task_id, capture_record(task)))
    saved, skipped = intake_module.save_many(root, [capture for _, capture in captures])
    evidence: dict[str, str] = {}
    for task_id, capture in captures:
        path = intake_module.target_path(root, capture)
        if path.is_file():
            evidence[task_id] = str(path.relative_to(root))
    return CaptureBatch(saved, skipped, evidence)


def capture_external(root: Path, tasks: dict[str, dict],
                     back: list[Incoming]) -> tuple[list[Path], int]:
    """Совместимый вход для ручной команды приёма."""
    batch = capture_external_batch(root, tasks, back)
    return batch.saved, batch.skipped


def apply_incoming(root: Path, items: list[Incoming], tasks: dict[str, dict],
                   evidence: dict[str, str], today: dt.date) -> list[str]:
    """Прямое действие в TickTick — подтверждение, а не догадка системы."""
    import activity
    import workflow

    applied: list[str] = []
    for item in items:
        task_id = str(item.note.data.get("ticktick") or "")
        task = tasks.get(task_id) or {}
        proof = evidence.get(task_id, "")
        if item.event == "выполнено" and proof:
            completed = parse_datetime(task.get("completedTime"))
            workflow.finish_from_external(
                root, item.note.rel,
                on=completed.date() if completed else today,
                resolution=proof,
                source="TickTick",
            )
            applied.append(item.note.rel)
            continue
        if item.event == "срок снят":
            workflow.update_frontmatter(root / item.note.rel, {"due": "null"})
            activity.append_once(root, [
                "реакция", "отложено", item.note.rel, "срок снят в TickTick",
            ])
            applied.append(item.note.rel)
            continue
        if item.event != "перенесено" or task.get("isAllDay") is False:
            continue
        moved = parse_datetime(task.get("dueDate"))
        if moved is None:
            continue
        path = root / item.note.rel
        workflow.update_frontmatter(path, {"due": moved.date().isoformat()})
        activity.append_once(root, [
            "реакция", "отложено", item.note.rel,
            f"срок изменён в TickTick на {moved.date():%d.%m.%Y}",
        ])
        applied.append(item.note.rel)
    return applied


def repair_links(root: Path, tasks: dict[str, dict]) -> list[str]:
    """Восстанавливает однозначную связь по метке, не создавая новую задачу."""
    import workflow

    loaded = store.load(root, "work")
    if loaded.unreadable:
        raise TickTickUnavailable(loaded.complain() or "work/ прочитан не полностью")
    repaired: list[str] = []
    for note in loaded.notes:
        if note.type != "commitment" or note.data.get("ticktick"):
            continue
        task_id = already_there(note.rel, tasks)
        if not task_id:
            continue
        workflow.update_frontmatter(root / note.rel, {"ticktick": task_id})
        repaired.append(note.rel)
    return repaired


def push(root: Path, project: str, items: list[Outgoing]) -> list[str]:
    """Заводит задачи и записывает связь обратно в карточку."""
    import workflow

    done = []
    for item in items:
        note = item.note
        due = note.date_field("due")
        body = {
            "title": note.title or Path(note.rel).stem,
            "projectId": project,
            "content": content_for(note),
        }
        if due:
            body["dueDate"] = f"{due:%Y-%m-%d}T09:00:00+0300"
            body["isAllDay"] = True
        created = call("/task", method="POST", body=body)
        task_id = str(created.get("id") or "")
        if not task_id:
            continue
        workflow.update_frontmatter(root / note.rel, {"ticktick": task_id})
        done.append(f"{note.title} → {task_id}")
    return done


def plan(root: Path, today: dt.date) -> Plan:
    loaded = store.load(root, "work")
    if loaded.unreadable:
        raise TickTickUnavailable(loaded.complain() or "work/ прочитан не полностью")
    notes = loaded.notes
    project = project_id(root)
    tasks = fetch_tasks(project)
    return Plan(push=outgoing(notes, today, tasks=tasks),
                back=incoming(notes, tasks, today),
                project=project, tasks=tasks)


def render(result: Plan) -> str:
    rows = []
    if result.push:
        rows.append(f"В приложение уйдёт задач: {len(result.push)}")
        rows += [f"  · {one.note.title} ({one.reason})" for one in result.push]
    else:
        rows.append("В приложение отправлять нечего.")
    if result.back:
        rows.append("")
        rows.append(f"Вернулось из приложения: {len(result.back)}")
        for one in result.back:
            rows.append(f"  · {one.note.title} — {one.event}: {one.detail}")
            rows.append(f"    {one.proposal}")
    else:
        rows.append("Из приложения ничего не вернулось.")
    return "\n".join(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="обмен с TickTick")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--today", type=dt.date.fromisoformat, default=dt.date.today())
    parser.add_argument("--apply", action="store_true",
                        help="завести задачи в приложении; без флага — только показать")
    parser.add_argument("--capture", action="store_true",
                        help="принять новые и изменённые внешние задачи в raw/inbox")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        result = plan(root, args.today)
    except TickTickUnavailable as exc:
        print(f"TickTick не прочитан: {exc}. Что в нём — неизвестно.")
        return 2
    print(render(result))
    if args.capture:
        try:
            repaired = repair_links(root, result.tasks)
            if repaired:
                loaded = store.load(root, "work")
                if loaded.unreadable:
                    raise TickTickUnavailable(
                        loaded.complain() or "work/ прочитан не полностью")
                result.back = incoming(loaded.notes, result.tasks, args.today)
            saved, skipped = capture_external(root, result.tasks, result.back)
        except (intake_module.IntakeError, TickTickUnavailable, OSError) as exc:
            print(f"вход TickTick не принят: {exc}")
            return 2
        if repaired:
            print(f"\nвосстановлено связей со складом: {len(repaired)}")
        print(f"\nво вход склада принято {len(saved)}, повторов {skipped}")
        for path in saved:
            print(f"  · {path.relative_to(root)}")
    if args.apply and result.push:
        try:
            done = push(root, result.project, result.push)
        except TickTickUnavailable as exc:
            print(f"не отправлено: {exc}")
            return 2
        print("\nзаведено: " + "; ".join(done) if done else "\nничего не заведено")
    elif result.push:
        print("\nЭто пробный прогон. Завести — make ticktick APPLY=1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
