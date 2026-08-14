#!/usr/bin/env python3
"""Досье к интервью: то, что можно собрать машинально, чтобы сценарий писался не с нуля.

Разделение труда: механическое — здесь, суждение — у роли `agents/custdev.md`.
Код не придумывает вопросы. Он отвечает на то, что проверяемо:

  что мы уже знаем по этому контейнеру и **о чём поэтому не спрашивать**;
  какие вопросы открыты и какие узкие места держат работу;
  какие коды уже встречались — чтобы за столом отличать новое от повторного;
  кого уже опрашивали и когда;
  какие гипотезы ждут проверки и какой риск они снимают.

Повторный вопрос тратит время собеседника и роняет доверие: он понимает, что его
не слушали в прошлый раз. Поэтому раздел «не спрашивать» — первый, а не последний.
"""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

from store import Note, load

EVENT_TYPES = {"interview", "observation", "meeting", "digest"}


def raw_notes(root: Path, complaints: list[str] | None = None) -> list[Note]:
    loaded = load(root, "raw")
    if complaints is not None and loaded.complain():
        complaints.append(loaded.complain())
    return [n for n in loaded.notes if n.type in EVENT_TYPES]


def container_of(note: Note, by_rel: dict[str, Note] | None = None) -> str:
    """Контейнер события — свой или унаследованный по ссылке на источник.

    Наблюдение и разбор не несут поля `container`: они ссылаются на источник, а
    контейнер знает он. Без этого досье считало наблюдения чужими и заявляло
    «первое интервью, кодов нет» при восьми записанных наблюдениях — ровно та
    тихая ложь, из-за которой известное принимают за новое.
    """
    own = str(note.data.get("container") or "")
    if own:
        return own
    source = str(note.data.get("source") or "")
    if by_rel and source in by_rel:
        return container_of(by_rel[source], None)
    return ""


def brief(root: Path, container: str, hypothesis: str | None) -> list[str]:
    complaints: list[str] = []
    store = load(root, "work")
    work = store.notes
    if store.complain():
        complaints.append(store.complain())
    inside = store.inside(container)
    raw = raw_notes(root, complaints)
    by_rel = {n.rel: n for n in raw}
    events = [n for n in raw if container_of(n, by_rel) == container.rstrip("/")]

    out: list[str] = list(complaints)
    holder = next((n for n in work if n.rel == f"{container.rstrip('/')}/client.md"
                   or n.rel == f"{container.rstrip('/')}/program.md"), None)
    out.append(f"# Досье к интервью — {holder.title if holder else container}")

    # 1. О чём НЕ спрашивать
    answered = [n for n in inside if n.type == "question" and n.status == "resolved"]
    decided = [n for n in inside if n.type == "decision" and n.status == "accepted"]
    out.append("\n## Не спрашивать — ответ уже есть")
    if answered or decided:
        for n in answered:
            out.append(f"- {n.title} — закрыт")
        for n in decided:
            out.append(f"- решено: {n.title}")
    else:
        out.append("- (пока нечего: закрытых вопросов и решений по контейнеру нет)")

    # 2. Что открыто
    open_q = [n for n in inside if n.type == "question" and n.status in {"open", "waiting"}]
    constraints = [n for n in inside if n.type == "constraint" and n.status == "open"]
    out.append("\n## Открыто — сюда целиться")
    for n in constraints:
        out.append(f"- УЗКОЕ МЕСТО ({n.data.get('leverage', '?')} плечо): {n.title}")
    for n in open_q:
        out.append(f"- вопрос: {n.title}")
    if not (open_q or constraints):
        out.append("- (открытых вопросов нет — режим порождающий, строим карту)")

    # 3. Риски: чего боимся заранее
    risks = [n for n in inside if n.type == "risk" and n.status == "open"]
    if risks:
        out.append("\n## Известные риски — проверить, подтверждаются ли")
        for n in risks:
            out.append(f"- {n.title}")

    # 4. Что проверяем
    out.append("\n## Что проверяем")
    if hypothesis:
        from store import parse
        data, why = parse((root / hypothesis).read_text(encoding="utf-8"))
        if data is None:
            out.append(f"ГИПОТЕЗА НЕ ПРОЧИТАНА ({hypothesis}): {why}")
            data = {}
        out.append(f"- гипотеза: {data.get('action', '?')}")
        out.append(f"  ожидаем: {data.get('expected', '?')}")
        out.append(f"  потому что: {data.get('rationale', '?')}")
        out.append(f"  риск: {data.get('risk_type', 'не указан')}; "
                   f"умирает если неверно: {data.get('kills_if_wrong', 'не указано')}")
        out.append("- режим: ПРОВЕРОЧНЫЙ. Цель — изменить статус гипотезы, а не понравиться ей.")
    else:
        hyps = [n for n in inside if n.type == "hypothesis" and n.status == "open"]
        for n in hyps:
            out.append(f"- ждёт проверки: {n.data.get('action', n.title)}")
        out.append("- режим: ПОРОЖДАЮЩИЙ. Цель — карта того, как устроено, а не подтверждение.")

    # 5. Насыщение и кого уже спрашивали
    observations = [n for n in events if n.type == "observation"]
    interviews = [n for n in events if n.type == "interview"]
    codes = Counter()
    for n in observations:
        for code in n.data.get("codes") or []:
            codes[str(code)] += 1
    out.append("\n## Насыщение")
    out.append(f"- интервью по контейнеру: {len(interviews)}; наблюдений: {len(observations)}")
    if codes:
        out.append("- уже встречавшиеся коды (новое отличаем от повторного):")
        for code, count in codes.most_common():
            out.append(f"    {code} × {count}")
    else:
        out.append("- кодов ещё нет: это первое интервью, инсайтом будет почти каждый пункт")
    if interviews:
        out.append("- уже опрошены:")
        for n in sorted(interviews, key=lambda n: str(n.data.get("date"))):
            out.append(f"    {n.data.get('date')} · {n.data.get('person', '?')}")

    # 6. Люди контейнера
    # Только люди этого контейнера плюс общие: показывать всех подряд — шум
    mentioned = "\n".join(n.path.read_text(encoding="utf-8") for n in inside)
    people = [n for n in work if n.type == "person"
              and (n.rel in mentioned or str(n.data.get("org", "")) in container
                   or n.rel.startswith("work/people/"))]
    if people:
        out.append("\n## Люди")
        for n in people:
            mark = "" if n.rel in mentioned else " (не упомянут в этом контейнере)"
            out.append(f"- {n.title} — {n.data.get('role', 'роль не указана')}{mark}")

    out.append("\n## Дальше — за ролью")
    out.append("Сценарий пишет `agents/custdev.md`: открывающий запрос «расскажите про "
               "последний раз», ветки по четырём силам, ловушки, и чем должен "
               "закончиться разговор (следующий шаг с датой, а не «интересно»).")
    return out


def codes(root: Path) -> list[str]:
    """Своды кодов. Непрочитанное называется так же, как в досье: иначе список
    кодов молча считался полным."""
    counter: Counter[str] = Counter()
    complaints: list[str] = []
    for note in raw_notes(root, complaints):
        for code in note.data.get("codes") or []:
            counter[str(code)] += 1
    if not counter:
        return complaints + ["Кодов нет: наблюдений ещё не заведено."]
    return complaints + [f"{code} × {count}" for code, count in counter.most_common()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Досье к интервью")
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("brief")
    b.add_argument("--container", required=True, help="напр. work/clients/alfa")
    b.add_argument("--hypothesis", help="путь к гипотезе, если режим проверочный")
    b.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    c = sub.add_parser("codes")
    c.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    root = args.root.resolve()

    lines = (brief(root, args.container, getattr(args, "hypothesis", None))
             if args.command == "brief" else codes(root))
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
