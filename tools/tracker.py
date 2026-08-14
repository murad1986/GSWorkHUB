#!/usr/bin/env python3
"""Трекер: что взять сейчас, сколько уже в работе, что похоже на сделанное.

Гибкость здесь — не набор настроек, а три свойства:
  ничего не ломается от пропуска (уровень обязательности вместо срока);
  ёмкость дня — вход, а не допущение;
  выбор остаётся за человеком в момент чтения — система сужает, не назначает.

Механики и происхождение:
  «приоритет не хранится, возникает при чтении» — Final Version Марка Форстера:
    один список без пометок, выбор по психологической готовности;
  «сейчас / дальше / когда-нибудь» вместо дат — Now-Next-Later Дженны Бастоу:
    обещание только «сейчас», остальное направление. Лекарство от 41% пунктов,
    которые не выполняются никогда;
  «когда — тогда» — implementation intentions, d = 0.65 по мета-анализу
    94 исследований: формулировка «если Y, то X» примерно вдвое повышает
    исполнение по сравнению с намерением без зацепки;
  лимит незавершённого — Kanban; квота на себя — «сначала заплати себе»;
  подбор сделанного — потому что только 15% выполненного за день начиналось как
    пункт списка: трекер, который не подбирает факт, описывает вымышленную жизнь.
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import subprocess
from pathlib import Path

import capacity as capacity_module
import yaml
from attention import parse_due
from store import Note, load

UNRESOLVED = {"open", "in-progress", "waiting"}
LEVELS = ("now", "next", "later")


def config(root: Path) -> dict:
    return yaml.safe_load((root / "config" / "attention.yml").read_text(encoding="utf-8")) or {}


def commitments(notes: list[Note]) -> list[Note]:
    return [n for n in notes if n.type == "commitment"]


def counts_as_wip(note: Note) -> bool:
    """Ожидание после старта остаётся незавершённой работой, а не освобождает слот."""
    return note.status == "in-progress" or (
        note.status == "waiting" and bool(note.data.get("started"))
    )


def level_of(note: Note) -> str:
    value = str(note.data.get("level") or "")
    return value if value in LEVELS else "next"


def readiness(note: Note, today: dt.date) -> tuple[int, str]:
    """Насколько готово к взятию. Не приоритет — зацепка: есть ли «когда — тогда»."""
    when_then = note.data.get("when_then") or {}
    due = parse_due(note)
    if isinstance(when_then, dict) and when_then.get("cue") and when_then.get("action"):
        if due and due <= today:
            return 3, "зацепка есть, срок прошёл"
        return 2, "зацепка есть"
    if due and due <= today:
        return 1, "срок прошёл, но зацепки нет — сформулируй «когда — тогда»"
    return 0, "нет зацепки: непонятно, в какой момент это начнётся"


def candidate_score(note: Note, today: dt.date, conf: dict) -> tuple[int, str]:
    """Порядок чтения кандидатов: обещанный срок важнее удобства начать."""
    due = parse_due(note)
    soon = int(conf.get("due_soon_days", 3))
    if due:
        delta = (due - today).days
        if delta < 0:
            return 1000 + abs(delta), f"просрочено на {abs(delta)} дн."
        if delta == 0:
            return 1000, "срок сегодня"
        if delta <= soon:
            return 900 - delta, f"срок {due:%d.%m}"
    ready, why = readiness(note, today)
    return 100 + ready, why


def status(notes: list[Note], conf: dict) -> list[str]:
    tracker = conf.get("tracker") or {}
    limit = int(tracker.get("wip_limit", 3))
    in_work = [n for n in commitments(notes) if counts_as_wip(n)]
    mine = [n for n in in_work if n.rel.startswith("work/me/")]
    out = [f"В работе {len(in_work)} из {limit}."]
    if len(in_work) >= limit:
        out.append("Лимит выбран — новое не берём, пока что-то не закроется или не будет отменено.")
    if tracker.get("self_slot") and not mine:
        out.append("Место за собой свободно и не занято: одно из трёх — всегда твоё.")
    for note in in_work:
        tail = " — ждёт ответа" if note.status == "waiting" else ""
        out.append(f"  · {note.container} · {note.title}{tail}")
    waiting = [n for n in commitments(notes)
               if n.status == "waiting" and not counts_as_wip(n)]
    for note in waiting:
        out.append(f"  ждёт ответа: {note.container} · {note.title}")
    return out


def pick(notes: list[Note], conf: dict, today: dt.date, capacity: str) -> list[str]:
    tracker = conf.get("tracker") or {}
    how_many = int((conf.get("capacity") or {}).get(capacity, tracker.get("candidates", 3)))
    limit = int(tracker.get("wip_limit", 3))
    in_work = [n for n in commitments(notes) if counts_as_wip(n)]
    if len(in_work) >= limit:
        return [f"Ничего не предлагаю: в работе уже {len(in_work)} из {limit}.",
                "Сначала закрой или отмени — набирать поверх лимита значит не закончить ничего."]

    pool = [n for n in commitments(notes)
            if n.status == "open" and level_of(n) == "now"]
    if not pool:
        return ["В «сейчас» пусто. Это нормально: значит нет обещаний с ближним горизонтом.",
                "Посмотри «дальше» — возможно, что-то пора поднять."]

    scored = sorted(pool, key=lambda n: (-candidate_score(n, today, conf)[0], n.title))
    shown = min(how_many, len(scored))
    out = [f"Ёмкость {capacity} — предлагаю {shown}. Выбирай по готовности, не по порядку:"]
    for note in scored[:how_many]:
        _, why = candidate_score(note, today, conf)
        when_then = note.data.get("when_then") or {}
        out.append(f"  · {note.container} · {note.title}")
        if isinstance(when_then, dict) and when_then.get("cue"):
            out.append(f"      когда {when_then['cue']} — {when_then.get('action', '')}")
        else:
            out.append(f"      {why}")
    rest = len(scored) - how_many
    if rest > 0:
        out.append(f"  ещё {rest} в «сейчас» — не показываю, чтобы не размывать выбор")
    return out


class GitUnavailable(RuntimeError):
    """История недоступна. Отличается от «изменений не было»."""


def recent_changes(root: Path, days: int) -> dict[str, dt.date]:
    """Что менялось за окно — из истории git, отдельной слежки не нужно."""
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    result = subprocess.run(
        ["git", "-C", str(root), "log", f"--since={since}", "--format=%x00%cI", "--name-only", "--", "work", "raw"],
        capture_output=True, text=True, check=False,
    )
    latest: dict[str, dt.date] = {}
    stamp: dt.date | None = None
    if result.returncode:
        # Та же тихая ложь, что чинили в самоанализе: «git не ответил» выдавалось
        # за «изменений не было», и подбор сделанного молча говорил «не нашлось»
        raise GitUnavailable(result.stderr.strip() or "git недоступен")
    for line in result.stdout.splitlines():
        if line.startswith("\x00"):
            stamp = dt.datetime.fromisoformat(line[1:]).date()
        elif line.strip() and stamp:
            latest[line.strip()] = max(latest.get(line.strip(), stamp), stamp)
    return latest


def event_date(root: Path, rel: str) -> dt.date | None:
    """Дата из имени события. Форма даты — ещё не существующий день: `2026-02-31`
    проходит по образцу и роняет разбор."""
    match = re.search(r"(\d{4}-\d{2}-\d{2})", Path(rel).name)
    if not match:
        return None
    try:
        return dt.date.fromisoformat(match.group(1))
    except ValueError:
        return None


def pickup(root: Path, notes: list[Note], conf: dict, days: int) -> list[str]:
    """Похоже на сделанное. Не закрывает — спрашивает: захватить не значит подтвердить.

    Правка файла обязательства признаком выполнения НЕ считается: правка — это
    правка. Признак — новое событие, которое произошло позже источника
    обязательства и упоминает его контейнер. Первая версия считала признаком
    любую правку и выдала 100% ложных срабатываний на трёх обязательствах из трёх.
    """
    try:
        changes = recent_changes(root, days)
    except GitUnavailable as exc:
        return [f"Подбор сделанного невозможен: {exc}.",
                "Это не «ничего не нашлось» — история недоступна."]
    # История git знает и об удалённых файлах. Удалённое событие не может быть
    # доказательством: его больше нет. Раньше подбор читал его с диска и падал —
    # первая же настоящая уборка склада уронила `make track`.
    events = {p: w for p, w in changes.items()
              if p.startswith(("raw/meetings/", "raw/sources/", "raw/interviews/"))
              and (root / p).exists()}
    gone = sum(1 for p in changes
               if p.startswith(("raw/meetings/", "raw/sources/", "raw/interviews/"))
               and not (root / p).exists())

    out: list[str] = []
    for note in commitments(notes):
        if note.status not in UNRESOLVED:
            continue
        origin = str(note.data.get("origin") or "")
        since = event_date(root, origin) if origin else None
        folder = note.rel.rsplit("/commitments/", 1)[0].split("/")[-1]
        hints: list[str] = []
        for path in events:
            if path == origin:
                continue
            happened = event_date(root, path)
            if since and happened and happened <= since:
                continue
            try:
                text = (root / path).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                hints.append(f"событие не прочитано: {path} ({type(exc).__name__})")
                continue
            if folder in text:
                hints.append(f"событие позже источника: {path}")
        if hints:
            out.append(f"  · {note.container} · {note.title}")
            out.append(f"      {'; '.join(hints)}")
            out.append("      это было оно? закрыть со ссылкой на результат / оставить открытым")
    tail = ([f"  ({gone} событий из истории больше нет на диске — они не в счёт)"]
            if gone else [])
    if not out:
        return [f"За {days} дн. ничего похожего на выполненное не нашлось."] + tail
    return ([f"Похоже на сделанное (окно {days} дн.). Ничего не закрываю — решаешь ты:"]
            + out + tail)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Трекер")
    parser.add_argument("command", choices=["status", "pick", "pickup", "all"], default="all", nargs="?")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--capacity", choices=["full", "half", "low", "auto"], default="auto",
                        help="auto — спросить данные о восстановлении вместо ручного флага")
    parser.add_argument("--today", type=dt.date.fromisoformat, default=dt.date.today())
    args = parser.parse_args(argv)
    root = args.root.resolve()

    conf = config(root)
    store = load(root, "work")
    notes = store.notes
    complaint = store.complain()
    if complaint:
        print(complaint + "\n")

    cap = args.capacity
    blocks: list[list[str]] = []
    if cap == "auto":
        measured = capacity_module.measure(today=args.today)
        if measured.level == "unknown":
            # Неизвестное состояние не заменяется удобным. Прежняя версия брала
            # «полную» — то есть увеличивала нагрузку именно тогда, когда данных
            # о состоянии недостаточно. Это противоположно смыслу предохранителя.
            print(f"Ёмкость по данным неизвестна — {measured.reason}.\n"
                  "Ничего не предлагаю, пока не задашь сам: make track CAP=full|half|low")
            return 2
        cap = measured.level
        blocks.append([measured.render()])
    if args.command in ("status", "all"):
        blocks.append(status(notes, conf))
    if args.command in ("pick", "all"):
        blocks.append(pick(notes, conf, args.today, cap))
    if args.command in ("pickup", "all"):
        window = int((conf.get("tracker") or {}).get("pickup_window_days", 7))
        blocks.append(pickup(root, notes, conf, window))

    print("\n\n".join("\n".join(block) for block in blocks))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
