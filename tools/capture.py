#!/usr/bin/env python3
"""Приём внешних записей в зону приёма из любого источника.

Коннекторы Granola, TickTick и календаря знают свои API и написаны отдельно.
Этот вход нужен для всего остального: диктофон, чужой MCP-сервер, выгрузка из
переписки, экспорт документов. Тот, кто умеет достать данные, приводит их к
общему виду и передаёт сюда — правила приёма (что считается повтором, куда
кладётся файл, какие поля обязательны) остаются в одном месте, а не
переписываются заново в каждом новом коннекторе.

Вход — JSON: один объект или список объектов, файлом или на стандартном входе.

    {"source": "plaud", "external_id": "rec_8123", "date": "2026-08-14",
     "title": "Планёрка по маршрутам", "body": "## Расшифровка\\n\\n…"}

Необязательно: ``revision`` — версия внешнего объекта (изменился объект —
изменилась версия, и это новое событие, а не правка старого); ``aliases`` —
другие идентификаторы того же объекта; ``fields`` — любые дополнительные поля
шапки, например ``container`` или ``participants``.

Повтор — пара ``source`` + ``source_ref`` — отбрасывается молча. Запуск дважды
подряд не создаёт второй копии, и это главное свойство: расписание, дёрнувшее
приём три раза в сутки, не имеет права размножить один и тот же разговор.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import intake

REQUIRED = ("source", "external_id", "date", "title")


def parse(payload: object) -> list[intake.Capture]:
    """Разбор входа. Неполная запись называет себя, а не пропускается молча."""
    items = payload if isinstance(payload, list) else [payload]
    captures: list[intake.Capture] = []
    for number, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise intake.IntakeError(f"запись {number}: ожидался объект, пришло {type(item).__name__}")
        missing = [name for name in REQUIRED if not str(item.get(name) or "").strip()]
        if missing:
            raise intake.IntakeError(f"запись {number}: нет обязательных полей: {', '.join(missing)}")
        aliases = item.get("aliases") or []
        if not isinstance(aliases, list):
            raise intake.IntakeError(f"запись {number}: aliases — список, пришло {type(aliases).__name__}")
        fields = item.get("fields") or {}
        if not isinstance(fields, dict):
            raise intake.IntakeError(f"запись {number}: fields — объект, пришло {type(fields).__name__}")
        captures.append(intake.Capture(
            source=str(item["source"]).strip(),
            external_id=str(item["external_id"]).strip(),
            date=str(item["date"]).strip(),
            title=str(item["title"]).strip(),
            body=str(item.get("body") or ""),
            revision=str(item.get("revision") or ""),
            aliases=tuple(str(alias) for alias in aliases),
            fields=dict(fields),
        ))
    return captures


def read_payload(source: Path | None) -> object:
    raw = source.read_text(encoding="utf-8") if source else sys.stdin.read()
    if not raw.strip():
        raise intake.IntakeError("вход пуст: нечего принимать")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as error:
        raise intake.IntakeError(f"вход не разобран как JSON: {error}") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="приём внешних записей в raw/inbox")
    parser.add_argument("--file", type=Path, help="JSON-файл; без него читается stdin")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="корень склада")
    parser.add_argument("--dry-run", action="store_true",
                        help="сказать, что было бы принято, и ничего не записывать")
    args = parser.parse_args(argv)

    try:
        captures = parse(read_payload(args.file))
    except intake.IntakeError as error:
        print(f"приём не состоялся: {error}", file=sys.stderr)
        return 2

    if args.dry_run:
        known = {}
        new = 0
        for capture in captures:
            seen = known.setdefault(
                capture.source, intake.known_signatures(args.root, capture.source))
            if capture.signatures & seen:
                continue
            seen.update(capture.signatures)
            new += 1
        print(f"пришло {len(captures)}, новых {new}, повторов {len(captures) - new}")
        return 0

    try:
        saved, skipped = intake.save_many(args.root, captures)
    except intake.IntakeError as error:
        print(f"приём не состоялся: {error}", file=sys.stderr)
        return 2

    for path in saved:
        print(f"принято · {path.relative_to(args.root.resolve())}")
    print(f"приём: новых {len(saved)}, повторов {skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
