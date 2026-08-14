#!/usr/bin/env python3
"""Разбор живой реплики до границы решения.

Модуль не исполняет значимые действия. Он сохраняет все найденные намерения,
разрешает местоименную ссылку, называет цену подтверждения и формулирует одну
строку, которую надо произнести человеку. Применение идёт после ответа через
существующие команды склада.
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import activity
import refer
import store

DO_NOW = "сделать сразу"
DO_AND_TELL = "сделать и показать"
PROPOSE_AND_WAIT = "предложить и ждать"
ALWAYS_ASK = "всегда спрашивать"
LEVELS = (DO_NOW, DO_AND_TELL, PROPOSE_AND_WAIT, ALWAYS_ASK)

INTENT_LABELS = {
    "confirmation": "подтверждение",
    "refusal": "отказ",
    "correction": "поправка",
    "defer": "отсрочка",
    "state_change": "смена состояния",
    "fact": "новый факт",
    "commitment": "новое обязательство",
    "decision": "решение",
    "preference": "предпочтение",
    "feedback": "отзыв о работе системы",
    "question": "вопрос",
    "request": "просьба",
    "capture": "захват идеи",
    "reflection": "размышление",
}

WEEKDAYS = {
    "понедельник": 0,
    "вторник": 1,
    "среду": 2,
    "четверг": 3,
    "пятницу": 4,
    "субботу": 5,
    "воскресенье": 6,
}


@dataclass(frozen=True)
class Intent:
    kind: str
    text: str
    start: int
    reason_kind: str = ""
    reason: str = ""
    workflow_action: str = ""
    until: dt.date | None = None

    @property
    def label(self) -> str:
        return INTENT_LABELS[self.kind]


@dataclass(frozen=True)
class ActionContext:
    name: str
    visible_to_other: bool = False
    touches_money: bool = False
    cancels_meeting: bool = False
    changes_client_document: bool = False
    closes_commitment: bool = False
    shifts_due: bool = False
    records_decision: bool = False
    changes_person_rule: bool = False
    creates_task: bool = False
    prepares_dossier: bool = False
    prepares_draft: bool = False
    records_observation: bool = False
    saves_feedback: bool = False
    moves_own_proposal: bool = False
    sets_reminder: bool = False


@dataclass(frozen=True)
class PolicyDecision:
    level: str
    why: str


@dataclass(frozen=True)
class PlannedTransition:
    action: str
    reason: str
    until: dt.date | None = None


@dataclass
class Interpretation:
    utterance: str
    intents: tuple[Intent, ...]
    reference: refer.Resolution | None
    transition: PlannedTransition | None
    policy: PolicyDecision
    spoken: str
    cancelled: bool = False


@dataclass(frozen=True)
class CorrectionResult:
    cancelled: bool
    correction: Intent
    replacement: Interpretation | None


def _matches(pattern: str, text: str) -> list[re.Match[str]]:
    return list(re.finditer(pattern, text, flags=re.IGNORECASE))


def _until(text: str, today: dt.date) -> dt.date | None:
    iso = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", text)
    if iso:
        try:
            return dt.date.fromisoformat(iso.group(1))
        except ValueError:
            return None
    lowered = text.lower()
    if "послезавтра" in lowered:
        return today + dt.timedelta(days=2)
    if "завтра" in lowered:
        return today + dt.timedelta(days=1)
    if "сегодня" in lowered:
        return today
    for word, weekday in WEEKDAYS.items():
        if re.search(rf"\b{word}\b", lowered):
            days = (weekday - today.weekday()) % 7
            return today + dt.timedelta(days=days or 7)
    return None


def refusal_reason(text: str) -> tuple[str, str, str]:
    """Причина отрицательной реакции и соответствующий переход склада."""
    lowered = text.lower()
    dependency = (
        r"\bжд[уеё]м\b", r"\bжду\b", r"\bпока\b.*\b(?:думают|решат|пришл|ответ)",
        r"\bне\s+(?:пришл|ответил|согласовал)", r"\bзависит\b",
    )
    not_ready = (
        r"\bне\s+(?:могу|готов|успеваю)\b", r"\bне сейчас\b",
        r"\bнет времени\b", r"\bпозже\b",
    )
    not_relevant = (
        r"\bнеактуал", r"\bне надо\b", r"\bне нужно\b", r"\bлишн",
        r"\bне относится\b", r"\bбольше не (?:нужно|показывай|напоминай)\b",
    )
    if any(re.search(pattern, lowered) for pattern in dependency):
        return "зависимость", "ждём внешнего события или решения", "wait"
    if any(re.search(pattern, lowered) for pattern in not_ready):
        return "неготовность", "сейчас не готов продолжать", "defer"
    if any(re.search(pattern, lowered) for pattern in not_relevant):
        return "неактуальность", "это больше не нужно", "dismiss"
    return "", "", ""


def classify(utterance: str, *, today: dt.date | None = None) -> list[Intent]:
    """Находит намерения независимо: одно совпадение не глушит остальные."""
    day = today or dt.date.today()
    found: list[Intent] = []
    seen: set[tuple[str, int]] = set()

    def add(kind: str, match: re.Match[str], *, reason_kind: str = "",
            reason: str = "", workflow_action: str = "",
            until: dt.date | None = None) -> None:
        marker = (kind, match.start())
        if marker in seen:
            return
        seen.add(marker)
        found.append(Intent(kind, match.group(0).strip(), match.start(), reason_kind,
                            reason, workflow_action, until))

    patterns = {
        "correction": r"\b(?:ты\s+)?неправильно\s+понял\b|\bмы договорились иначе\b|\bформулировка неверн\w*",
        "confirmation": r"\b(?:да|верно|согласен|подтверждаю|беру)\b",
        "refusal": r"\b(?:нет|не надо|не нужно|не могу|не готов|не сейчас|неактуальн\w*|отказываюсь)\b",
        "defer": r"\b(?:вернись|отложи|перенеси|не сейчас|позже)\b[^;.!?]*",
        "state_change": r"\b(?:уже\s+(?:сделали|закрыли|прислали|готово)|ответ приш[её]л|продолжаю|вопрос закрыт)\b",
        "fact": r"\b[А-Яа-яЁёA-Za-z-]+\s+теперь\s+(?:отвечает|вед[её]т|является)\b[^;.!?]*",
        "commitment": r"\bя\s+(?:обещал|обязался|отправлю|сделаю|подготовлю)\b[^;.!?]*",
        "decision": r"\b(?:решили|решаю|выбираем|выбираю|ид[её]м по|останавливаемся на)\b[^;.!?]*",
        "preference": r"\b(?:предпочитаю|мне удобнее|такие вещи|подобные мелочи|всегда|никогда)\b[^;.!?]*",
        "feedback": r"\b(?:каждый раз|больше не напоминай|не надо каждый раз|это было лишним)\b[^;.!?]*",
        "request": r"\b(?:закрой|закрывай|поставь|запиши|перенеси|отложи|вернись|сделай|подготовь|собери|убери|не показывай|не присылай)\b[^;.!?]*",
        "capture": r"\bзапиши\s+(?:идею|мысль)\b[^;.!?]*",
        "reflection": r"\b(?:была такая мысль|я думаю|я размышляю|можно бы|возможно|если .*можно)\b[^;.!?]*",
    }

    for kind, pattern in patterns.items():
        for match in _matches(pattern, utterance):
            if kind == "refusal":
                reason_kind, reason, action = refusal_reason(utterance)
                add(kind, match, reason_kind=reason_kind, reason=reason,
                    workflow_action=action)
            elif kind == "defer":
                add(kind, match, until=_until(match.group(0), day))
            else:
                add(kind, match)

    for match in _matches(r"(?:^|[.!?]\s*)(?:почему|зачем|как|что|когда|кто)\b[^?]*\?", utterance):
        add("question", match)
    if "?" in utterance and not any(one.kind == "question" for one in found):
        position = utterance.index("?")
        synthetic = re.search(r"[^;.!?]+\?", utterance)
        if synthetic:
            add("question", synthetic)
        else:
            found.append(Intent("question", utterance, position))

    return sorted(found, key=lambda one: (one.start, list(INTENT_LABELS).index(one.kind)))


def confirmation_policy(action: ActionContext) -> PolicyDecision:
    """Самая дорогая сторона действия определяет уровень подтверждения."""
    if (action.visible_to_other or action.touches_money or action.cancels_meeting
            or action.changes_client_document):
        return PolicyDecision(ALWAYS_ASK, "действие видит другой человек или оно необратимо")
    if (action.closes_commitment or action.shifts_due or action.records_decision
            or action.changes_person_rule):
        return PolicyDecision(PROPOSE_AND_WAIT, "меняется обязательство, решение, срок или правило")
    if action.creates_task or action.prepares_dossier or action.prepares_draft:
        return PolicyDecision(DO_AND_TELL, "действие обратимо, но создаёт новый рабочий результат")
    if (action.records_observation or action.saves_feedback or action.moves_own_proposal
            or action.sets_reminder):
        return PolicyDecision(DO_NOW, "действие дешёвое и обратимое")
    return PolicyDecision(PROPOSE_AND_WAIT, "цена неизвестного действия не установлена")


def infer_action(utterance: str, intents: Iterable[Intent]) -> ActionContext:
    lowered = utterance.lower()
    kinds = {one.kind for one in intents}
    general_rule = bool(re.search(r"\b(?:такие|подобные|всегда|никогда|вообще)\b", lowered))
    return ActionContext(
        name=utterance,
        visible_to_other=bool(re.search(r"\b(?:отправ|напиши|пригласи|опубликуй|сообщи)\b", lowered)),
        touches_money=bool(re.search(r"\b(?:деньги|оплат|рубл|₽|сч[её]т)\b", lowered)),
        cancels_meeting=bool(re.search(r"\bотмени\b.*\b(?:встреч|созвон)\b", lowered)),
        changes_client_document=bool(re.search(r"\b(?:измени|исправь|перепиши)\b.*\b(?:клиент|регламент|договор|документ)\b", lowered)),
        closes_commitment=("state_change" in kinds
                           or bool(re.search(r"\bзакро(?:й|йте|ю|ывай)\b", lowered))),
        shifts_due=("defer" in kinds),
        records_decision=("decision" in kinds),
        changes_person_rule=(general_rule and bool(kinds & {"preference", "feedback", "request"})),
        creates_task=bool(re.search(r"\b(?:поставь|заведи|создай)\b.*\bзадач", lowered)),
        prepares_dossier=bool(re.search(r"\b(?:собери|подготовь)\b.*\bдосье\b", lowered)),
        prepares_draft=bool(re.search(r"\b(?:сделай|подготовь|напиши)\b.*\bчерновик\b", lowered)),
        records_observation=("capture" in kinds or "fact" in kinds),
        saves_feedback=("feedback" in kinds),
        moves_own_proposal=bool(re.search(r"\bперенеси\b.*\b(?:предложение|совет)\b", lowered)),
        sets_reminder=bool(re.search(r"\b(?:напомни|поставь напоминание)\b", lowered)),
    )


def planned_transition(intents: Iterable[Intent]) -> PlannedTransition | None:
    """Явная отсрочка сильнее причины отказа: только defer хранит день возврата."""
    items = list(intents)
    refusal = next((one for one in items if one.kind == "refusal"), None)
    deferred = next((one for one in items if one.kind == "defer"), None)
    if deferred is not None:
        reason = refusal.reason if refusal is not None else "человек назвал день возврата"
        return PlannedTransition("defer", reason, deferred.until)
    if refusal is not None and refusal.workflow_action:
        return PlannedTransition(refusal.workflow_action, refusal.reason)
    return None


def _spoken(intents: tuple[Intent, ...], resolution: refer.Resolution | None,
            transition: PlannedTransition | None, policy: PolicyDecision) -> str:
    if not intents:
        target = ""
        if resolution is not None and resolution.target is not None:
            target = f" по «{resolution.target.title}»"
        return f"Сохранил реплику дословно{target}; в работу ничего не переношу."
    if resolution is not None and resolution.confidence == refer.LOW:
        return resolution.question

    target = ""
    if resolution is not None and resolution.target is not None:
        target = f" по «{resolution.target.title}»"

    refusal = next((one for one in intents if one.kind == "refusal"), None)
    if refusal is not None:
        if not refusal.reason_kind:
            return f"Понял отказ{target}, но не понял причину. Почему это не берём?"
        action = {"wait": "ставлю в ожидание", "defer": "откладываю",
                  "dismiss": "больше не поднимаю"}[transition.action]
        until = ""
        if transition.until is not None:
            until = f" до {transition.until:%d.%m.%Y}"
        return f"Понял: {action}{target}{until}; причина — {refusal.reason}. Так?"

    labels = ", ".join(dict.fromkeys(one.label for one in intents)) or "смысл не распознан"
    if resolution is not None and resolution.confidence == refer.MEDIUM:
        return f"Я понял{target}: {labels}. Верно?"
    if policy.level in {PROPOSE_AND_WAIT, ALWAYS_ASK}:
        return f"Я понял{target}: {labels}. Так?"
    if policy.level == DO_AND_TELL:
        return f"Делаю{target}: {labels}; затем покажу результат."
    return f"Записываю{target}: {labels}."


def interpret(root: Path, utterance: str, notes: list[store.Note], *,
              now: dt.datetime | None = None,
              calendar_events: Iterable[object] | None = None,
              recent_notes: Iterable[store.Note] | None = None,
              known_target: str | None = None) -> Interpretation:
    moment = now or dt.datetime.now()
    intents = tuple(classify(utterance, today=moment.date()))
    resolution = None
    context_notes = list(recent_notes or [])
    if known_target is not None:
        by_rel = {one.rel: one for one in notes + context_notes}
        target = by_rel.get(known_target)
        if target is None:
            raise ValueError(f"поправленная цель не найдена в складе: {known_target}")
        resolution = refer.decide(
            utterance, [refer.Candidate(target, "человек назвал в поправке", 0.95)])
    elif (refer.has_explicit_reference(utterance, notes + context_notes)
          or (intents and refer.has_pointer(utterance))):
        resolution = refer.resolve_reference(
            root, utterance, notes, moment, calendar_events=calendar_events,
            recent_notes=context_notes)
    transition = planned_transition(intents)
    policy = confirmation_policy(infer_action(utterance, intents))
    return Interpretation(utterance, intents, resolution, transition, policy,
                          _spoken(intents, resolution, transition, policy))


def refusal_transition(intent: Intent) -> tuple[str, str]:
    if intent.kind != "refusal":
        raise ValueError("нужна отрицательная реакция")
    if not intent.reason_kind or not intent.reason or not intent.workflow_action:
        raise ValueError("голый отказ не записывается — нужна причина")
    return intent.workflow_action, f"{intent.reason_kind}: {intent.reason}"


def record_refusal(root: Path, intent: Intent, target: str, *,
                   now: dt.datetime | None = None) -> None:
    action, reason = refusal_transition(intent)
    activity.append(root, ["реакция", action, target, reason], now=now)


def confirm_reference(root: Path, interpretation: Interpretation, *,
                      actual_target: str | None = None,
                      now: dt.datetime | None = None) -> None:
    if interpretation.reference is None:
        return
    refer.record_outcome(root, interpretation.reference, "человек подтвердил",
                         actual_target=actual_target, now=now)


def _correction_payload(utterance: str) -> str:
    match = re.search(
        r"(?:ты\s+)?неправильно\s+понял\s*[:;,—-]?\s*(.*)$",
        utterance, flags=re.IGNORECASE)
    return match.group(1).strip() if match else ""


def correct(root: Path, previous: Interpretation, utterance: str,
            notes: list[store.Note], *, actual_target: str | None = None,
            now: dt.datetime | None = None,
            calendar_events: Iterable[object] | None = None,
            recent_notes: Iterable[store.Note] | None = None) -> CorrectionResult:
    """Отменяет прошлую догадку, сохраняет поправку и разбирает остаток снова."""
    moment = now or dt.datetime.now()
    context_notes = list(recent_notes or [])
    corrections = [one for one in classify(utterance, today=moment.date())
                   if one.kind == "correction"]
    if not corrections:
        raise ValueError("в реплике нет поправки «ты неправильно понял»")
    if actual_target is not None:
        known = {one.rel for one in notes + context_notes}
        if actual_target not in known:
            raise ValueError(f"поправленная цель не найдена в складе: {actual_target}")
    previous.cancelled = True
    target = (previous.reference.target.rel
              if previous.reference is not None and previous.reference.target is not None
              else "предыдущая интерпретация")
    activity.append(root, ["поправка", target, utterance], now=moment)
    if previous.reference is not None:
        refer.record_outcome(root, previous.reference, "человек поправил",
                             actual_target=actual_target, now=moment)

    payload = _correction_payload(utterance)
    replacement = None
    if payload:
        replacement = interpret(root, payload, notes, now=moment,
                                calendar_events=calendar_events,
                                recent_notes=context_notes,
                                known_target=actual_target)
    return CorrectionResult(True, corrections[0], replacement)


def render(result: Interpretation) -> str:
    rows = [result.spoken, f"Подтверждение: {result.policy.level} — {result.policy.why}"]
    if result.intents:
        rows.append("Намерения: " + ", ".join(one.label for one in result.intents))
    if result.reference is not None:
        rows.append(f"Ссылка: {result.reference.confidence} — {result.reference.action}")
    if result.transition is not None:
        until = f" до {result.transition.until:%Y-%m-%d}" if result.transition.until else ""
        rows.append(f"Переход после подтверждения: {result.transition.action}{until}")
    return "\n".join(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="разобрать живую реплику")
    parser.add_argument("utterance", help="реплика человека дословно")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    root = args.root.resolve()
    loaded = store.load(root, "work", "raw")
    if loaded.unreadable:
        print(loaded.complain())
        return 1
    work = [one for one in loaded.notes if one.rel.startswith("work/")]
    recent = [one for one in loaded.notes if one.rel.startswith("raw/")
              and one.type in {"meeting", "interview"}]
    print(render(interpret(root, args.utterance, work, recent_notes=recent)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
