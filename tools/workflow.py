#!/usr/bin/env python3
"""Явные переходы обязательств и реакции на представленные строки.

Просмотр никогда не меняет позицию. Каждая команда здесь — подтверждённое
действие человека: взять, приостановить, продолжить, завершить или отменить.
Отдельные реакции на совет (`defer`, `dismiss`, `correct`) пишут только факт
ответа и не переписывают обязательство.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import tempfile
from pathlib import Path

import activity
import store
import yaml

STATE_ACTIONS = {"take", "wait", "resume", "finish", "cancel"}
FEEDBACK_ACTIONS = {"defer", "dismiss", "correct"}
REACTIONS = {
    "take": "взято",
    "wait": "отложено",
    "resume": "взято",
    "finish": "завершено",
    "cancel": "отменено",
    "defer": "отложено",
    "dismiss": "отклонено",
    "correct": "поправлено",
}
ALLOWED_FROM = {
    "take": {"open"},
    "wait": {"in-progress"},
    "resume": {"waiting"},
    "finish": {"in-progress", "waiting"},
    "cancel": {"open", "in-progress", "waiting"},
}
SAFE_SCALAR = re.compile(r"^[A-Za-z0-9_./-]+$")
EXTERNAL_STATUS_CHANNELS = ("ticktick",)


class WorkflowError(RuntimeError):
    """Команда не выполнена: состояние оставлено без изменений."""


def _inside(root: Path, path: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def resolve_target(root: Path, target: str, *, commitment: bool) -> tuple[Path, dict]:
    root = root.resolve()
    path = (root / target).resolve()
    if not _inside(root, path) or not path.is_file():
        raise WorkflowError(f"цель не найдена внутри склада: {target}")
    rel = str(path.relative_to(root))
    if not rel.startswith(("work/", "raw/")):
        raise WorkflowError("реакцию можно связать только с raw/ или work/")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise WorkflowError(f"{target} не прочитан: {type(exc).__name__}") from exc
    data, why = store.parse(text)
    if data is None:
        raise WorkflowError(f"{target}: {why}")
    if commitment and (not rel.startswith("work/") or data.get("type") != "commitment"):
        raise WorkflowError(f"{target} — не обязательство в work/")
    return path, data


def resolve_evidence(root: Path, value: str | None) -> str:
    root = root.resolve()
    if not value:
        raise WorkflowError("для завершения или отмены нужна ссылка --resolution")
    path = (root / value).resolve()
    if not _inside(root, path) or not path.is_file():
        raise WorkflowError(f"результат не найден внутри склада: {value}")
    rel = str(path.relative_to(root))
    if not rel.startswith(("raw/", "work/")):
        raise WorkflowError("результат должен лежать в raw/ или work/")
    return rel


def _scalar(value: str) -> str:
    if SAFE_SCALAR.fullmatch(value):
        return value
    # safe_dump скалярной строки добавляет отдельный маркер ``...``. Внутри
    # уже открытой YAML-шапки это начинает второй документ и ломает карточку.
    return "'" + value.replace("'", "''") + "'"


def update_frontmatter(path: Path, changes: dict[str, str]) -> None:
    """Меняет только названные поля, не переформатируя человеческий текст."""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise WorkflowError(f"{path}: нет шапки")
    end = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
    if end is None:
        raise WorkflowError(f"{path}: шапка не закрыта")

    found: set[str] = set()
    for i in range(1, end):
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*):", lines[i])
        if not match or match.group(1) not in changes:
            continue
        field = match.group(1)
        ending = "\n" if lines[i].endswith("\n") else ""
        lines[i] = f"{field}: {_scalar(changes[field])}{ending}"
        found.add(field)

    missing = [field for field in ("started", "resolved", "resolution")
               if field in changes and field not in found]
    missing += [field for field in changes if field not in found and field not in missing
                and field != "status"]
    if missing:
        after_status = next(
            (i + 1 for i in range(1, end) if re.match(r"^status:", lines[i])),
            end,
        )
        additions = [f"{field}: {_scalar(changes[field])}\n" for field in missing]
        lines[after_status:after_status] = additions

    folder = path.parent
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=folder)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write("".join(lines))
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def drop_frontmatter(path: Path, fields: list[str]) -> None:
    """Убирает названные поля из шапки. Связь, которой больше нет, не хранится."""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise WorkflowError(f"{path}: нет шапки")
    end = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
    if end is None:
        raise WorkflowError(f"{path}: шапка не закрыта")

    drop = set(fields)
    kept: list[str] = []
    for i, line in enumerate(lines):
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*):", line) if 1 <= i < end else None
        if match and match.group(1) in drop:
            continue
        kept.append(line)
    if len(kept) == len(lines):
        return

    folder = path.parent
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=folder)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write("".join(kept))
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def wip_count(root: Path, *, excluding: str = "") -> int:
    loaded = store.load(root, "work")
    if loaded.unreadable:
        raise WorkflowError(loaded.complain() or "work/ прочитан не полностью")
    return sum(1 for note in loaded.notes
               if note.type == "commitment"
               and (note.status == "in-progress"
                    or (note.status == "waiting" and bool(note.data.get("started"))))
               and note.rel != excluding)


def wip_limit(root: Path) -> int:
    try:
        conf = yaml.safe_load((root / "config" / "attention.yml").read_text(
            encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise WorkflowError(f"не прочитан config/attention.yml: {exc}") from exc
    return int((conf.get("tracker") or {}).get("wip_limit", 3))


def transition(root: Path, command: str, target: str, *, on: dt.date,
               resolution: str | None = None, reason: str = "",
               now: dt.datetime | None = None) -> str:
    root = root.resolve()
    path, data = resolve_target(root, target, commitment=True)
    current = str(data.get("status") or "")
    if current not in ALLOWED_FROM[command]:
        expected = ", ".join(sorted(ALLOWED_FROM[command]))
        raise WorkflowError(
            f"{target}: переход {command} недопустим из {current or 'без статуса'} "
            f"(ожидалось: {expected})"
        )

    rel = str(path.relative_to(root))
    changes: dict[str, str] = {}
    if command in {"take", "resume"}:
        limit = wip_limit(root)
        count = wip_count(root, excluding=rel)
        if count >= limit:
            raise WorkflowError(f"лимит работы выбран: {count} из {limit}")
        changes["status"] = "in-progress"
        if not data.get("started"):
            changes["started"] = on.isoformat()
    elif command == "wait":
        if not reason.strip():
            raise WorkflowError("для ожидания нужна причина --reason")
        changes["status"] = "waiting"
    elif command in {"finish", "cancel"}:
        if command == "cancel" and not reason.strip():
            raise WorkflowError("для отмены нужна причина --reason")
        changes["status"] = "resolved" if command == "finish" else "cancelled"
        changes["resolved"] = on.isoformat()
        changes["resolution"] = resolve_evidence(root, resolution)

    update_frontmatter(path, changes)
    row: list[object] = ["реакция", REACTIONS[command], rel]
    if reason.strip():
        row.append(reason)
    activity.append(root, row, now=now)
    if command in {"finish", "cancel"}:
        note = close_outside(root, rel, data)
        if note:
            print(note)
    return rel


def finish_from_external(root: Path, target: str, *, on: dt.date,
                         resolution: str, source: str) -> str:
    """Записывает явную отметку человека во внешнем канале как завершение.

    Это не самостоятельное решение системы: галочка уже поставлена человеком.
    Отдельный вход raw служит доказательством результата и не даёт повторному
    проходу закрыть обязательство второй раз.
    """
    root = root.resolve()
    path, data = resolve_target(root, target, commitment=True)
    if str(data.get("status") or "") not in {"open", "in-progress", "waiting"}:
        return str(path.relative_to(root))
    proof = resolve_evidence(root, resolution)
    rel = str(path.relative_to(root))
    update_frontmatter(path, {
        "status": "resolved",
        "resolved": on.isoformat(),
        "resolution": proof,
    })
    activity.append(root, [
        "реакция", "завершено", rel, f"подтверждено в {source}",
    ])
    return rel


def external_status_targets(data: dict) -> dict[str, str]:
    """Только интерфейсы, где дело действительно имеет внешний статус.

    Календарь сюда намеренно не входит: событие даёт контекст времени, но не
    является второй карточкой обязательства. Состоявшуюся встречу подтверждает
    новое событие в raw/, а не правка календарной записи задним числом.
    """
    return {
        channel: str(data.get(channel) or "")
        for channel in EXTERNAL_STATUS_CHANNELS
        if data.get(channel)
    }


def close_outside(root: Path, rel: str, data: dict) -> str:
    """Закрыл в складе — закрыть и там, где человек это видит.

    Дело живёт в складе и приложении задач. Календарь в этот список не входит:
    он показывает занятость и события, а не состояние обязательства. Закрытое
    в складе и открытое в TickTick — обещание, которое система продолжает
    показывать человеку после выполнения.

    Внешняя часть не имеет права уронить переход: обязательство уже закрыто в
    складе, и отсутствие сети этого не отменяет. Поэтому неудача возвращается
    строкой, а не исключением.
    """
    external = external_status_targets(data).get("ticktick", "")
    if not external:
        return ""
    try:
        import ticktick
        project = ticktick.project_id(root)
        ticktick.call(f"/project/{project}/task/{external}/complete", method="POST")
    except Exception as exc:                                  # noqa: BLE001
        return (f"в приложении задача {external} осталась открытой: {exc}. "
                "Закрыть вручную или повторить обмен позже.")
    return f"в приложении задача закрыта тоже: {external}"


def feedback(root: Path, command: str, target: str, *, reason: str,
             until: dt.date | None = None,
             now: dt.datetime | None = None) -> str:
    """Отклик на показанную строку. `defer` умеет назвать день возврата.

    Отложенное без дня возврата не всплывает само: «вернись в понедельник»
    записать было некуда, и половина ответа человека терялась. Поэтому у
    `defer` есть `--until`: он не меняет статус позиции, но ставит поле
    `review`, по которому экран вернёт строку в назначенный день.
    """
    root = root.resolve()
    if not reason.strip():
        raise WorkflowError(f"для {command} нужна причина --reason")
    if until and command != "defer":
        raise WorkflowError("день возврата задаётся только при defer")
    path, _ = resolve_target(root, target, commitment=False)
    rel = str(path.relative_to(root))
    if until:
        update_frontmatter(path, {"review": until.isoformat()})
    row: list[object] = ["реакция", REACTIONS[command], rel, reason]
    if until:
        row.append(f"вернуться {until:%d.%m.%Y}")
    activity.append(root, row, now=now)
    return rel


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="явный рабочий цикл обязательств")
    parser.add_argument("command", choices=sorted(STATE_ACTIONS | FEEDBACK_ACTIONS))
    parser.add_argument("target", help="путь к позиции относительно корня склада")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--on", type=dt.date.fromisoformat, default=dt.date.today(),
                        help="дата перехода; по умолчанию сегодня")
    parser.add_argument("--resolution", help="путь к результату для finish/cancel")
    parser.add_argument("--reason", default="", help="причина ожидания или реакции")
    parser.add_argument("--until", type=dt.date.fromisoformat,
                        help="день возврата для defer: «вернись к этому в понедельник»")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        if args.command in STATE_ACTIONS:
            rel = transition(root, args.command, args.target, on=args.on,
                             resolution=args.resolution, reason=args.reason)
        else:
            rel = feedback(root, args.command, args.target, reason=args.reason,
                           until=args.until)
    except WorkflowError as exc:
        print(f"не выполнено: {exc}")
        return 2
    tail = f", вернуться {args.until:%d.%m.%Y}" if args.until else ""
    print(f"{REACTIONS[args.command]}: {rel}{tail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
