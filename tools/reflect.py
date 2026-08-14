#!/usr/bin/env python3
"""Самоанализ: помогает ли рабочий вход и движется ли сама работа.

Техническая сборка, представление человеку и его реакция — разные события.
Отдача считается только по явной паре `представлен → реакция`; правка файла не
считается действием, а старые строки `показано` не входят в новую выборку.

Предложения по настройке не применяются автоматически. Система показывает
наблюдение и цену, решение остаётся человеку.
"""

from __future__ import annotations

import argparse
import datetime as dt
import statistics
from dataclasses import dataclass, field
from pathlib import Path

import yaml
import activity
from store import Note, load

MIN_SAMPLES = 5
MIN_DAYS = 10
TOUCH_WINDOW = 7
TOUCH_KINDS = set(activity.TOUCHES.values())
POSITIVE = {"взято", "завершено", "поправлено"}
KNOWN_REACTIONS = POSITIVE | {"отложено", "отклонено", "отменено"}


@dataclass(frozen=True)
class Presentation:
    stamp: dt.datetime
    kind: str
    target: str
    order: int = 0


@dataclass(frozen=True)
class Reaction:
    stamp: dt.datetime
    action: str
    target: str
    reason: str = ""
    order: int = 0


@dataclass
class Signal:
    kind: str
    shown: int = 0
    accepted: int = 0
    completed: int = 0
    corrected: int = 0
    deferred: int = 0
    dismissed: int = 0
    cancelled: int = 0
    days: set[str] = field(default_factory=set)

    @property
    def acted(self) -> int:
        return self.accepted + self.completed + self.corrected

    @property
    def responded(self) -> int:
        return (self.acted + self.deferred + self.dismissed + self.cancelled)

    @property
    def closed(self) -> int:
        """Петля закрыта любым явным разрешением, а не только делом.

        «Мне это неважно, больше не показывай» — полноценное закрытие: система
        получила ответ и знает, что делать дальше. Более того, оно учит сильнее,
        чем «беру»: согласие подтверждает то, что система и так думала, отказ
        показывает, где её модель человека неверна.
        """
        return self.responded

    @property
    def closure(self) -> float | None:
        """Главная мера: какая доля поднятого получила явное разрешение."""
        return self.closed / self.shown if self.shown else None

    @property
    def rate(self) -> float | None:
        """Вспомогательная: какая доля закрылась делом, а не отказом."""
        return self.acted / self.shown if self.shown else None


@dataclass(frozen=True)
class Touch:
    stamp: dt.datetime
    kind: str


@dataclass(frozen=True)
class TouchDay:
    day: dt.date
    morning: bool
    evening: bool
    answered: bool

    @property
    def complete(self) -> bool:
        return self.morning and self.evening

    @property
    def missing(self) -> list[str]:
        return ([] if self.morning else ["утро"]) + ([] if self.evening else ["вечер"])


@dataclass
class Flow:
    wip: int
    throughput: int
    ages: list[int]
    cycles: list[int]
    undated_closed: int
    window_days: int


def read_log(root: Path) -> tuple[list[Presentation], list[Reaction]]:
    """Читает только новую телеметрию; техническое `показано` намеренно забыто."""
    presentations: list[Presentation] = []
    reactions: list[Reaction] = []
    for entry in activity.read(root, events={"представлен", "реакция"}):
        if entry.event == "представлен" and len(entry.parts) == 2:
            presentations.append(Presentation(
                entry.stamp, entry.part(0), entry.part(1), entry.order))
        elif (entry.event == "реакция" and len(entry.parts) >= 2
              and entry.part(0) in KNOWN_REACTIONS):
            reactions.append(Reaction(
                entry.stamp, entry.part(0), entry.part(1),
                " · ".join(entry.parts[2:]), entry.order))
    return presentations, reactions


def read_touches(root: Path) -> list[Touch]:
    """Заход системы — отдельный факт: он не говорит, что человек читал."""
    touches: list[Touch] = []
    for entry in activity.read(root, events={"касание"}):
        if len(entry.parts) == 1 and entry.part(0) in TOUCH_KINDS:
            touches.append(Touch(entry.stamp, entry.part(0)))
    return touches


def touch_days(touches: list[Touch], reactions: list[Reaction], today: dt.date,
               days: int = TOUCH_WINDOW) -> list[TouchDay]:
    """Только завершённые сутки: у сегодняшнего дня вечер ещё не наступил.

    Дни до первого касания в журнале пропусками не считаются: тогда механики
    ещё не было, и назвать это молчанием канала значило бы выдумать наблюдение.
    """
    if not touches:
        return []
    kinds: dict[dt.date, set[str]] = {}
    for touch in touches:
        kinds.setdefault(touch.stamp.date(), set()).add(touch.kind)
    answered = {reaction.stamp.date() for reaction in reactions}
    first = max(today - dt.timedelta(days=days), min(kinds))
    rows: list[TouchDay] = []
    day = first
    while day < today:
        seen = kinds.get(day, set())
        rows.append(TouchDay(day, "утро" in seen, "вечер" in seen, day in answered))
        day += dt.timedelta(days=1)
    return rows


def touch_line(rows: list[TouchDay]) -> str:
    """Одна строка для утреннего брифа: молчание канала не должно выглядеть тишиной."""
    if not rows:
        return "Касания ещё не отмечались."
    last = rows[-1]
    if last.missing:
        gap = " и ".join(last.missing)
        return (f"Вчера ({last.day:%d.%m}) не состоялось: {gap}. "
                "Заход не дошёл до склада — это не спокойный день.")
    silent = sum(1 for row in rows if not row.answered)
    if silent == len(rows):
        return (f"Заходы шли все {len(rows)} дн., но ни один не получил ответа: "
                "система говорит, человек молчит.")
    return (f"Заходы за {len(rows)} дн.: полных дней "
            f"{sum(1 for row in rows if row.complete)}, "
            f"с ответом человека {len(rows) - silent}.")


def analyse(presentations: list[Presentation], reactions: list[Reaction],
            window_days: int) -> dict[str, Signal]:
    """Одна реакция оплачивает один, ближайший к ней показ той же цели."""
    signals: dict[str, Signal] = {}
    indexed: list[tuple[Presentation, bool]] = []
    for presentation in sorted(presentations, key=lambda item: item.stamp):
        signal = signals.setdefault(presentation.kind, Signal(presentation.kind))
        signal.shown += 1
        signal.days.add(presentation.stamp.date().isoformat())
        indexed.append((presentation, False))

    used: set[int] = set()
    for reaction in sorted(reactions, key=lambda item: item.stamp):
        candidates = [
            (index, presentation)
            for index, (presentation, _) in enumerate(indexed)
            if index not in used and presentation.target == reaction.target
            and (presentation.stamp, presentation.order)
            < (reaction.stamp, reaction.order)
            and reaction.stamp <= presentation.stamp + dt.timedelta(days=window_days)
        ]
        if not candidates:
            continue
        index, presentation = max(
            candidates, key=lambda item: (item[1].stamp, item[1].order)
        )
        used.add(index)
        signal = signals[presentation.kind]
        if reaction.action == "взято":
            signal.accepted += 1
        elif reaction.action == "завершено":
            signal.completed += 1
        elif reaction.action == "поправлено":
            signal.corrected += 1
        elif reaction.action == "отложено":
            signal.deferred += 1
        elif reaction.action == "отклонено":
            signal.dismissed += 1
        elif reaction.action == "отменено":
            signal.cancelled += 1
    return signals


def working_days(signals: dict[str, Signal]) -> int:
    return len({day for signal in signals.values() for day in signal.days})


REFUSALS = {"отклонено", "отменено"}
RULE_AFTER = 3


@dataclass
class Refusal:
    kind: str
    count: int
    reasons: list[str]
    targets: list[str]


def refusals(presentations: list[Presentation], reactions: list[Reaction],
             threshold: int = RULE_AFTER) -> list[Refusal]:
    """Отказы, накопившиеся по одному поводу, — заготовка правила.

    §9 контракта: правило, выведенное из трёх отказов подряд, сильнее любой
    инструкции. Механика здесь только считает и показывает; правило пишет
    человек, потому что вывод из отказов — это суждение о нём самом.

    Повод — вид строки, а не отдельная позиция: три раза отклонённое «чтение»
    говорит о правиле, три отказа по одному делу — только об этом деле.
    """
    kind_of = {}
    for presentation in sorted(presentations, key=lambda item: item.stamp):
        kind_of.setdefault(presentation.target, presentation.kind)

    grouped: dict[str, Refusal] = {}
    for reaction in sorted(reactions, key=lambda item: item.stamp):
        if reaction.action not in REFUSALS:
            continue
        kind = kind_of.get(reaction.target)
        if not kind:
            continue
        entry = grouped.setdefault(kind, Refusal(kind, 0, [], []))
        entry.count += 1
        if reaction.reason and reaction.reason not in entry.reasons:
            entry.reasons.append(reaction.reason)
        if reaction.target not in entry.targets:
            entry.targets.append(reaction.target)

    return sorted((one for one in grouped.values() if one.count >= threshold),
                  key=lambda one: -one.count)


def rule_proposals(found: list[Refusal]) -> list[str]:
    out = []
    for one in found:
        why = "; ".join(one.reasons[:3]) if one.reasons else "причина не названа"
        out.append(
            f"«{one.kind}» отвергнуто {one.count} раз — это уже правило, а не "
            f"разовое возражение. Названные причины: {why}. Записать правило в "
            "профильный файл роли и перестать показывать, либо переписать сигнал."
        )
    return out


def proposals(signals: dict[str, Signal], config: dict, days: int = 0) -> list[str]:
    if days < MIN_DAYS:
        return [(f"Предложений нет: {days} дней наблюдений из {MIN_DAYS}. "
                 "Настраивать правила по такому объёму — гадание.")]
    out: list[str] = []
    for signal in sorted(signals.values(), key=lambda one: one.kind):
        if signal.shown < MIN_SAMPLES:
            continue
        rate = signal.rate or 0
        if rate < 0.2:
            out.append(
                f"«{signal.kind}»: полезная реакция была после {rate:.0%} из "
                f"{signal.shown} представлений. Сначала переписать или поднять "
                "порог, затем наблюдать ещё месяц; удаление оставит слепую зону."
            )
        if signal.dismissed >= MIN_SAMPLES and signal.dismissed > signal.acted:
            out.append(
                f"«{signal.kind}»: отклонено {signal.dismissed} раз, полезных "
                f"реакций {signal.acted}. Проверить, объясняет ли строка цену "
                "бездействия и предлагает ли конкретный следующий шаг."
            )
        if signal.deferred >= MIN_SAMPLES and signal.deferred > signal.acted:
            out.append(
                f"«{signal.kind}»: отложено {signal.deferred} раз — возможно, "
                "сигнал приходит слишком рано или не учитывает ёмкость."
            )
    stale = config.get("stale_days") or {}
    if not out and any(signal.shown for signal in signals.values()):
        out.append("Оснований менять правила нет. Текущие пороги: "
                   + ", ".join(f"{kind} {days} дн." for kind, days in stale.items()))
    return out


def flow_metrics(notes: list[Note], today: dt.date, window_days: int = 30) -> Flow:
    commitments = [note for note in notes if note.type == "commitment"]
    edge = today - dt.timedelta(days=window_days - 1)
    live_started = [
        note for note in commitments
        if note.status in {"in-progress", "waiting"} and note.date_field("started")
    ]
    closed_in_window = [
        note for note in commitments
        if note.status == "resolved"
        and (resolved := note.date_field("resolved"))
        and edge <= resolved <= today
    ]
    ages = [
        max(0, (today - started).days)
        for note in live_started if (started := note.date_field("started"))
    ]
    cycles = [
        max(0, (resolved - started).days)
        for note in closed_in_window
        if (started := note.date_field("started"))
        and (resolved := note.date_field("resolved"))
    ]
    undated_closed = sum(
        1 for note in commitments
        if note.status in {"resolved", "cancelled"}
        and (not note.date_field("started") or not note.date_field("resolved"))
    )
    return Flow(
        wip=len(live_started),
        throughput=len(closed_in_window),
        ages=ages,
        cycles=cycles,
        undated_closed=undated_closed,
        window_days=window_days,
    )


def _number(value: float) -> str:
    return f"{value:g}"


def render_touches(rows: list[TouchDay]) -> str:
    if not rows:
        return "Касания ещё не отмечались."
    mark = {True: "да", False: "нет"}
    table = "\n".join(
        f"| {row.day:%d.%m} | {mark[row.morning]} | {mark[row.evening]} | "
        f"{mark[row.answered]} |"
        for row in rows
    )
    gaps = [f"{row.day:%d.%m} — нет {' и '.join(row.missing)}"
            for row in rows if row.missing]
    tail = ("\n\nНе состоялось: " + "; ".join(gaps) + "."
            if gaps else "\n\nПропусков нет.")
    return ("| День | Утро | Вечер | Ответ человека |\n|---|---|---|---|\n"
            + table + tail)


def render(signals: dict[str, Signal], props: list[str], flow: Flow,
           response_window: int, now: dt.datetime,
           touches: list[TouchDay] | None = None,
           rules: list[str] | None = None) -> str:
    useful = working_days(signals)
    enough = useful >= MIN_DAYS
    if not signals:
        table = ("Новая выборка пока пуста. Старые строки `показано`, созданные "
                 "пересборкой, намеренно не считаются использованием.")
    elif not enough:
        rows = "\n".join(
            f"| {signal.kind} | {signal.shown} | {signal.responded} |"
            for signal in sorted(signals.values(), key=lambda one: -one.shown)
        )
        table = (
            f"Отдача не считается: наблюдений меньше {MIN_DAYS} дней. Пока только объём:\n\n"
            "| Строка | Представлена | Получила реакцию |\n"
            "|---|---:|---:|\n" + rows
        )
    else:
        rows = "\n".join(
            f"| {signal.kind} | {signal.shown} | "
            f"{'—' if signal.closure is None else format(signal.closure, '.0%')} | "
            f"{signal.acted} | {signal.deferred} | {signal.dismissed} |"
            for signal in sorted(signals.values(), key=lambda one: -one.shown)
        )
        table = (
            "| Строка | Поднято | Петля закрыта | Делом | Отложено | Отвергнуто |\n"
            "|---|---:|---:|---:|---:|---:|\n" + rows
        )

    days = sorted({day for signal in signals.values() for day in signal.days})
    span = (
        f"Рабочие строки действительно представлялись в {len(days)} днях, "
        f"с {days[0][8:10]}.{days[0][5:7]} по {days[-1][8:10]}.{days[-1][5:7]}."
        if days else "Рабочие строки ещё не представлялись."
    )
    age = ("—" if not flow.ages else
           f"медиана {_number(statistics.median(flow.ages))} дн., "
           f"самому старому {max(flow.ages)} дн.")
    cycle = ("—" if not flow.cycles else
             f"медиана {_number(statistics.median(flow.cycles))} дн.")
    legacy = (
        f"\n- Исторических закрытий без полной пары дат: {flow.undated_closed}; "
        "они не входят в длительность цикла."
        if flow.undated_closed else ""
    )
    warning = (
        f"\n> Дней с реальным представлением: {useful} из {MIN_DAYS}, нужных для выводов.\n"
        "> До порога проценты не показываются.\n"
        if not enough else ""
    )

    return f"""---
type: self-review
generated: true
generated_at: {now:%Y-%m-%dT%H:%M}
window_days: {response_window}
---

# Работает ли система

## Состоялись ли заходы

Заход — то, что система вышла на связь: утренний бриф и вечернее закрытие.
Читал ли человек и ответил ли — отдельные факты, по заходу они не выводятся.

{render_touches(touches or [])}

## Движется ли работа

- В работе: {flow.wip}.
- Завершено за {flow.window_days} дн.: {flow.throughput}.
- Возраст незавершённых: {age}
- Длительность завершённого цикла: {cycle}{legacy}

## Закрывается ли петля

Главная мера — не «сколько сигналов сработало», а **какая доля поднятого
получила явное разрешение**. Закрытием считается любой ответ, по которому видно,
что делать дальше: взял, отложил, жду, отменил, поправил формулировку, «больше
не показывай». Отказ закрывает петлю не хуже дела — и учит сильнее: согласие
подтверждает то, что система и так думала.

{span}
{warning}
Реакция относится к ближайшему предшествующему представлению той же позиции в
течение {response_window} дн. Одна реакция не оплачивает повторные показы.

{table}

## Отказы, накопившиеся в правило

{chr(10).join(f"- {rule}" for rule in (rules or [])) or
 "- пока ни один повод не набрал трёх отказов"}

Правило пишет человек: вывод из отказов — суждение о нём самом, и делать его за
него нельзя. Место правила — профильный файл роли в `agents/`, а не память
инструмента: память не в git, её не видят гейты и она не переживёт смену
инструмента.

## Что предлагаю подкрутить

{chr(10).join(f"- {proposal}" for proposal in props) or "- нечего: строк не было"}

Предложения не применяются сами. Правила живут в `config/attention.yml`;
менять их — решение человека.
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="самоанализ рабочего цикла")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--window", type=int, default=3,
                        help="окно между представлением и реакцией, дней")
    parser.add_argument("--flow-window", type=int, default=30,
                        help="окно завершённых циклов, дней")
    parser.add_argument("--today", type=dt.date.fromisoformat, default=dt.date.today())
    parser.add_argument("--touches", action="store_true",
                        help="одной строкой: состоялись ли заходы, без пересборки вида")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    root = args.root.resolve()

    conf = yaml.safe_load((root / "config" / "attention.yml").read_text(
        encoding="utf-8")) or {}
    presentations, reactions = read_log(root)
    touches = touch_days(read_touches(root), reactions, args.today)
    if args.touches:
        print(touch_line(touches))
        return 0
    signals = analyse(presentations, reactions, args.window)
    loaded = load(root, "work")
    if loaded.unreadable:
        print(loaded.complain())
        return 1
    flow = flow_metrics(loaded.notes, args.today, args.flow_window)
    content = render(
        signals,
        proposals(signals, conf, working_days(signals)),
        flow,
        args.window,
        dt.datetime.now().replace(second=0, microsecond=0),
        touches,
        rule_proposals(refusals(presentations, reactions)),
    )

    if args.dry_run:
        print(content)
        return 0
    (root / "wiki" / "self-review.md").write_text(content, encoding="utf-8")
    print(f"самоанализ: {len(presentations)} представлений, {len(reactions)} реакций, "
          f"{len(signals)} классов строк")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
