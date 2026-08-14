#!/usr/bin/env python3
"""Сборщик навигации: `index.md` в каждой значимой папке.

Зачем он нужен, если есть экран внимания. Экран отвечает на вопрос «что делать
сегодня» и потому обязан молчать: семь строк, остальное скрыто. Полная картина
контейнера там не помещается по устройству. Индекс — обратное: он ничего не
скрывает и ничего не советует, а просто показывает, что в папке лежит и в каком
оно состоянии. Один вид отвечает за срочность, другой за полноту; сливать их
значит потерять либо одно, либо другое.

Индекс **полностью производный** — как и любой вид, он пересчитывается целиком и
своего состояния не хранит. Поэтому он не может разойтись с реальностью, и
поэтому же править его руками бессмысленно.

`index.md` зарезервирован OKF наравне с `log.md`: это навигация о папке, а не
запись в ней. Отсюда единственное исключение из правила «`raw/` не
переписывается» — оно про события, а индекс событием не является.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
from collections import Counter
from pathlib import Path

from store import Note, load

SORTS = ("raw", "work", "wiki")
RESERVED = {"index.md", "log.md"}
# Что показывать в колонке «состояние»: у разных типов это разные поля
STATE = ("status", "mode", "level", "kind")


def state_of(note: Note) -> str:
    for field in STATE:
        value = note.data.get(field)
        if value:
            return str(value)
    return "—"


def folders(notes: list[Note]) -> dict[str, list[Note]]:
    """Папка → её собственные заметки. Вложенные не поднимаются: у них свой индекс."""
    out: dict[str, list[Note]] = {}
    for note in notes:
        if Path(note.rel).name in RESERVED:
            continue
        out.setdefault(str(Path(note.rel).parent), []).append(note)
    return out


def children(folder: str, all_folders: set[str]) -> list[str]:
    """Прямые подпапки — чтобы из индекса можно было спуститься, а не искать."""
    prefix = folder.rstrip("/") + "/"
    depth = len(Path(folder).parts) + 1
    return sorted({f for f in all_folders
                   if f.startswith(prefix) and len(Path(f).parts) == depth})


def pointing_at(note: Note, everything: list[Note]) -> list[Note]:
    """Кто указывает на эту заметку полем `process`.

    Связь хранится в одном месте — у того, кто зависит. Процесс не ведёт список
    своих вопросов и рисков: такой список разошёлся бы со складом на первой же
    новой записи. Тот же довод, по которому шаг знает свою цель, а не наоборот.
    """
    return sorted((n for n in everything if str(n.data.get("process") or "") == note.rel),
                  key=lambda n: (n.type, n.title))


def render(folder: str, notes: list[Note], subfolders: list[str],
           now: dt.datetime, everything: list[Note] | None = None) -> str:
    by_type = Counter(n.type for n in notes)
    summary = ", ".join(f"{kind} {count}" for kind, count in sorted(by_type.items()))
    if not summary:
        # «Пусто» о папке с четырьмя подпапками — ложь. Развилка так и называется.
        summary = (f"Развилка: своих заметок нет, подпапок {len(subfolders)}."
                   if subfolders else "Пусто.")

    rows = "\n".join(
        f"| [{n.title}]({Path(n.rel).name}) | {n.type} | {state_of(n)} |"
        for n in sorted(notes, key=lambda n: (n.type, n.title))
    )
    table = ("| Что | Тип | Состояние |\n|---|---|---|\n" + rows) if rows else \
            "Своих заметок нет."

    attached = ""
    for note in sorted(notes, key=lambda n: n.title):
        if note.type != "process":
            continue
        linked = pointing_at(note, everything or [])
        if not linked:
            continue
        by_type: dict[str, list[Note]] = {}
        for n in linked:
            by_type.setdefault(n.type, []).append(n)
        rows = []
        for kind in sorted(by_type):
            # Путь считается от папки оглавления до записи, а не собирается из
            # имени родителя. Прежняя сборка молча предполагала, что связанная
            # запись лежит в соседней папке того же контейнера, и ломалась на
            # первом же источнике из raw/: выходило ../inbox/… вместо подъёма
            # на четыре уровня.
            items = ", ".join(
                f"[{n.title}]({os.path.relpath(n.rel, folder)})" for n in by_type[kind])
            rows.append(f"| {kind} | {items} |")
        attached += (f"\n## Что относится к «{note.title}»\n\n"
                     f"Собрано по полю `process`, руками не ведётся.\n\n"
                     "| Тип | Записи |\n|---|---|\n" + "\n".join(rows) + "\n")

    down = ""
    if subfolders:
        links = "\n".join(f"- [{Path(f).name}]({Path(f).name}/index.md)" for f in subfolders)
        down = f"\n## Вглубь\n\n{links}\n"

    return f"""---
type: index
title: {Path(folder).name}
generated: true
generated_at: {now:%Y-%m-%dT%H:%M}
count: {len(notes)}
---

# {folder}

{summary or "Пусто."}
{down}{attached}
## Что здесь лежит

{table}

---

Вид: пересчитывается целиком командой `make index`, своего состояния не хранит.
Править руками бессмысленно — затрётся. Экран внимания отвечает на вопрос «что
делать», этот файл — на вопрос «что вообще есть».
"""


def build(root: Path, now: dt.datetime) -> tuple[list[str], list[str]]:
    """Пересобрать все индексы. Возвращает записанное и жалобы на непрочитанное."""
    store = load(root, *SORTS)
    complaints = [store.complain()] if store.complain() else []
    tree = folders(store.notes)
    # Дерево берётся с диска, а не из заметок. Пустая папка со старым индексом
    # иначе продолжала бы врать: своих заметок в ней нет, значит в разбор она не
    # попадала, значит её index.md никто не пересобирал — и он вечно обещал
    # сборщика, которого для неё не существует.
    all_folders = set(tree)
    for sort in SORTS:
        base = root / sort
        if not base.exists():
            continue
        all_folders.add(sort)
        for path in base.rglob("*"):
            if path.is_dir():
                all_folders.add(str(path.relative_to(root)))

    written: list[str] = []
    for folder in sorted(all_folders):
        if folder.startswith("work/archive"):
            continue   # чужая схема — не наш контракт, и 236 файлов в индексе не нужны
        target = root / folder / "index.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            render(folder, tree.get(folder, []), children(folder, all_folders), now,
                   store.notes),
            encoding="utf-8")
        written.append(str(target.relative_to(root)))
    return written, complaints


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Сборка навигации по складу")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    root = args.root.resolve()

    now = dt.datetime.now().replace(second=0, microsecond=0)
    if args.dry_run:
        store = load(root, *SORTS)
        tree = folders(store.notes)
        for folder in sorted(tree):
            if not folder.startswith("work/archive"):
                print(f"{folder}: {len(tree[folder])} заметок")
        return 0

    written, complaints = build(root, now)
    for line in complaints:
        print(line)
    print(f"индексы: пересобрано {len(written)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
