#!/usr/bin/env python3
"""Единственный способ прочитать склад.

Зачем. Раньше каждый инструмент сам решал, что считать заметкой: `attention`,
`interview` и `lint` имели три разных разбора шапки и три разных правила пропуска.
Заметка с повреждённой шапкой **молча исчезала** из утреннего экрана — то есть
обязательство существовало, а система говорила «сигналов нет». Это тот же класс
отказа, что весь список в docs/known-failure-modes.md: работает, рапортует успех,
данные теряются.

Правило: потребитель либо получает разобранную заметку, либо узнаёт, что файл не
прочитан. Тихого третьего варианта нет.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

SORTS = ("raw", "work", "wiki")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass
class Note:
    path: Path
    rel: str
    data: dict
    container: str = ""
    mode: str = ""

    @property
    def type(self) -> str:
        return str(self.data.get("type", ""))

    @property
    def status(self) -> str:
        return str(self.data.get("status", ""))

    @property
    def title(self) -> str:
        return str(self.data.get("title") or self.data.get("name") or self.path.stem)

    @property
    def key(self) -> str:
        return str(self.data.get("key") or "")

    def date_field(self, *names: str) -> dt.date | None:
        for name in names:
            match = DATE.search(str(self.data.get(name) or ""))
            if match:
                try:
                    return dt.date.fromisoformat(match.group(0))
                except ValueError:
                    continue
        return None


@dataclass
class Store:
    """Прочитанный склад: заметки и — обязательно — список непрочитанных файлов."""

    notes: list[Note] = field(default_factory=list)
    unreadable: list[tuple[str, str]] = field(default_factory=list)

    @property
    def by_rel(self) -> dict[str, Note]:
        return {n.rel: n for n in self.notes}

    def sort(self, name: str) -> list[Note]:
        return [n for n in self.notes if n.rel.startswith(name + "/")]

    def of_type(self, *types: str) -> list[Note]:
        return [n for n in self.notes if n.type in types]

    def inside(self, container: str) -> list[Note]:
        prefix = container.rstrip("/") + "/"
        return [n for n in self.notes if n.rel.startswith(prefix)]

    def complain(self) -> str | None:
        """Строка для человека, если что-то не прочиталось. None — всё чисто."""
        if not self.unreadable:
            return None
        head = f"НЕ ПРОЧИТАНО {len(self.unreadable)} файлов — они выпали из расчёта:"
        return head + "".join(f"\n  · {rel}: {why}" for rel, why in self.unreadable)


def parse(text: str) -> tuple[dict | None, str | None]:
    """Шапка или причина, по которой её нет. Пустая шапка — тоже причина."""
    if not text.startswith("---"):
        return None, "нет шапки"
    # Закрывающий разделитель — отдельная строка. Поиск "\n---" принимал
    # "\n---oops" и молча считал шапку закрытой.
    end = -1
    for match in re.finditer(r"^---\s*$", text[3:], flags=re.MULTILINE):
        end = match.start() + 3
        break
    if end == -1:
        return None, "шапка открыта, но не закрыта"
    try:
        data = yaml.safe_load(text[3:end])
    except (yaml.YAMLError, ValueError) as exc:
        # ValueError — не опечатка в перехвате. PyYAML разбирает шапку успешно и
        # падает уже на типизации значения: `due: 2026-02-31` даёт голый
        # ValueError из конструктора дат, мимо всей иерархии YAMLError. Без этого
        # одна невозможная дата роняла lint, attention и tracker разом —
        # трейсбеком вместо причины, то есть тем самым «тихим третьим вариантом»,
        # которого по документации этого файла не бывает.
        return None, f"шапка не разбирается: {str(exc).splitlines()[0]}"
    if data is None:
        return None, "шапка пуста"
    if not isinstance(data, dict):
        return None, f"шапка не словарь, а {type(data).__name__}"
    return data, None


def attach_containers(notes: list[Note]) -> None:
    """Позиция знает свой контейнер и его режим: idle не стареет."""
    holders = {str(Path(n.rel).parent): n for n in notes
               if n.type in {"client", "program", "self"}}
    for note in notes:
        for folder, holder in holders.items():
            if note.rel.startswith(folder + "/") or note.rel == holder.rel:
                note.container = holder.title
                note.mode = str(holder.data.get("mode", ""))
                break


def load(root: Path, *sorts: str, skip_archive: bool = True) -> Store:
    """Прочитать указанные корни. Непрочитанное попадает в `unreadable`, не в тишину."""
    store = Store()
    for sort in (sorts or SORTS):
        base = root / sort
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.md")):
            rel = str(path.relative_to(root))
            if skip_archive and rel.startswith("work/archive/"):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                store.unreadable.append((rel, f"{type(exc).__name__}"))
                continue
            data, why = parse(text)
            if data is None:
                store.unreadable.append((rel, why or "неизвестно"))
                continue
            store.notes.append(Note(path=path, rel=rel, data=data))
    attach_containers(store.notes)
    return store
