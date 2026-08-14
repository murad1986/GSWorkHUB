#!/usr/bin/env python3
"""Расшифровка голосового сырья Deepgram без SDK и второй схемы данных."""

from __future__ import annotations

import json
import mimetypes
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

import store

ENDPOINT = "https://api.deepgram.com/v1/listen"
MODEL = "nova-3"
LANGUAGE = "ru"
MAX_KEYTERMS = 100
TIMEOUT = 90
TERM_TYPES = {"person", "client", "program", "process", "entity", "concept"}


class TranscriptionError(RuntimeError):
    """Расшифровка не получена; ключ и содержимое ответа наружу не выходят."""


@dataclass(frozen=True)
class Transcript:
    text: str
    confidence: float | None = None
    request_id: str = ""


def _values(note: store.Note) -> Iterable[str]:
    yield note.title
    for field in ("name", "org", "prefix"):
        value = note.data.get(field)
        if value:
            yield str(value)
    aliases = note.data.get("aliases") or []
    if isinstance(aliases, list):
        yield from (str(value) for value in aliases)


def keyterms(notes: Iterable[store.Note], limit: int = MAX_KEYTERMS) -> list[str]:
    """Имена, компании и устойчивые термины склада для Nova-3.

    Deepgram принимает не больше ста отдельных `keyterm`; порядок здесь —
    порядок ценности: люди и контейнеры раньше сводных понятий.
    """
    ranked = sorted(
        (note for note in notes if note.type in TERM_TYPES),
        key=lambda note: (0 if note.type == "person" else
                          1 if note.type in {"client", "program"} else 2,
                          note.rel),
    )
    out: list[str] = []
    seen: set[str] = set()
    for note in ranked:
        for raw in _values(note):
            term = " ".join(raw.split()).strip(" ·—-_")
            marker = term.casefold()
            if len(term) < 2 or marker in seen:
                continue
            seen.add(marker)
            out.append(term)
            if len(out) >= limit:
                return out
    return out


def request_url(terms: Iterable[str]) -> str:
    params: list[tuple[str, str]] = [
        ("model", MODEL),
        ("language", LANGUAGE),
        ("smart_format", "true"),
        ("punctuate", "true"),
    ]
    params.extend(("keyterm", term) for term in list(terms)[:MAX_KEYTERMS])
    return ENDPOINT + "?" + urllib.parse.urlencode(params)


def _request(url: str, headers: dict[str, str], body: bytes,
             timeout: int) -> bytes:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise TranscriptionError(f"Deepgram отказал: HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TranscriptionError("Deepgram не ответил") from exc


def transcribe(audio: bytes, *, api_key: str, content_type: str = "audio/ogg",
               terms: Iterable[str] = (), timeout: int = TIMEOUT,
               requester: Callable[[str, dict[str, str], bytes, int], bytes] = _request,
               ) -> Transcript:
    if not api_key.strip():
        raise TranscriptionError("не задан DEEPGRAM_API_KEY")
    if not audio:
        raise TranscriptionError("голосовое сообщение пустое")
    headers = {
        "Authorization": f"Token {api_key.strip()}",
        "Content-Type": content_type or "application/octet-stream",
    }
    raw = requester(request_url(terms), headers, audio, timeout)
    try:
        payload = json.loads(raw)
        alternative = payload["results"]["channels"][0]["alternatives"][0]
        text = str(alternative["transcript"]).strip()
        confidence = alternative.get("confidence")
        request_id = str((payload.get("metadata") or {}).get("request_id") or "")
    except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError) as exc:
        raise TranscriptionError("ответ Deepgram не разобран") from exc
    if not text:
        raise TranscriptionError("Deepgram вернул пустую расшифровку")
    return Transcript(text, float(confidence) if confidence is not None else None,
                      request_id)


def content_type(path: Path, fallback: str = "audio/ogg") -> str:
    return mimetypes.guess_type(path.name)[0] or fallback
