#!/usr/bin/env python3
"""Обмен между ноутбуком и сервером: один писатель в каждый момент.

Требование человека простое: конфликтов быть не должно, рассинхрона тоже.
Разрешать конфликты умно — не решение; надо, чтобы они не возникали.

Устройство держится на трёх правилах.

**Одна правда.** У сторон нет своих баз, обе работают с клонами одного
репозитория. Расхождение git показывает, а не прячет.

**Один писатель.** Ноутбук, пока за ним работают, отмечается в журнале
касанием. Сервер, видя свежую отметку, в `work/` не пишет вовсе: он кладёт
намерение отдельным файлом в `raw/intents/` и говорит человеку, что применит его
позже. Одновременной правки одной позиции при этом просто не бывает.

**Разные сорта — разные права.** Сервер всегда волен дописывать события в
`raw/` (новые файлы, конфликт невозможен) и строки в журнал (сливается union).
Виды он не публикует вовсе: они производные, пересобираются командой, и
сливать их построчно значит получить таблицу, которой не соответствует ни одно
состояние склада.

Что остаётся: одновременная правка *разных* позиций в `work/`. Git сливает её
без вмешательства. Одной и той же — не бывает по правилу выше.
"""

from __future__ import annotations

import argparse
import datetime as dt
import subprocess
from pathlib import Path

import activity

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "raw" / "log.md"
INTENTS = ROOT / "raw" / "intents"
# Сколько минут отметка ноутбука считается свежей. Меньше — сервер начнёт писать
# в work/ прямо во время работы человека; больше — намерения будут копиться,
# хотя ноутбук давно закрыт.
ACTIVE_MINUTES = 20
DERIVED = ("wiki/", "index.md")


class SyncError(RuntimeError):
    """Обмен не выполнен. Состояние склада не тронуто."""


def git(*args: str, check: bool = True) -> str:
    done = subprocess.run(["git", "-C", str(ROOT), *args],
                          capture_output=True, text=True, timeout=120)
    if check and done.returncode != 0:
        raise SyncError(f"git {' '.join(args)}: {done.stderr.strip()[:200]}")
    return done.stdout.strip()


def laptop_active(now: dt.datetime | None = None,
                  minutes: int = ACTIVE_MINUTES) -> bool:
    """Свежая отметка ноутбука в журнале означает: за складом сейчас работают."""
    now = now or dt.datetime.now()
    if not LOG.is_file():
        return False
    edge = now - dt.timedelta(minutes=minutes)
    root = LOG.parent.parent
    entries = activity.read(root, events={"присутствие"}, now=now)
    for entry in reversed(entries):
        if entry.part(0) == "ноутбук":
            return entry.stamp >= edge
    return False


def changed(kinds: tuple[str, ...] = ()) -> list[str]:
    files = [line[3:].strip().strip('"')
             for line in git("status", "--porcelain").splitlines() if line]
    if not kinds:
        return files
    return [f for f in files if any(k in f for k in kinds)]


def derived_only(files: list[str]) -> bool:
    return bool(files) and all(any(k in f for k in DERIVED) for f in files)


def rebuild_views() -> None:
    """Виды не сливают, а пересобирают: они производные от склада."""
    for target in ("attention", "index"):
        subprocess.run(["make", "-C", str(ROOT), "-s", target],
                       capture_output=True, text=True, timeout=300)


def pull() -> str:
    """Тянет чужие изменения. Виды при расхождении не сливаются, а пересобираются."""
    git("fetch", "--quiet", "origin")
    behind = git("rev-list", "--count", "HEAD..@{u}", check=False)
    if not behind or behind == "0":
        return "чужих изменений нет"
    ours = changed(DERIVED)
    if ours:
        git("checkout", "--", *ours)          # свои виды не жалко: пересоберём
    done = subprocess.run(["git", "-C", str(ROOT), "rebase", "origin/master"],
                          capture_output=True, text=True, timeout=180)
    if done.returncode != 0:
        stuck = changed()
        if derived_only(stuck):
            git("checkout", "--theirs", *stuck, check=False)
            git("add", *stuck)
            subprocess.run(["git", "-C", str(ROOT), "rebase", "--continue"],
                           capture_output=True, text=True,
                           env={"GIT_EDITOR": "true"}, timeout=120)
            rebuild_views()
            return f"подтянуто {behind}; виды пересобраны"
        git("rebase", "--abort", check=False)
        raise SyncError(
            "расхождение в позициях: " + ", ".join(stuck[:5])
            + ". Не трогаю: две правды об одном деле хуже остановки")
    rebuild_views()
    return f"подтянуто {behind}"


def push(message: str) -> str:
    files = changed()
    if not files:
        return "отправлять нечего"
    git("add", "-A")
    git("commit", "-q", "-m", message)
    for attempt in (1, 2, 3):
        done = subprocess.run(["git", "-C", str(ROOT), "push", "--quiet", "origin", "HEAD"],
                              capture_output=True, text=True, timeout=180)
        if done.returncode == 0:
            return f"отправлено файлов: {len(files)}"
        pull()          # кто-то опередил — подтянуть и повторить
        if attempt == 3:
            raise SyncError("не отправлено после трёх попыток: " + done.stderr[:200])
    return "не отправлено"


def park_intent(text: str, now: dt.datetime | None = None,
                root: Path | None = None) -> Path:
    """Кладёт намерение отдельным файлом: за складом сейчас работает человек.

    Отдельный файл, а не правка позиции, — потому что конфликт возможен только
    там, где двое трогают одно. Новый файл не трогает ничего.
    """
    now = now or dt.datetime.now()
    intents = (root.resolve() / "raw" / "intents") if root is not None else INTENTS
    intents.mkdir(parents=True, exist_ok=True)
    path = intents / f"{now:%Y-%m-%d-%H%M%S}-namerenie.md"
    path.write_text(
        "---\ntype: source\n"
        f"date: {now:%Y-%m-%d}\n"
        'title: "Отложенное намерение сервера"\n'
        "source: сервер\n"
        f"source_ref: intent-{now:%Y%m%d-%H%M%S}\n"
        "container: work/me\n---\n\n"
        "# Намерение, отложенное до свободного склада\n\n"
        "За складом в этот момент работал человек, поэтому позиция не менялась.\n\n"
        f"{text}\n",
        encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="обмен между ноутбуком и сервером")
    parser.add_argument("action", choices=["pull", "push", "status", "presence"])
    parser.add_argument("--message", default="обмен: изменения со стороны сервера")
    args = parser.parse_args(argv)

    try:
        if args.action == "presence":
            activity.append(ROOT, ["присутствие", "ноутбук"])
            print("отмечено: за складом работают")
            return 0
        if args.action == "status":
            print(f"ноутбук активен: {'да' if laptop_active() else 'нет'}")
            print(f"своих изменений: {len(changed())}")
            return 0
        print(pull() if args.action == "pull" else push(args.message))
    except SyncError as exc:
        print(f"обмен не выполнен: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
