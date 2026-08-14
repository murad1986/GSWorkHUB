#!/usr/bin/env python3
"""Общий повторобезопасный приём внешних событий в ``raw/inbox``.

Коннектор приносит факт, а не позицию работы. Поэтому календарь, TickTick и
следующие источники сначала сохраняют дословный снимок здесь; обязательство,
решение или срок появляются в ``work/`` только после разбора.

Повтор одной версии отбрасывается. Изменение внешнего объекта — новое событие:
у него тот же ``external_id``, но другая ``external_revision`` и, следовательно,
другой ``source_ref``. Уже записанный raw-файл никогда не открывается на запись.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

import store
import yaml


class IntakeError(RuntimeError):
    """Приём не состоялся полностью; молчаливой потери нет."""


def _normal(value: object) -> str:
    return " ".join(str(value or "").split()).casefold()


def signature(identity: str, revision: str = "") -> str:
    identity = _normal(identity)
    revision = _normal(revision)
    return f"{identity}@{revision}" if revision else identity


@dataclass(frozen=True)
class Capture:
    source: str
    external_id: str
    date: str
    title: str
    body: str
    revision: str = ""
    aliases: tuple[str, ...] = ()
    fields: dict[str, object] = field(default_factory=dict)

    @property
    def source_ref(self) -> str:
        return signature(self.external_id, self.revision)

    @property
    def signatures(self) -> set[str]:
        identities = (self.external_id, *self.aliases)
        return {signature(identity, self.revision)
                for identity in identities if _normal(identity)}


def note_signatures(note: store.Note) -> set[str]:
    """Все ключи одной сохранённой версии, включая альтернативный id API."""
    revision = str(note.data.get("external_revision") or "")
    identities: list[object] = [note.data.get("external_id")]
    aliases = note.data.get("source_aliases") or []
    if isinstance(aliases, list):
        identities.extend(aliases)
    out = {signature(identity, revision) for identity in identities
           if _normal(identity)}
    ref = _normal(note.data.get("source_ref"))
    if ref:
        out.add(ref)
    return out


def known_signatures(root: Path, source: str) -> set[str]:
    loaded = store.load(root, "raw")
    if loaded.unreadable:
        raise IntakeError(loaded.complain() or "raw/ прочитан не полностью")
    wanted = _normal(source)
    known: set[str] = set()
    for note in loaded.notes:
        if _normal(note.data.get("source")) == wanted:
            known |= note_signatures(note)
    return known


def is_known(root: Path, capture: Capture) -> bool:
    return bool(capture.signatures & known_signatures(root, capture.source))


def _safe(value: str) -> str:
    clean = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return clean[:36].rstrip("-") or "event"


def target_path(root: Path, capture: Capture) -> Path:
    digest = hashlib.sha1(
        f"{capture.source}\0{capture.source_ref}".encode("utf-8")
    ).hexdigest()[:10]
    stem = f"{capture.date}-{_safe(capture.source)}-{_safe(capture.external_id)}-{digest}"
    return root / "raw" / "inbox" / f"{stem}.md"


def document(capture: Capture) -> str:
    front: dict[str, object] = {
        "type": "source",
        "date": capture.date,
        "title": capture.title,
        "source": capture.source,
        "source_ref": capture.source_ref,
        "external_id": capture.external_id,
    }
    if capture.revision:
        front["external_revision"] = capture.revision
    aliases = [alias for alias in capture.aliases if _normal(alias)]
    if aliases:
        front["source_aliases"] = aliases
    front.update(capture.fields)
    heading = capture.title.strip() or "Внешнее событие"
    body = capture.body.rstrip() or "Источник не передал содержимое."
    return ("---\n" + yaml.safe_dump(front, allow_unicode=True, sort_keys=False)
            + f"---\n\n# {heading}\n\n{body}\n")


def save(root: Path, capture: Capture,
         *, known: set[str] | None = None) -> Path | None:
    """Сохраняет новую версию один раз; существующее никогда не переписывает."""
    root = root.resolve()
    seen = known if known is not None else known_signatures(root, capture.source)
    if capture.signatures & seen:
        return None
    path = target_path(root, capture)
    content = document(capture)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(content)
    except FileExistsError:
        if path.read_text(encoding="utf-8") == content:
            seen.update(capture.signatures)
            return None
        raise IntakeError(
            f"путь {path.relative_to(root)} занят другим содержимым"
        ) from None
    if path.read_text(encoding="utf-8") != content:
        raise IntakeError(f"запись не перечиталась: {path.relative_to(root)}")
    seen.update(capture.signatures)
    return path


def save_many(root: Path, captures: list[Capture]) -> tuple[list[Path], int]:
    """Общий набор известных версий делает один запуск линейным, не квадратичным."""
    by_source: dict[str, set[str]] = {}
    saved: list[Path] = []
    skipped = 0
    for capture in captures:
        source = _normal(capture.source)
        if source not in by_source:
            by_source[source] = known_signatures(root, capture.source)
        known = by_source[source]
        path = save(root, capture, known=known)
        if path is None:
            skipped += 1
        else:
            saved.append(path)
    return saved, skipped
