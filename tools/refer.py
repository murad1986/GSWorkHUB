#!/usr/bin/env python3
"""К чему относится «это», «они», «тот вопрос вчера».

Человек не говорит «измени обязательство ALF-C-006». Он говорит «это пока не
нужно» — и прав: называть позиции ключами должна система, а не он. Значит
разрешать ссылку — её работа.

Порядок опоры жёсткий, от надёжного к слабому:

1. **последнее показанное** — если строка была на экране минуту назад, «это»
   почти наверняка про неё;
2. **слова самой фразы** — имя человека, клиента, темы;
3. **открытые позиции** контейнера, о котором шла речь.

Уверенность возвращается вместе с кандидатами, потому что решение зависит от
неё: уверен — действуй, сомневаешься — покажи интерпретацию, не знаешь — задай
один вопрос. Догадка, выданная за факт, дороже любого уточнения.

Свежесть показа считается в минутах: контекст разговора живёт минутами, а не
вечно. Через два часа «перенеси это» уже ничего не значит без уточнения.
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import activity
import store

FRESH_MINUTES = 30          # пока строка «горячая», она и есть «это»
FADING_MINUTES = 180        # дальше опора слабеет до подсказки
POINTERS = {"это", "эту", "этот", "эта", "того", "тот", "ту", "её", "его", "их",
            "они", "он", "она", "тем", "туда", "там"}
NAMED_TYPES = {"person", "client", "program", "process", "entity", "self"}
STOP = {"пока", "надо", "нужно", "давай", "сделай", "закрой", "перенеси",
        "потом", "сейчас", "уже", "ещё", "если", "когда", "чтобы", "будет"}
OPEN_STATUSES = {"", "open", "in-progress", "waiting", "active", "draft", "candidate"}
HIGH = "высокая"
MEDIUM = "средняя"
LOW = "низкая"
ACTIONS = {
    HIGH: "действовать",
    MEDIUM: "показать интерпретацию",
    LOW: "уточнить",
}
OUTCOMES = {"человек подтвердил", "человек поправил"}


@dataclass
class Candidate:
    note: store.Note
    why: str
    weight: float

    @property
    def confidence(self) -> str:
        if self.weight >= 0.9:
            return "уверен"
        if self.weight >= 0.5:
            return "сомневаюсь"
        return "не знаю"


@dataclass(frozen=True)
class Resolution:
    """Решение по ссылке вместе с безопасным следующим действием."""
    phrase: str
    candidates: tuple[Candidate, ...]
    confidence: str
    action: str
    question: str = ""

    @property
    def target(self) -> store.Note | None:
        return self.candidates[0].note if self.candidates else None


def shown_recently(root: Path, now: dt.datetime,
                   minutes: int = FADING_MINUTES) -> list[tuple[dt.datetime, str]]:
    """Что система показывала человеку и когда — из журнала, а не из памяти."""
    edge = now - dt.timedelta(minutes=minutes)
    out: list[tuple[dt.datetime, str]] = []
    for entry in activity.read(root, events={"представлен"}, now=now):
        if len(entry.parts) >= 2 and entry.stamp >= edge:
            out.append((entry.stamp, entry.parts[-1]))
    return sorted(out, reverse=True)


def words(value: str) -> set[str]:
    return {w for w in re.findall(r"\w+", value.lower())
            if len(w) > 3 and w not in STOP and w not in POINTERS}


def has_pointer(value: str) -> bool:
    lowered = value.lower()
    # В «это 10-е» и «это десятое» слово связывает подлежащее со значением,
    # а не указывает на старую строку разговора.
    lowered = re.sub(
        r"\bэто\s+(?=(?:\d|перв|втор|трет|четв|пят|шест|седьм|восьм|девят|десят))",
        "", lowered)
    seen = set(re.findall(r"\w+", lowered))
    return bool(seen & POINTERS or re.search(r"\bтот\s+(?:вопрос|разговор|документ)",
                                             lowered))


def _names(note: store.Note) -> list[str]:
    values = [note.title, str(note.data.get("name") or ""),
              str(note.data.get("org") or ""), str(note.data.get("prefix") or "")]
    aliases = note.data.get("aliases") or []
    if isinstance(aliases, list):
        values.extend(str(alias) for alias in aliases)
    return [" ".join(value.split()) for value in values if len(" ".join(value.split())) >= 3]


def explicit_name(value: str, note: store.Note) -> str:
    """Какое имя сущности человек произнёс целиком, если произнёс."""
    if note.type not in NAMED_TYPES:
        return ""
    for name in sorted(_names(note), key=len, reverse=True):
        distinctive = (len(name.split()) >= 2 or name.isupper()
                       or any(char.isupper() for char in name[1:]))
        if note.type != "person" and not distinctive:
            continue
        if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", value, re.IGNORECASE):
            return name
    return ""


def has_explicit_reference(value: str, notes: Iterable[store.Note]) -> bool:
    return any(explicit_name(value, note) for note in notes)


def _put(found: dict[str, Candidate], note: store.Note, why: str,
         weight: float) -> None:
    candidate = Candidate(note, why, max(0.0, min(weight, 0.99)))
    if note.rel not in found or found[note.rel].weight < candidate.weight:
        found[note.rel] = candidate


def _holder(notes: Iterable[store.Note], folder: str) -> store.Note | None:
    """Карточка контейнера по пути папки, без догадки по первому файлу."""
    for note in notes:
        if str(Path(note.rel).parent) == folder and note.type in {"client", "program", "self"}:
            return note
    return None


def resolve(root: Path, phrase: str, notes: list[store.Note],
            now: dt.datetime | None = None, *,
            calendar_events: Iterable[object] | None = None,
            recent_notes: Iterable[store.Note] | None = None) -> list[Candidate]:
    now = now or dt.datetime.now()
    context_notes = list(recent_notes or [])
    all_notes = notes + context_notes
    by_rel = {n.rel: n for n in all_notes}
    said = words(phrase)
    points = bool({w for w in re.findall(r"\w+", phrase.lower())} & POINTERS)

    found: dict[str, Candidate] = {}

    # 0. Полностью названный человек или контейнер сильнее совпадения слов с
    # делом внутри него: «Стандарт Управление» — программа, а не любое старое
    # обязательство, в заголовке которого встречаются те же два слова.
    for note in all_notes:
        named = explicit_name(phrase, note)
        if named:
            _put(found, note, f"названо явно: {named}", 0.99)

    # 1. Последнее показанное — самая надёжная опора для «это».
    for stamp, target in shown_recently(root, now):
        note = by_rel.get(target)
        if note is None:
            continue
        age = (now - stamp).total_seconds() / 60
        if age <= FRESH_MINUTES:
            weight, why = 0.95, f"показано {int(age)} мин назад"
        else:
            weight, why = 0.45, f"показано {int(age // 60)} ч назад — опора слабая"
        if not points and said:
            weight -= 0.2
        _put(found, note, why, weight)
        break                      # самое свежее показанное — одно

    # 2. Слова фразы: имя человека, клиента, темы.
    if said:
        for note in all_notes:
            if note.type in {"index", "skill", "reading"}:
                continue
            pool = words(f"{note.title} {note.data.get('name') or ''} "
                         f"{' '.join(str(a) for a in (note.data.get('aliases') or []))}")
            common = said & pool
            if len(common) < 2 and not (common and note.type == "person"):
                continue
            weight = 0.55 + 0.15 * len(common)
            if note.type == "person" and common:
                # Явно произнесённое имя важнее местоимения из прошлого показа.
                # При конфликте решение всё равно будет средней уверенности.
                weight = max(weight, 0.98)
            if note.status in OPEN_STATUSES:
                weight += 0.05
            why = f"совпало по словам: {', '.join(sorted(common))}"
            _put(found, note, why, weight)

    # 3. Календарь даёт ситуативную подсказку, но один никогда не достаточен
    # для действия: название встречи часто короче реального предмета разговора.
    for event in calendar_events or []:
        event_words = words(str(getattr(event, "title", "")))
        common = said & event_words
        base = 0.58 if common else 0.38
        if not common and not points:
            continue
        folder = str(getattr(event, "container", "") or "")
        holder = _holder(notes, folder) if folder else None
        if holder is not None:
            why = "контекст встречи в календаре"
            if common:
                why += f": {', '.join(sorted(common))}"
            _put(found, holder, why, base)
        for person_rel in list(getattr(event, "people", []) or []):
            person = by_rel.get(str(person_rel))
            if person is not None:
                _put(found, person, "участник ближайшей встречи", base)

    # 4. Недавняя встреча помогает понять «тот вопрос вчера», но тоже остаётся
    # подсказкой до словесного совпадения или подтверждения человека.
    for note in context_notes:
        if note.type not in {"meeting", "interview"}:
            continue
        common = said & words(note.title)
        if common:
            _put(found, note, f"недавняя встреча; совпало: {', '.join(sorted(common))}",
                 0.55 + 0.1 * len(common))
        elif points:
            happened = note.date_field("date")
            age = (now.date() - happened).days if happened else 99
            if 0 <= age <= 1:
                _put(found, note, "встреча была сегодня или вчера", 0.42)

    return sorted(found.values(), key=lambda c: -c.weight)[:5]


def decide(phrase: str, candidates: list[Candidate]) -> Resolution:
    """Переводит неоткалиброванный вес в три наблюдаемых исхода.

    Порог сам по себе не доказательство: близкий второй кандидат понижает даже
    сильную первую догадку. Подтверждения и поправки записывает `record_outcome`,
    чтобы веса можно было позже откалибровать на реальном использовании.
    """
    ordered = tuple(candidates)
    if not ordered:
        return Resolution(phrase, ordered, LOW, ACTIONS[LOW], "О чём именно речь?")

    best = ordered[0]
    runner_up = ordered[1] if len(ordered) > 1 else None
    exact_best = best.why.startswith("названо явно:")
    exact_tie = (runner_up is not None
                 and runner_up.why.startswith("названо явно:")
                 and runner_up.weight == best.weight)
    ambiguous = (runner_up is not None and best.weight - runner_up.weight < 0.20
                 and (not exact_best or exact_tie))
    if best.weight >= 0.9 and not ambiguous:
        level = HIGH
    elif best.weight >= 0.5 or (best.weight >= 0.35 and not ambiguous):
        level = MEDIUM
    else:
        level = LOW

    question = ""
    if level == LOW:
        if runner_up is not None:
            question = f"Ты про {best.note.title} или про {runner_up.note.title}?"
        else:
            question = f"Ты про {best.note.title}?"
    return Resolution(phrase, ordered, level, ACTIONS[level], question)


def resolve_reference(root: Path, phrase: str, notes: list[store.Note],
                      now: dt.datetime | None = None, *,
                      calendar_events: Iterable[object] | None = None,
                      recent_notes: Iterable[store.Note] | None = None) -> Resolution:
    return decide(phrase, resolve(root, phrase, notes, now,
                                  calendar_events=calendar_events,
                                  recent_notes=recent_notes))


def record_outcome(root: Path, resolution: Resolution, outcome: str, *,
                   actual_target: str | None = None,
                   now: dt.datetime | None = None) -> None:
    """Пишет только человеческий исход — не собственную догадку системы."""
    if outcome not in OUTCOMES:
        raise ValueError(f"неизвестный исход разрешения ссылки: {outcome}")
    proposed = resolution.target.rel if resolution.target is not None else "не определено"
    actual = actual_target or proposed
    if outcome == "человек поправил" and actual == "не определено":
        raise ValueError("для поправки нужна фактическая цель")
    activity.append(root, ["ссылка разрешена", outcome, proposed, actual,
                           resolution.confidence, resolution.phrase], now=now)


def render(candidates: list[Candidate]) -> str:
    if not candidates:
        return ("Не понял, о чём речь: ничего свежего не показывалось и слова "
                "фразы ни с чем не совпали. Спроси человека прямо.")
    best = candidates[0]
    rows = [f"Уверенность: {best.confidence}"]
    for one in candidates:
        rows.append(f"  · {one.note.title} — {one.why} [{one.note.rel}]")
    if best.confidence == "уверен":
        rows.append("Действовать, назвав, что понял.")
    elif best.confidence == "сомневаюсь":
        rows.append("Назвать свою интерпретацию и дождаться «да».")
    else:
        rows.append("Задать один уточняющий вопрос, а не гадать.")
    return "\n".join(rows)


def render_resolution(resolution: Resolution) -> str:
    if not resolution.candidates:
        return f"Уверенность: {resolution.confidence}\n{resolution.question}"
    rows = [f"Уверенность: {resolution.confidence}"]
    rows.extend(f"  · {one.note.title} — {one.why} [{one.note.rel}]"
                for one in resolution.candidates)
    if resolution.confidence == HIGH:
        rows.append("Действовать, назвав, что понял.")
    elif resolution.confidence == MEDIUM:
        rows.append("Назвать свою интерпретацию и дождаться «да».")
    else:
        rows.append(resolution.question)
    return "\n".join(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="к чему относится реплика")
    parser.add_argument("phrase", help="фраза человека, как сказана")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    root = args.root.resolve()
    loaded = store.load(root, "work")
    if loaded.unreadable:
        print(loaded.complain())
        return 1
    print(render_resolution(resolve_reference(root, args.phrase, loaded.notes)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
