#!/usr/bin/env python3
"""История советов роли: что предлагала и чем это кончилось.

Третий обязательный элемент роли по архитектуре (§9): без памяти о своих советах
роль повторит уже отвергнутое, и доверие кончится. Совет — такое же наблюдаемое
событие журнала, как показ и реакция, и живёт по тем же правилам: только
дописывание, никаких списков внутри карточек.

Совет всегда адресован позиции склада. Совет без адресата нельзя связать с
ответом человека, то есть нельзя проверить, — такой в журнал не пишется.
Тип, контекст и основание задаются явно: по тексту нельзя надёжно угадать,
повторяет ли роль прежний способ помощи или предлагает другой.
"""

from __future__ import annotations

import argparse
import datetime as dt
from dataclasses import dataclass
from pathlib import Path

import activity

ANSWER_WINDOW = 7
INEFFECTIVE_AFTER = 3
REJECTED = {"отклонено", "отменено", "поправлено"}
ACCEPTED = {"взято", "завершено"}


@dataclass(frozen=True)
class Advice:
    stamp: dt.datetime
    role: str
    target: str
    text: str
    kind: str = ""
    context: str = ""
    basis: str = ""
    answer: str = ""
    answer_stamp: dt.datetime | None = None
    completed_stamp: dt.datetime | None = None

    @property
    def outcome(self) -> str:
        if self.answer in REJECTED:
            return "отвергнут"
        if self.answer == "завершено" or self.completed_stamp is not None:
            return "принят и сделан"
        if self.answer in ACCEPTED:
            return "принят, но не сделан"
        if self.answer:
            return f"ответ: {self.answer}"
        return "ответа нет"


def roles(root: Path) -> list[str]:
    """Роли берутся из agents/, а не из списка в коде: новая роль — новый файл."""
    folder = root / "agents"
    return sorted(path.stem for path in folder.glob("*.md") if path.stem != "index")


def ineffective_kinds(history: list[Advice], *,
                      threshold: int = INEFFECTIVE_AFTER
                      ) -> dict[tuple[str, str], tuple[int, int]]:
    """Типы, которые принимали достаточно раз, но ни разу не довели до дела."""
    grouped: dict[tuple[str, str], list[Advice]] = {}
    for one in history:
        if one.kind and one.outcome in {"принят и сделан", "принят, но не сделан"}:
            grouped.setdefault((one.role, one.kind), []).append(one)
    out: dict[tuple[str, str], tuple[int, int]] = {}
    for key, accepted in grouped.items():
        completed = sum(one.outcome == "принят и сделан" for one in accepted)
        if len(accepted) >= threshold and completed == 0:
            out[key] = (len(accepted), completed)
    return out


def give(root: Path, role: str, target: str, text: str, *, advice_type: str = "",
         context: str = "", basis: str = "",
         now: dt.datetime | None = None) -> None:
    if not text.strip():
        raise ValueError("совет без текста не записывается")
    if not (root / target).is_file():
        raise ValueError(f"позиция не найдена: {target}")
    missing = [name for name, value in (("тип", advice_type), ("контекст", context),
                                        ("основание", basis)) if not value.strip()]
    if missing:
        raise ValueError("у совета не заполнены: " + ", ".join(missing))
    blocked = ineffective_kinds(read(root, role))
    if (role, advice_type) in blocked:
        accepted, completed = blocked[(role, advice_type)]
        raise ValueError(
            f"тип совета «{advice_type}» больше не предлагать: принят {accepted} раз, "
            f"сделано {completed}"
        )
    activity.append(root, ["совет", role, target, advice_type, context, basis, text],
                    now=now)


def read(root: Path, role: str | None = None,
         window_days: int = ANSWER_WINDOW) -> list[Advice]:
    """Связывает совет с принятием и последующим фактическим завершением."""
    given: list[tuple[dt.datetime, int, str, str, str, str, str, str]] = []
    reactions: list[tuple[dt.datetime, int, str, str]] = []
    for entry in activity.read(root, events={"совет", "реакция"}):
        if entry.event == "совет" and len(entry.parts) >= 3:
            if len(entry.parts) >= 6:
                kind, context, basis = entry.part(2), entry.part(3), entry.part(4)
                text = " · ".join(entry.parts[5:])
            else:
                # Старые события неизменны и остаются читаемыми, но не участвуют
                # в группировке по типу: его нельзя честно восстановить из текста.
                kind = context = basis = ""
                text = " · ".join(entry.parts[2:])
            given.append((entry.stamp, entry.order, entry.part(0), entry.part(1),
                          kind, context, basis, text))
        elif entry.event == "реакция" and len(entry.parts) >= 2:
            reactions.append((entry.stamp, entry.order, entry.part(0), entry.part(1)))

    out: list[Advice] = []
    for stamp, order, said_by, target, kind, context, basis, said in given:
        if role and said_by != role:
            continue
        answers = [
            (moment, reaction_order, action)
            for moment, reaction_order, action, path in reactions
            if (path == target
                and (moment, reaction_order) >= (stamp, order)
                and moment <= stamp + dt.timedelta(days=window_days))
        ]
        if answers:
            moment, reaction_order, action = min(answers)
            completions = [
                completion
                for completion, completion_order, later_action, path in reactions
                if (path == target and later_action == "завершено"
                    and (completion, completion_order) >= (moment, reaction_order))
            ] if action in ACCEPTED else []
            out.append(Advice(
                stamp=stamp, role=said_by, target=target, text=said, kind=kind,
                context=context, basis=basis, answer=action, answer_stamp=moment,
                completed_stamp=min(completions) if completions else None,
            ))
        else:
            out.append(Advice(stamp=stamp, role=said_by, target=target, text=said,
                              kind=kind, context=context, basis=basis))
    return out


def render(history: list[Advice], role: str | None) -> str:
    who = f"«{role}»" if role else "всех ролей"
    if not history:
        return (f"История советов {who} пуста. Это не разрешение советовать что "
                "угодно: просто повторять пока нечего.")
    rows = []
    for one in history:
        detail = (f"тип: {one.kind}; контекст: {one.context}; основание: {one.basis}"
                  if one.kind else "старый формат: контекст и основание не записаны")
        rows.append(f"- {one.stamp:%d.%m} · {one.role} · {one.text} "
                    f"→ {one.outcome} · {one.target}\n  {detail}")
    refused = [one for one in history if one.outcome == "отвергнут"]
    tail = ""
    if refused:
        tail = ("\n\nОтвергнуто — не повторять ни этими словами, ни другими:\n"
                + "\n".join(f"- {one.text} ({one.target})" for one in refused))
    blocked = ineffective_kinds(history)
    if blocked:
        tail += ("\n\nБесполезные типы — больше не предлагать:\n"
                 + "\n".join(
                     f"- {blocked_role} · {kind}: принято {accepted} раз, "
                     f"сделано {completed}"
                     for (blocked_role, kind), (accepted, completed)
                     in sorted(blocked.items())
                 ))
    silent = sum(one.outcome == "ответа нет" for one in history)
    if silent == len(history):
        tail += ("\n\nНи один совет не получил ответа. Прежде чем советовать снова, "
                 "спроси себя, доходят ли твои советы вообще.")
    return f"Советов {who}: {len(history)}.\n" + "\n".join(rows) + tail


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="история советов роли")
    parser.add_argument("role", nargs="?", help="имя роли из agents/")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--item", help="позиция склада, которой адресован совет")
    parser.add_argument("--text", help="совет одной строкой")
    parser.add_argument("--type", dest="advice_type", help="устойчивый тип совета")
    parser.add_argument("--context", help="что происходило в момент совета")
    parser.add_argument("--basis", help="какие факты стали основанием")
    parser.add_argument("--list", action="store_true", help="показать историю")
    args = parser.parse_args(argv)
    root = args.root.resolve()

    known = roles(root)
    if args.role and args.role not in known:
        print(f"неизвестная роль: {args.role}. Есть: {', '.join(known)}")
        return 2
    if args.list or not (args.item or args.text):
        print(render(read(root, args.role), args.role))
        return 0
    if not args.role:
        print(f"чей это совет? Укажи роль: {', '.join(known)}")
        return 2
    if not args.item or not args.text:
        print("совет записывается только парой: --item <позиция> и --text «…»")
        return 2
    try:
        give(root, args.role, args.item, args.text, advice_type=args.advice_type or "",
             context=args.context or "", basis=args.basis or "")
    except ValueError as exc:
        print(f"не записано: {exc}")
        return 2
    print(f"совет записан: {args.role} · {args.item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
