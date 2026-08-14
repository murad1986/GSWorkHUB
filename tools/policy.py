#!/usr/bin/env python3
"""Ситуативная политика: какой класс предложений уместен именно сегодня.

Профиль человека задаёт устойчивые ограничения. Этот модуль добавляет состояние
дня и не выдумывает его из текста: ёмкость читает capacity, рабочие сигналы —
attention, закрытие петли — reflect. Выход — не скрытый балл, а решение с
причинами: класс снят целиком, поднят или оставлен допустимым.
"""

from __future__ import annotations

import argparse
import datetime as dt
from dataclasses import dataclass
from pathlib import Path

import activity
import attention
import capacity as capacity_module
import reflect
import store
import yaml

BLOCKED = "снято"
RAISED = "поднято"
ALLOWED = "допустимо"
UNRESOLVED = {"open", "in-progress", "waiting"}
PROPOSAL_CLASSES = ("research", "action", "decision", "learning", "closure")
CLASS_NAMES = {
    "research": "Исследование",
    "action": "Действие",
    "decision": "Решение",
    "learning": "Обучение",
    "closure": "Закрытие начатого",
}
RESEARCH_TYPES = {"question", "digest", "hypothesis", "risk"}
NIGHT_DECISION_EVENTS = {
    "реакция", "решение подтверждено", "подтверждено", "принято", "поправка",
}


class PolicyError(RuntimeError):
    """Состояние прочитано не полностью, поэтому решение нельзя выдавать."""


@dataclass(frozen=True)
class Situation:
    today: dt.date
    capacity: str
    capacity_reason: str = ""
    active_containers: int = 0
    max_active_containers: int = 3
    open_promises: int = 0
    recent_research: int = 0
    analysis_drift: bool = False
    night_entries: int = 0
    closure_rate: float | None = None
    closure_samples: int = 0


@dataclass(frozen=True)
class Decision:
    proposal_class: str
    status: str
    reasons: tuple[str, ...]

    @property
    def allowed(self) -> bool:
        return self.status != BLOCKED

    def render(self) -> str:
        name = CLASS_NAMES[self.proposal_class]
        why = "; ".join(self.reasons)
        return f"{name} сегодня {self.status}: {why}."


@dataclass(frozen=True)
class Proposal:
    proposal_class: str
    text: str


def _closure(root: Path, today: dt.date, conf: dict) -> tuple[float | None, int]:
    policy = conf.get("situational_policy") or {}
    window = int(policy.get("closure_window_days", 7))
    response = int(policy.get("closure_response_days", 3))
    edge = today - dt.timedelta(days=window - 1)
    presentations, reactions = reflect.read_log(root)
    recent_presentations = [
        one for one in presentations if edge <= one.stamp.date() <= today
    ]
    bounded_reactions = [one for one in reactions if one.stamp.date() <= today]
    signals = reflect.analyse(recent_presentations, bounded_reactions, response)
    shown = sum(one.shown for one in signals.values())
    closed = sum(one.closed for one in signals.values())
    return (closed / shown if shown else None, shown)


def snapshot(root: Path, today: dt.date, *, capacity: str = "auto",
             now: dt.datetime | None = None) -> Situation:
    """Собирает один снимок из фактов склада, не читая производный wiki-вид."""
    root = root.resolve()
    now = now or dt.datetime.now().replace(microsecond=0)
    try:
        conf = yaml.safe_load((root / "config" / "attention.yml").read_text(
            encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise PolicyError(f"настройки состояния не прочитаны: {exc}") from exc

    loaded = store.load(root, "work", "raw")
    if loaded.unreadable:
        raise PolicyError(loaded.complain() or "склад прочитан не полностью")
    work = loaded.sort("work")
    raw = loaded.sort("raw")
    entries = activity.read(root, now=now)

    if capacity == "auto":
        measured = capacity_module.measure(today=today)
        capacity_level, capacity_reason = measured.level, measured.reason
    else:
        capacity_level, capacity_reason = capacity, "задана на входе"

    tracker = conf.get("tracker") or {}
    max_active = int(tracker.get("max_active_containers", 3))
    active = sum(
        note.type in {"client", "program", "self"}
        and str(note.data.get("mode") or "") == "active"
        for note in work
    )
    promises = sum(
        note.type == "commitment" and note.status in UNRESOLVED
        and str(note.data.get("direction") or "") == "outbound"
        for note in work
    )

    policy_conf = conf.get("situational_policy") or {}
    research_days = int(policy_conf.get("recent_research_days", 1))
    by_rel = {note.rel: note for note in loaded.notes}
    recent_research = sum(
        note.type in RESEARCH_TYPES
        and (age := attention.age_days(note, today, by_rel)) is not None
        and 0 <= age <= research_days
        for note in work
    )

    lines = attention.build_lines(
        work, conf, today, raw=raw, log_entries=entries, now=now
    )
    drift = any(line.kind == "анализ" for line in lines)
    decision_entries = [entry for entry in entries
                        if entry.event in NIGHT_DECISION_EVENTS]
    night = len(attention.night_log_entries(
        decision_entries, conf.get("night") or {}, now
    ))
    closure_rate, closure_samples = _closure(root, today, conf)
    return Situation(
        today=today,
        capacity=capacity_level,
        capacity_reason=capacity_reason,
        active_containers=active,
        max_active_containers=max_active,
        open_promises=promises,
        recent_research=recent_research,
        analysis_drift=drift,
        night_entries=night,
        closure_rate=closure_rate,
        closure_samples=closure_samples,
    )


def _closure_is_low(situation: Situation, conf: dict) -> bool:
    policy = conf.get("situational_policy") or {}
    samples = int(policy.get("min_closure_samples", 3))
    minimum = float(policy.get("min_closure_rate", 0.5))
    return (situation.closure_samples >= samples
            and situation.closure_rate is not None
            and situation.closure_rate < minimum)


def decide(situation: Situation, proposal_class: str,
           conf: dict | None = None) -> Decision:
    """Складывает факты в решение; первое значение — класс, не отдельный текст."""
    if proposal_class not in PROPOSAL_CLASSES:
        raise ValueError(f"неизвестный класс предложения: {proposal_class}")
    conf = conf or {}
    policy = conf.get("situational_policy") or {}
    promises_limit = int(policy.get("promises_before_research_block", 2))
    low_closure = _closure_is_low(situation, conf)

    if proposal_class == "research":
        blockers: list[str] = []
        if situation.capacity == "low" and situation.recent_research:
            blockers.append(
                f"ёмкость низкая, а свежих разборов уже {situation.recent_research}"
            )
        if situation.analysis_drift:
            blockers.append("уже сработал сигнал «анализ без действия»")
        if (situation.active_containers > situation.max_active_containers
                and situation.open_promises >= promises_limit):
            blockers.append(
                f"живых контуров {situation.active_containers} при пределе "
                f"{situation.max_active_containers}, открытых обещаний "
                f"{situation.open_promises}"
            )
        if low_closure and situation.recent_research:
            blockers.append(
                f"закрыто {situation.closure_rate:.0%} из "
                f"{situation.closure_samples} поднятых петель"
            )
        if blockers:
            return Decision(proposal_class, BLOCKED, tuple(blockers))
        if situation.capacity == "full":
            return Decision(
                proposal_class, RAISED,
                ("ёмкость полная и нет сигнала, что новый разбор увеличит незавершённое",),
            )
        return Decision(
            proposal_class, ALLOWED,
            ("нет основания снять класс, но полная ёмкость не подтверждена",),
        )

    if proposal_class == "action":
        reasons = []
        if situation.analysis_drift:
            reasons.append("накопился анализ без действия")
        if situation.recent_research:
            reasons.append(f"свежих разборов {situation.recent_research} — пора применить")
        if situation.open_promises:
            reasons.append(f"открытых обещаний {situation.open_promises}")
        return Decision(
            proposal_class, RAISED if reasons else ALLOWED,
            tuple(reasons or ["сигналов срочно поднимать действие нет"]),
        )

    if proposal_class == "decision":
        if situation.night_entries:
            return Decision(
                proposal_class, BLOCKED,
                ((f"ночных решений и реакций {situation.night_entries}: "
                  "сначала утренний пересмотр"),),
            )
        return Decision(proposal_class, ALLOWED, ("ночного пересмотра не требуется",))

    if proposal_class == "learning":
        if situation.capacity == "low" and situation.open_promises:
            return Decision(
                proposal_class, BLOCKED,
                (f"ёмкость низкая, открытых обещаний {situation.open_promises}",),
            )
        if situation.capacity == "full" and not situation.open_promises:
            return Decision(
                proposal_class, RAISED,
                ("ёмкость полная и открытых обещаний нет",),
            )
        return Decision(proposal_class, ALLOWED, ("нет причины снимать обучение",))

    reasons = []
    if situation.open_promises:
        reasons.append(f"открытых обещаний {situation.open_promises}")
    if low_closure:
        reasons.append(
            f"закрыто {situation.closure_rate:.0%} из {situation.closure_samples} петель"
        )
    return Decision(
        proposal_class, RAISED if reasons else ALLOWED,
        tuple(reasons or ["заметного долга по закрытию петель нет"]),
    )


def filter_proposals(situation: Situation, proposals: list[Proposal],
                     conf: dict | None = None) -> list[Proposal]:
    """Запрет применяется ко всему классу, а не поштучно после ранжирования."""
    decisions = {
        proposal.proposal_class: decide(situation, proposal.proposal_class, conf)
        for proposal in proposals
    }
    return [proposal for proposal in proposals
            if decisions[proposal.proposal_class].allowed]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="политика предложений на сегодня")
    parser.add_argument("proposal_class", nargs="?", choices=PROPOSAL_CLASSES)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--today", type=dt.date.fromisoformat, default=dt.date.today())
    parser.add_argument("--capacity", choices=["auto", "full", "half", "low", "unknown"],
                        default="auto")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        situation = snapshot(root, args.today, capacity=args.capacity)
        conf = yaml.safe_load((root / "config" / "attention.yml").read_text(
            encoding="utf-8")) or {}
    except (PolicyError, OSError, yaml.YAMLError) as exc:
        print(f"политика не рассчитана: {exc}")
        return 2
    classes = [args.proposal_class] if args.proposal_class else list(PROPOSAL_CLASSES)
    for proposal_class in classes:
        print(decide(situation, proposal_class, conf).render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
