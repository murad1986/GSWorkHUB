#!/usr/bin/env python3
"""Движок вмешательств: что сделать, когда и каким каналом.

`attention` находит то, что требует внимания. Этот модуль делает следующий
шаг: детерминированно собирает кандидатов, проверяет состояние дня и бюджет
внимания, находит реальное окно календаря и только после этого при
необходимости отдаёт один найденный кандидат модели для подготовки черновика.

Движок ничего не отправляет наружу. Даже шестой уровень означает «подготовить
и спросить владельца», а не написать другому человеку.
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

import activity
import agenda
import attention
import policy
import store
import yaml

LEVEL_NAMES = {
    1: "молча записать",
    2: "добавить в ближайший бриф",
    3: "создать задачу",
    4: "написать самому",
    5: "подготовить решение с черновиком",
    6: "подготовить и спросить разрешения",
}
LEVEL_CHANNELS = {
    1: "журнал",
    2: "бриф",
    3: "TickTick",
    4: "Telegram",
    5: "Telegram",
    6: "Telegram",
}
RESOLUTIONS = ("сделать", "изменить", "не делать")


@dataclass(frozen=True)
class Candidate:
    """Наблюдаемый повод вмешаться, найденный без модели."""

    key: str
    kind: str
    target: str
    text: str
    significance: int
    base_level: int
    proposal_class: str = "action"
    optional: bool = True
    required_minutes: int = 0
    deadline: dt.datetime | None = None
    needs_model: bool = False
    draft_seed: str = ""
    choices: tuple[str, ...] = ()


@dataclass(frozen=True)
class DeliveryWindow:
    start: dt.datetime
    end: dt.datetime
    reason: str

    @property
    def minutes(self) -> int:
        return max(0, int((self.end - self.start).total_seconds() // 60))

    def render(self) -> str:
        if self.start == self.end:
            return self.reason
        day = "сегодня" if self.start.date() == dt.date.today() \
            else f"{self.start:%d.%m}"
        return f"{day} {self.start:%H:%M}–{self.end:%H:%M} · {self.reason}"


@dataclass(frozen=True)
class Intervention:
    key: str
    kind: str
    target: str
    text: str
    significance: int
    level: int
    base_level: int
    window: DeliveryWindow
    channel: str
    outcome: str
    optional: bool
    ignored: int = 0
    draft: str = ""
    choices: tuple[str, ...] = ()
    model_used: bool = False

    @property
    def level_name(self) -> str:
        return LEVEL_NAMES[self.level]


@dataclass(frozen=True)
class EngineResult:
    candidates: tuple[Candidate, ...]
    interventions: tuple[Intervention, ...]
    suppressed: int
    model_calls: int
    calendar_known: bool
    calendar_reason: str = ""


def _attention_level(kind: str) -> tuple[int, str, bool]:
    """Уровень, класс и обязательность по наблюдаемому классу attention."""
    return {
        "обещание": (4, "closure", False),
        "вернуться": (4, "closure", False),
        "целостность": (3, "action", False),
        "решение": (6, "decision", True),
        "анализ": (4, "action", True),
        "наружу": (5, "action", True),
        "перегрев": (6, "decision", True),
        "ночное": (2, "decision", True),
        "ждёт разбора": (2, "closure", True),
        "ждёт": (2, "closure", True),
        "профиль": (2, "learning", True),
        "чтение": (2, "learning", True),
        "цель без шагов": (3, "action", True),
        "застряло": (3, "action", True),
    }.get(kind, (2, "action", True))


def detect_attention(lines: list[attention.Line], conf: dict) -> list[Candidate]:
    """Преобразует сигналы attention в кандидатов без языковой модели."""
    engine = conf.get("interventions") or {}
    minutes = int(engine.get("default_task_minutes", 25))
    out: list[Candidate] = []
    for line in lines:
        if line.demoted:
            continue
        level, proposal_class, optional = _attention_level(line.kind)
        # Ранг отвечает за класс тяжести, вес — только за место внутри класса.
        significance = max(1, min(100, 110 - line.rank * 20
                                  + min(30, max(0, line.weight))))
        target = line.target or line.kind
        out.append(Candidate(
            key=f"attention:{line.kind}:{target}",
            kind=line.kind,
            target=target,
            text=line.text,
            significance=significance,
            base_level=level,
            proposal_class=proposal_class,
            optional=optional,
            required_minutes=minutes if level >= 3 else 0,
            draft_seed=(f"Предлагаю минимальный следующий шаг: {line.text}"
                        if level >= 5 else ""),
            needs_model=level >= 5,
            choices=RESOLUTIONS if level >= 5 else (),
        ))
    return out


def _blocking(questions: list[store.Note]) -> list[store.Note]:
    return [note for note in questions if note.data.get("blocks")]


def detect_events(contexts: list[agenda.Context], now: dt.datetime,
                  conf: dict) -> list[Candidate]:
    """Находит значимые хвосты перед событием; пустая встреча молчит."""
    engine = conf.get("interventions") or {}
    advance = dt.timedelta(hours=float(engine.get("advance_hours", 24)))
    minutes = int(engine.get("event_prep_minutes", 20))
    out: list[Candidate] = []
    for one in contexts:
        event = one.event
        default_event = int(engine.get("default_event_minutes", 60))
        event_end = event.end or event.start + dt.timedelta(minutes=default_event)
        if event_end < now or event.start > now + advance or one.empty:
            continue

        blocking = _blocking(one.questions)
        overdue = [note for note in one.mine
                   if (due := note.date_field("due")) is not None
                   and due < now.date()]
        if blocking:
            names = ", ".join(note.title for note in blocking)
            why = f"вопрос «{names}» блокирует уже открытое решение"
            level, significance, optional = 6, 100, False
        elif overdue:
            names = ", ".join(note.title for note in overdue)
            why = f"к встрече просрочено обещание «{names}»"
            level, significance, optional = 6, 95, False
        elif one.mine:
            names = ", ".join(note.title for note in one.mine)
            why = f"к встрече остаётся моё обещание «{names}»"
            level, significance, optional = 5, 85, False
        elif one.questions:
            names = ", ".join(note.title for note in one.questions)
            why = f"до встречи открыт вопрос «{names}»"
            level, significance, optional = 5, 70, True
        else:
            names = ", ".join(note.title for note in one.theirs)
            why = f"на встрече стоит проверить чужое обещание «{names}»"
            level, significance, optional = 5, 55, True

        target = event.container or event.title
        text = (f"{event.title} · {event.start:%d.%m %H:%M}: {why}; "
                "это стоит между человеком и событием.")
        out.append(Candidate(
            key=f"event:{event.start.isoformat()}:{target}",
            kind="подготовка к встрече",
            target=target,
            text=text,
            significance=significance,
            base_level=level,
            proposal_class="closure",
            optional=optional,
            required_minutes=minutes,
            deadline=event.start,
            needs_model=True,
            draft_seed=(f"К встрече «{event.title}» предлагаю сначала закрыть "
                        f"следующий хвост: {why}. Сделать, изменить или не делать?"),
            choices=RESOLUTIONS,
        ))
    return out


def _clock(value: object, default: str) -> dt.time:
    try:
        return dt.time.fromisoformat(str(value or default))
    except ValueError:
        return dt.time.fromisoformat(default)


def _event_interval(event: agenda.Event, default_minutes: int,
                    day_start: dt.datetime,
                    day_end: dt.datetime) -> tuple[dt.datetime, dt.datetime] | None:
    if event.all_day and event.start.date() == day_start.date():
        return day_start, day_end
    end = event.end or event.start + dt.timedelta(minutes=default_minutes)
    start = max(event.start, day_start)
    end = min(end, day_end)
    return (start, end) if start < end else None


def free_windows(events: list[agenda.Event], now: dt.datetime, conf: dict,
                 *, until: dt.datetime | None = None) -> list[DeliveryWindow]:
    """Возвращает фактические промежутки между занятыми событиями календаря."""
    engine = conf.get("interventions") or {}
    work_start = _clock(engine.get("working_start"), "09:00")
    work_end = _clock(engine.get("working_end"), "21:00")
    default_minutes = int(engine.get("default_event_minutes", 60))
    last_day = (until or now).date()
    if last_day < now.date():
        return []
    windows: list[DeliveryWindow] = []
    day = now.date()
    while day <= last_day:
        start = dt.datetime.combine(day, work_start)
        end = dt.datetime.combine(day, work_end)
        cursor = max(start, now) if day == now.date() else start
        busy = sorted(
            interval for event in events
            if (interval := _event_interval(event, default_minutes, start, end))
        )
        merged: list[tuple[dt.datetime, dt.datetime]] = []
        for left, right in busy:
            if merged and left <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], right))
            else:
                merged.append((left, right))
        for left, right in merged:
            if cursor < left:
                windows.append(DeliveryWindow(cursor, left,
                                              "свободное окно календаря"))
            cursor = max(cursor, right)
        if cursor < end:
            windows.append(DeliveryWindow(cursor, end,
                                          "свободное окно календаря"))
        day += dt.timedelta(days=1)
    return windows


def choose_window(windows: list[DeliveryWindow], minutes: int,
                  deadline: dt.datetime | None = None) -> DeliveryWindow | None:
    """Выбирает первый реальный промежуток нужной длины до срока кандидата."""
    duration = dt.timedelta(minutes=max(0, minutes))
    for window in windows:
        edge = min(window.end, deadline) if deadline else window.end
        if window.start + duration <= edge:
            return DeliveryWindow(window.start, window.start + duration,
                                  f"{minutes} мин. в свободном окне календаря")
    return None


def current_event(events: list[agenda.Event], now: dt.datetime,
                  default_minutes: int = 60) -> agenda.Event | None:
    for event in events:
        end = event.end or event.start + dt.timedelta(minutes=default_minutes)
        if ((event.all_day and event.start.date() == now.date())
                or event.start <= now < end):
            return event
    return None


def ignored_streak(entries: list[activity.Entry], target: str,
                   now: dt.datetime, response_hours: float = 24) -> int:
    """Считает подряд устаревшие показы цели после последней явной реакции."""
    shown = [entry for entry in entries
             if entry.event == "представлен" and entry.part(1) == target]
    reactions = [entry for entry in entries
                 if entry.event == "реакция" and entry.part(1) == target]
    last_reaction = max((entry.stamp for entry in reactions), default=dt.datetime.min)
    edge = now - dt.timedelta(hours=response_hours)
    return sum(last_reaction < entry.stamp <= edge for entry in shown)


def optional_used(entries: list[activity.Entry], today: dt.date) -> int:
    """Считает уже доставленные сегодня необязательные прерывания."""
    return sum(
        entry.event == "вмешательство"
        and entry.stamp.date() == today
        and entry.part(4).lower() == "telegram"
        and entry.part(5) == "необязательное"
        for entry in entries
    )


def _budget(situation: policy.Situation, conf: dict) -> int:
    engine = conf.get("interventions") or {}
    budgets = engine.get("optional_budget") or {}
    return max(0, int(budgets.get(situation.capacity, budgets.get("unknown", 0))))


def plan(candidates: list[Candidate], events: list[agenda.Event],
         entries: list[activity.Entry], situation: policy.Situation, conf: dict,
         now: dt.datetime, *, calendar_known: bool = True,
         calendar_reason: str = "",
         prepare: Callable[[Candidate], str] | None = None) -> EngineResult:
    """Строит вмешательства. `prepare` вызывается только после детектора."""
    ordered = sorted(candidates, key=lambda one: (-one.significance, one.key))
    response_hours = float((conf.get("interventions") or {}).get(
        "response_hours", 24))
    default_event_minutes = int((conf.get("interventions") or {}).get(
        "default_event_minutes", 60))
    meeting = current_event(events, now, default_event_minutes) \
        if calendar_known else None
    budget = _budget(situation, conf)
    used = optional_used(entries, now.date())
    planned: list[Intervention] = []
    model_calls = 0
    suppressed = 0

    for candidate in ordered:
        decision = policy.decide(situation, candidate.proposal_class, conf)
        if not decision.allowed:
            suppressed += 1
            continue

        ignored = ignored_streak(entries, candidate.target, now, response_hours)
        level = max(1, candidate.base_level - ignored)
        outcome = "готово к минимальному действию"

        horizon = candidate.deadline or dt.datetime.combine(
            now.date(), _clock((conf.get("interventions") or {}).get("working_end"),
                               "21:00"))
        windows = free_windows(events, now, conf, until=horizon) \
            if calendar_known else []
        window = choose_window(windows, candidate.required_minutes,
                               candidate.deadline)
        if candidate.required_minutes <= 0:
            window = DeliveryWindow(now, now, "отдельного рабочего окна не требуется")
        if window is None:
            reason = ("календарь недоступен"
                      if not calendar_known else
                      f"нет свободных {candidate.required_minutes} мин. до срока")
            window = DeliveryWindow(now, now, reason)
            if level >= 4:
                level = 2
                outcome = "перенесено в бриф: рабочего окна не найдено"

        if ignored:
            outcome = (f"давление снижено после {ignored} проигнорированных показов; "
                       "помощь изменена")

        channel = LEVEL_CHANNELS[level]
        if meeting is not None and level >= 4:
            channel = "бриф"
            outcome = (f"не прерывает встречу «{meeting.title}»; "
                       "подготовлено к свободному окну")

        if candidate.optional and channel == "Telegram":
            if used >= budget:
                level = 2
                channel = LEVEL_CHANNELS[level]
                outcome = (f"перенесено в бриф: бюджет необязательных "
                           f"вмешательств {budget} исчерпан")
            else:
                used += 1

        draft = candidate.draft_seed
        model_used = False
        if candidate.needs_model and level >= 5 and prepare is not None:
            draft = prepare(candidate)
            model_calls += 1
            model_used = True

        planned.append(Intervention(
            key=candidate.key,
            kind=candidate.kind,
            target=candidate.target,
            text=candidate.text,
            significance=candidate.significance,
            level=level,
            base_level=candidate.base_level,
            window=window,
            channel=channel,
            outcome=outcome,
            optional=candidate.optional,
            ignored=ignored,
            draft=draft,
            choices=candidate.choices,
            model_used=model_used,
        ))

    return EngineResult(tuple(ordered), tuple(planned), suppressed, model_calls,
                        calendar_known, calendar_reason)


def resolve(intervention: Intervention, choice: str) -> Intervention:
    """Любой из трёх ответов завершает петлю, а не только «сделать»."""
    if choice not in RESOLUTIONS:
        raise ValueError(f"неизвестный ответ: {choice}")
    return replace(intervention, outcome=f"петля закрыта: {choice}")


def record(root: Path, intervention: Intervention,
           *, now: dt.datetime | None = None) -> None:
    """Фиксирует только реально доставленное вмешательство, не план."""
    activity.append(root, [
        "вмешательство", intervention.key, intervention.kind,
        intervention.target, intervention.level, intervention.channel,
        "необязательное" if intervention.optional else "обязательное",
        intervention.outcome,
    ], now=now)


def render(result: EngineResult, limit: int = 7) -> list[str]:
    if not result.interventions:
        return ["Вмешательств нет — молчание."]
    rows: list[str] = []
    for item in result.interventions[:limit]:
        window = item.window.render()
        rows.append(
            f"L{item.level} · {item.level_name} · {item.channel} · {window}\n"
            f"    {item.text}\n"
            f"    Исход: {item.outcome}"
        )
        if item.draft and item.level >= 5:
            rows.append(f"    Черновик: {item.draft}")
        if item.choices:
            rows.append("    Ответ: " + " / ".join(item.choices))
    hidden = len(result.interventions) - limit
    if hidden > 0:
        rows.append(f"Ещё {hidden} вмешательств скрыто лимитом.")
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="вмешательства на текущую ситуацию")
    parser.add_argument("--root", type=Path,
                        default=Path(__file__).resolve().parents[1])
    parser.add_argument("--today", type=dt.date.fromisoformat,
                        default=dt.date.today())
    parser.add_argument("--capacity",
                        choices=["auto", "full", "half", "low", "unknown"],
                        default="auto")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    now = dt.datetime.now().replace(microsecond=0)
    try:
        conf = yaml.safe_load((root / "config" / "attention.yml").read_text(
            encoding="utf-8")) or {}
        loaded = store.load(root, "work", "raw")
        if loaded.unreadable:
            raise RuntimeError(loaded.complain() or "склад прочитан не полностью")
        work = loaded.sort("work")
        raw = loaded.sort("raw")
        entries = activity.read(root, now=now)
        lines = attention.build_lines(work, conf, args.today, raw=raw,
                                      log_entries=entries, now=now)
        situation = policy.snapshot(root, args.today, capacity=args.capacity, now=now)
    except (OSError, RuntimeError, yaml.YAMLError, policy.PolicyError) as exc:
        print(f"вмешательства не рассчитаны: {exc}")
        return 2

    engine = conf.get("interventions") or {}
    days = max(1, math.ceil(float(engine.get("advance_hours", 24)) / 24) + 1)
    calendar_known, calendar_reason = True, ""
    try:
        events = agenda.match(agenda.fetch(args.today, days=days), work)
    except agenda.CalendarUnavailable as exc:
        events = []
        calendar_known, calendar_reason = False, str(exc)
    contexts = [agenda.context(event, work, raw) for event in events]
    candidates = detect_attention(lines, conf) + detect_events(contexts, now, conf)
    result = plan(candidates, events, entries, situation, conf, now,
                  calendar_known=calendar_known,
                  calendar_reason=calendar_reason)
    for row in render(result, int(engine.get("max_output", 7))):
        print(row)
    if not calendar_known:
        print(f"Календарь не прочитан: {calendar_reason}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
