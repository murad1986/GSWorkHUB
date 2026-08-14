#!/usr/bin/env python3
"""Telegram как разговорный вход в тот же workhub, а не отдельный помощник."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

import activity
import check_env
import deepgram
import dialogue
import store
import sync
import workflow
import yaml

ROOT = Path(__file__).resolve().parents[1]
INBOX = "raw/inbox"
CONTEXT_MINUTES = 120
POLL_SECONDS = 25
NETWORK_TIMEOUT = 35


class TelegramError(RuntimeError):
    """Telegram не выполнил действие; токен и адрес запроса не раскрываются."""


@dataclass(frozen=True)
class ReplyContext:
    state: str                         # active | expired | missing
    targets: tuple[str, ...] = ()
    stamp: dt.datetime | None = None


@dataclass(frozen=True)
class RawMessage:
    path: str
    kind: str
    text: str = ""
    original: str = ""


@dataclass(frozen=True)
class Action:
    command: str
    target: str
    reason: str = ""
    until: dt.date | None = None
    resolution: str = ""


@dataclass
class HandlerResult:
    response: str = ""
    targets: tuple[str, ...] = ()
    raw: RawMessage | None = None
    interpretation: dialogue.Interpretation | None = None
    pending: Action | None = None
    ignored: bool = False


def message_ref(chat_id: int, message_id: int) -> str:
    return f"telegram:{chat_id}:{message_id}"


def _json_request(url: str, payload: dict | None, timeout: int) -> dict:
    data = None
    headers: dict[str, str] = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers,
                                     method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        raise TelegramError(f"Telegram отказал: HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TelegramError("Telegram не ответил") from exc
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise TelegramError("ответ Telegram не разобран") from exc
    if not parsed.get("ok"):
        raise TelegramError("Telegram вернул отказ")
    return parsed


class TelegramAPI:
    def __init__(self, token: str,
                 requester: Callable[[str, dict | None, int], dict] = _json_request):
        if not token.strip():
            raise TelegramError("не задан TELEGRAM_BOT_TOKEN")
        self._token = token.strip()
        self._requester = requester

    def _url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self._token}/{method}"

    def call(self, method: str, payload: dict | None = None,
             timeout: int = NETWORK_TIMEOUT) -> object:
        try:
            return self._requester(self._url(method), payload, timeout)["result"]
        except KeyError as exc:
            raise TelegramError(f"в ответе {method} нет result") from exc

    def get_updates(self, offset: int | None = None,
                    timeout: int = POLL_SECONDS) -> list[dict]:
        payload: dict[str, object] = {
            "timeout": timeout,
            "allowed_updates": ["message"],
        }
        if offset is not None:
            payload["offset"] = offset
        result = self.call("getUpdates", payload, timeout + 10)
        return list(result) if isinstance(result, list) else []

    def get_file(self, file_id: str) -> str:
        result = self.call("getFile", {"file_id": file_id})
        if not isinstance(result, dict) or not result.get("file_path"):
            raise TelegramError("Telegram не вернул путь голосового файла")
        return str(result["file_path"])

    def download(self, file_path: str, timeout: int = NETWORK_TIMEOUT) -> bytes:
        quoted = urllib.parse.quote(file_path, safe="/")
        url = f"https://api.telegram.org/file/bot{self._token}/{quoted}"
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                return response.read()
        except (urllib.error.HTTPError, urllib.error.URLError,
                TimeoutError, OSError) as exc:
            raise TelegramError("голосовой файл Telegram не скачан") from exc

    def send_message(self, chat_id: int, text: str) -> int:
        result = self.call("sendMessage", {"chat_id": chat_id, "text": text})
        if not isinstance(result, dict) or "message_id" not in result:
            raise TelegramError("Telegram не вернул номер отправленного сообщения")
        return int(result["message_id"])


def _event_once(root: Path, event: str, first_part: str,
                row: list[object], *, now: dt.datetime) -> None:
    if any(entry.part(0) == first_part
           for entry in activity.read(root, events={event}, future_ok=True)):
        return
    activity.append(root, row, now=now)


def processed_updates(root: Path) -> set[int]:
    out: set[int] = set()
    for entry in activity.read(root, events={"telegram обработано"}, future_ok=True):
        try:
            out.add(int(entry.part(0)))
        except ValueError:
            continue
    return out


def next_offset(root: Path) -> int | None:
    done = processed_updates(root)
    return max(done) + 1 if done else None


def reply_context(root: Path, chat_id: int, reply_message_id: int, *,
                  now: dt.datetime | None = None,
                  minutes: int = CONTEXT_MINUTES) -> ReplyContext:
    moment = now or dt.datetime.now()
    wanted = message_ref(chat_id, reply_message_id)
    for entry in reversed(activity.read(root, events={"telegram отправлено"}, now=moment)):
        if entry.part(0) != wanted:
            continue
        targets = tuple(part for part in entry.parts[1:]
                        if part.startswith(("work/", "raw/")))
        state = "active" if entry.stamp >= moment - dt.timedelta(minutes=minutes) else "expired"
        return ReplyContext(state, targets, entry.stamp)
    return ReplyContext("missing")


def _safe_targets(root: Path, targets: Iterable[str]) -> tuple[str, ...]:
    safe: list[str] = []
    for target in targets:
        path = (root / target).resolve()
        try:
            path.relative_to(root.resolve())
        except ValueError:
            continue
        if path.is_file() and target.startswith(("work/", "raw/")):
            safe.append(target)
    return tuple(dict.fromkeys(safe))


def record_outgoing(root: Path, chat_id: int, message_id: int,
                    targets: Iterable[str], *, text: str = "",
                    now: dt.datetime | None = None) -> None:
    moment = now or dt.datetime.now().replace(microsecond=0)
    ref = message_ref(chat_id, message_id)
    linked = _safe_targets(root, targets)
    row: list[object] = ["telegram отправлено", ref, *(linked or ("без связи",))]
    if text:
        row.append("текст " + json.dumps(text, ensure_ascii=False))
    activity.append(root, row, now=moment)


def outgoing_text(root: Path, chat_id: int, message_id: int) -> str:
    """Точный ответ Telegram из дописываемого журнала."""
    wanted = message_ref(chat_id, message_id)
    for entry in reversed(activity.read(root, events={"telegram отправлено"},
                                        future_ok=True)):
        if entry.part(0) != wanted:
            continue
        for part in entry.parts[1:]:
            if not part.startswith("текст "):
                continue
            try:
                value = json.loads(part.removeprefix("текст "))
            except json.JSONDecodeError:
                return ""
            return value if isinstance(value, str) else ""
    return ""


def _stem(message: dict, moment: dt.datetime) -> str:
    chat_id = abs(int((message.get("chat") or {}).get("id") or 0))
    message_id = int(message.get("message_id") or 0)
    return f"{moment:%Y-%m-%d}-telegram-{chat_id}-{message_id}"


def message_time(message: dict, fallback: dt.datetime) -> dt.datetime:
    """Telegram date делает путь повтора стабильным даже через полночь."""
    try:
        return dt.datetime.fromtimestamp(int(message.get("date")))
    except (TypeError, ValueError, OSError, OverflowError):
        return fallback


def _write_once(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(content)
    except FileExistsError:
        if path.read_text(encoding="utf-8") != content:
            raise TelegramError(
                f"повтор Telegram расходится с уже записанным: {path.name}") from None


def _write_bytes_once(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as stream:
            stream.write(content)
    except FileExistsError:
        if path.read_bytes() != content:
            raise TelegramError(
                f"повтор голоса расходится с оригиналом: {path.name}") from None


def _source_document(message: dict, update_id: int, kind: str, text: str,
                     moment: dt.datetime, *, media: str = "") -> str:
    chat_id = int((message.get("chat") or {}).get("id") or 0)
    message_id = int(message.get("message_id") or 0)
    front = {
        "type": "source",
        "date": moment.date().isoformat(),
        "title": f"Telegram · {'голос' if kind == 'voice' else 'текст'}",
        "source": "telegram",
        "source_ref": message_ref(chat_id, message_id),
        "received_at": moment.replace(microsecond=0).isoformat(),
        "telegram_update": update_id,
        "telegram_chat": chat_id,
        "telegram_message": message_id,
    }
    if media:
        front["media"] = media
    heading = "Дословная расшифровка" if kind == "voice" else "Дословный текст"
    return ("---\n" + yaml.safe_dump(front, allow_unicode=True, sort_keys=False)
            + "---\n\n" + f"# {front['title']}\n\n## {heading}\n\n{text}\n")


def save_text(root: Path, update_id: int, message: dict,
              moment: dt.datetime) -> RawMessage:
    text = str(message.get("text") or "")
    if not text:
        raise TelegramError("в текстовом сообщении нет текста")
    path = root / INBOX / f"{_stem(message, moment)}.md"
    _write_once(path, _source_document(message, update_id, "text", text, moment))
    return RawMessage(str(path.relative_to(root)), "text", text)


def save_voice_original(root: Path, update_id: int, message: dict,
                        audio: bytes, extension: str,
                        moment: dt.datetime) -> RawMessage:
    suffix = extension.lower() if re.fullmatch(r"\.[a-z0-9]{2,5}", extension.lower()) else ".ogg"
    original = root / INBOX / f"{_stem(message, moment)}{suffix}"
    _write_bytes_once(original, audio)
    transcript = root / INBOX / f"{_stem(message, moment)}.md"
    return RawMessage(str(transcript.relative_to(root)), "voice", "",
                      str(original.relative_to(root)))


def save_voice_transcript(root: Path, update_id: int, message: dict,
                          raw: RawMessage, text: str,
                          moment: dt.datetime) -> RawMessage:
    path = root / raw.path
    _write_once(path, _source_document(message, update_id, "voice", text, moment,
                                       media=raw.original))
    return RawMessage(raw.path, "voice", text, raw.original)


def _direct_action(text: str, target: str, raw_path: str) -> Action | None:
    lowered = text.lower().strip()
    if re.search(r"\bберу\b", lowered):
        return Action("take", target)
    if (re.search(r"\b(?:сделано|готово|завершил[аи]?|уже сделали)\b", lowered)
            and re.search(r"\b(?:закрой|закрыл|закрывай|сделано|готово)\b", lowered)):
        return Action("finish", target, resolution=raw_path)
    if re.search(r"\b(?:ответ приш[её]л|продолжаю)\b", lowered):
        return Action("resume", target)
    if re.search(r"\bжду\b.*\b(?:ответ|решени|цифр|документ)", lowered):
        return Action("wait", target, reason=text)
    if re.search(r"\bотменяю\b", lowered):
        return Action("cancel", target, reason=text, resolution=raw_path)
    return None


def action_from_interpretation(result: dialogue.Interpretation,
                               target: str) -> Action | None:
    transition = result.transition
    if transition is None:
        return None
    return Action(transition.action, target, transition.reason, transition.until)


def execute_action(root: Path, action: Action, *, raw_path: str,
                   now: dt.datetime | None = None,
                   writer_active: Callable[[dt.datetime], bool] | None = None) -> str:
    moment = now or dt.datetime.now()
    active = writer_active(moment) if writer_active else sync.laptop_active(moment)
    if active:
        parked = sync.park_intent(
            f"После подтверждения Telegram: {action.command} {action.target}; "
            f"причина: {action.reason or 'не требуется'}; источник: {raw_path}",
            now=moment, root=root)
        return f"Записал намерение: за складом сейчас работают. Применю позже ({parked.name})."
    if action.command in workflow.STATE_ACTIONS:
        workflow.transition(root, action.command, action.target, on=moment.date(),
                            resolution=action.resolution or raw_path,
                            reason=action.reason, now=moment)
    elif action.command in workflow.FEEDBACK_ACTIONS:
        workflow.feedback(root, action.command, action.target, reason=action.reason,
                          until=action.until, now=moment)
    else:
        raise TelegramError(f"неизвестное подтверждённое действие: {action.command}")
    return f"Записал: {action.target} — {action.command}."


class Bot:
    def __init__(self, root: Path, api: TelegramAPI, *, allowed_user_id: int,
                 deepgram_key: str = "",
                 transcriber: Callable[..., deepgram.Transcript] = deepgram.transcribe,
                 now: Callable[[], dt.datetime] = dt.datetime.now,
                 writer_active: Callable[[dt.datetime], bool] | None = None):
        if not allowed_user_id:
            raise TelegramError("не задан TELEGRAM_ALLOWED_USER_ID")
        self.root = root.resolve()
        self.api = api
        self.allowed_user_id = allowed_user_id
        self.deepgram_key = deepgram_key
        self.transcriber = transcriber
        self.now = now
        self.writer_active = writer_active
        self.pending: dict[str, Action] = {}

    def _notes(self) -> list[store.Note]:
        loaded = store.load(self.root, "work", "wiki")
        raw = store.load(self.root, "raw")
        unreadable = loaded.unreadable + [one for one in raw.unreadable
                                         if one[0] != "raw/log.md"]
        if unreadable:
            details = "; ".join(f"{rel}: {why}" for rel, why in unreadable[:5])
            raise TelegramError(f"склад прочитан не полностью: {details}")
        return loaded.notes + raw.notes

    def _reply(self, message: dict, moment: dt.datetime) -> ReplyContext:
        replied = message.get("reply_to_message") or {}
        if not replied.get("message_id"):
            return ReplyContext("missing")
        chat_id = int((message.get("chat") or {}).get("id") or 0)
        return reply_context(self.root, chat_id, int(replied["message_id"]), now=moment)

    def _receive_voice(self, update_id: int, message: dict,
                       moment: dt.datetime) -> tuple[RawMessage, str]:
        voice = message.get("voice") or {}
        file_id = str(voice.get("file_id") or "")
        if not file_id:
            raise TelegramError("в голосовом сообщении нет file_id")
        file_path = self.api.get_file(file_id)
        audio = self.api.download(file_path)
        extension = Path(file_path).suffix or ".ogg"
        raw = save_voice_original(self.root, update_id, message, audio, extension, moment)
        if not self.deepgram_key.strip():
            return raw, ""
        terms = deepgram.keyterms(self._notes())
        transcript = self.transcriber(
            audio, api_key=self.deepgram_key,
            content_type=deepgram.content_type(Path(file_path)), terms=terms)
        complete = save_voice_transcript(self.root, update_id, message, raw,
                                         transcript.text, moment)
        activity.append(self.root, ["голос расшифрован", complete.path,
                                    deepgram.MODEL, f"подсказок {len(terms)}"], now=moment)
        return complete, transcript.text

    def _interpret(self, text: str, raw: RawMessage, reply: ReplyContext,
                   moment: dt.datetime) -> HandlerResult:
        if reply.state == "expired":
            return HandlerResult(
                "Связь с тем сообщением устарела. О чём именно речь?",
                raw=raw)
        if reply.state == "active" and len(reply.targets) > 1:
            return HandlerResult(
                "В том сообщении было несколько дел. Какое именно ты имеешь в виду?",
                raw=raw)

        target = reply.targets[0] if reply.state == "active" and reply.targets else ""
        notes = self._notes()
        result = dialogue.interpret(self.root, text, notes, now=moment,
                                    known_target=target or None)
        resolved = target
        if not resolved and result.reference is not None and result.reference.target is not None:
            resolved = result.reference.target.rel
        targets = (resolved,) if resolved else ()

        by_rel = {note.rel: note for note in notes}
        resolved_note = by_rel.get(resolved)
        if not result.intents:
            linked = f" Связал со «{resolved_note.title}»." if resolved_note else ""
            if raw.kind == "voice":
                compact = " ".join(text.split())
                preview = compact if len(compact) <= 500 else compact[:497] + "…"
                response = (f"Голос и расшифровку сохранил дословно.{linked}\n"
                            f"Расшифровка: «{preview}»\n"
                            "В работу ничего не переносил.")
            else:
                response = (f"Текст сохранил дословно.{linked} "
                            "В работу ничего не переносил.")
            return HandlerResult(response, targets, raw, result)
        direct = (_direct_action(text, resolved, raw.path)
                  if resolved_note is not None and resolved_note.type == "commitment"
                  else None)
        if direct is not None:
            response = execute_action(self.root, direct, raw_path=raw.path, now=moment,
                                      writer_active=self.writer_active)
            return HandlerResult(response, targets, raw, result)
        pending = action_from_interpretation(result, resolved) if resolved else None
        if (pending is not None and pending.command in workflow.STATE_ACTIONS
                and (resolved_note is None or resolved_note.type != "commitment")):
            pending = None
        return HandlerResult(result.spoken, targets, raw, result, pending)

    def handle(self, update: dict) -> HandlerResult:
        update_id = int(update.get("update_id") or 0)
        message = update.get("message") or {}
        sender = int((message.get("from") or {}).get("id") or 0)
        chat = message.get("chat") or {}
        chat_type = str(chat.get("type") or "")
        chat_id = int(chat.get("id") or 0)
        if (sender != self.allowed_user_id
                or (chat_type and chat_type != "private")
                or (chat_type == "private" and chat_id != self.allowed_user_id)):
            return HandlerResult(ignored=True)
        message_id = int(message.get("message_id") or 0)
        if not update_id or not chat_id or not message_id:
            raise TelegramError("обновление Telegram неполное")
        moment = self.now().replace(microsecond=0)
        source_moment = message_time(message, moment)
        reply = self._reply(message, moment)
        reply_message_id = int((message.get("reply_to_message") or {}).get("message_id") or 0)
        pending = self.pending.get(message_ref(chat_id, reply_message_id))

        if message.get("voice"):
            raw, text = self._receive_voice(update_id, message, source_moment)
            kind = "voice"
        elif message.get("text") is not None:
            raw = save_text(self.root, update_id, message, source_moment)
            text = raw.text
            kind = "text"
        else:
            return HandlerResult("Пока понимаю только текст и голос.")

        _event_once(
            self.root, "telegram получено", str(update_id),
            ["telegram получено", update_id, message_ref(chat_id, message_id), kind,
             raw.original or raw.path], now=moment)

        if kind == "text" and text.strip().lower() in {"/start", "/help"}:
            return HandlerResult(
                "Я на связи. Пришли текст или голос — сохраню оригинал, "
                "разберу реплику и назову, что требует подтверждения.",
                raw=raw)

        if kind == "voice" and not text:
            activity.append(self.root, ["голос ожидает расшифровки", raw.original,
                                        "не задан DEEPGRAM_API_KEY"], now=moment)
            return HandlerResult(
                "Голос сохранил без изменений. Для расшифровки не задан ключ Deepgram.",
                reply.targets, raw)

        intents = dialogue.classify(text, today=moment.date())
        if pending is not None and any(one.kind == "confirmation" for one in intents):
            response = execute_action(self.root, pending, raw_path=raw.path, now=moment,
                                      writer_active=self.writer_active)
            self.pending.pop(message_ref(chat_id, reply_message_id), None)
            return HandlerResult(response, (pending.target,), raw)

        return self._interpret(text, raw, reply, moment)

    def send_linked(self, chat_id: int, text: str, targets: Iterable[str] = (), *,
                    now: dt.datetime | None = None) -> int:
        message_id = self.api.send_message(chat_id, text)
        moment = now or self.now().replace(microsecond=0)
        record_outgoing(self.root, chat_id, message_id, targets, text=text, now=moment)
        return message_id

    def process(self, update: dict) -> HandlerResult:
        result = self.handle(update)
        update_id = int(update.get("update_id") or 0)
        message = update.get("message") or {}
        chat_id = int((message.get("chat") or {}).get("id") or 0)
        if result.response and not result.ignored:
            sent = self.send_linked(chat_id, result.response, result.targets)
            if result.pending is not None:
                self.pending[message_ref(chat_id, sent)] = result.pending
        _event_once(self.root, "telegram обработано", str(update_id),
                    ["telegram обработано", update_id,
                     "пропущено" if result.ignored else "ответ дан"],
                    now=self.now().replace(microsecond=0))
        return result

    def poll(self, *, once: bool = False) -> None:
        offset = next_offset(self.root)
        while True:
            updates = self.api.get_updates(offset, timeout=0 if once else POLL_SECONDS)
            for update in updates:
                update_id = int(update.get("update_id") or 0)
                if update_id in processed_updates(self.root):
                    offset = max(offset or 0, update_id + 1)
                    continue
                self.process(update)
                offset = update_id + 1
            if once:
                return
            if not updates:
                time.sleep(1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Telegram-интерфейс склада")
    parser.add_argument("action", choices=["poll", "check", "send"])
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--item", default="")
    parser.add_argument("--text", default="")
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)

    check_env.load_secrets(args.root / "config" / "secrets.env")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    deepgram_key = os.environ.get("DEEPGRAM_API_KEY", "").strip()
    try:
        allowed = int(os.environ.get("TELEGRAM_ALLOWED_USER_ID", "0") or 0)
    except ValueError:
        print("TELEGRAM_ALLOWED_USER_ID должен быть числом")
        return 2
    try:
        bot = Bot(args.root, TelegramAPI(token), allowed_user_id=allowed,
                  deepgram_key=deepgram_key)
        if args.action == "check":
            me = bot.api.call("getMe")
            name = me.get("username", "без имени") if isinstance(me, dict) else "без имени"
            print(f"Telegram отвечает: @{name}; Deepgram: {'есть' if deepgram_key else 'НЕТ ключа'}")
            return 0
        if args.action == "send":
            if not args.item or not args.text:
                raise TelegramError("для send нужны --item и --text")
            bot.send_linked(allowed, args.text, [args.item])
            print("сообщение отправлено и связано с позицией")
            return 0
        bot.poll(once=args.once)
    except (TelegramError, deepgram.TranscriptionError,
            workflow.WorkflowError, sync.SyncError) as exc:
        print(f"Telegram-интерфейс остановлен: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
