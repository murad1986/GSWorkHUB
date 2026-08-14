#!/usr/bin/env python3
"""Коннектор Granola → зона приёма raw/inbox/.

Зачем. Расшифровки исчезают вместе с подпиской Granola, поэтому склад хранит их
у себя (architecture-v3, §про источники). Коннектор забирает встречи по API и
кладёт сырьё в зону приёма: сводку и полную расшифровку. Поля разбора —
контейнер, участники — он не проставляет: это суждение, а не факт из записи
(storage-schema, «чего нельзя требовать на входе»).

Три исхода прихода. Новое — записать. Правка записанного — запрещена: коннектор
никогда не открывает существующий файл. Повтор — отбросить: разговор узнаётся по
паре source=granola + source_ref=<id заметки>, и если она уже есть в raw/,
приход отбрасывается молча. Ранние записи ссылались на Granola по названию
встречи, а не по id, — их коннектор тоже узнаёт и не дублирует.

Ключ. Берётся из переменной GRANOLA_API_KEY, иначе из Keychain
(security find-generic-password -s granola-api). В складе ключ не лежит.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import store

API = "https://public-api.granola.ai/v1"
ROOT = Path(__file__).resolve().parents[1]
INBOX = ROOT / "raw" / "inbox"

# Пауза между запросами: предел Granola — 5 запросов в секунду.
PAUSE = 0.25

TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def api_key() -> str:
    key = os.environ.get("GRANOLA_API_KEY", "").strip()
    if key:
        return key
    result = subprocess.run(
        ["security", "find-generic-password", "-s", "granola-api", "-w"],
        capture_output=True, text=True, check=False,
    )
    key = result.stdout.strip()
    if not key:
        sys.exit("нет ключа Granola: положи его в Keychain "
                 "(security add-generic-password -s granola-api -a workhub -w <ключ>) "
                 "или в переменную GRANOLA_API_KEY")
    return key


def api_get(key: str, path: str, **params: str) -> dict:
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v})
    url = f"{API}{path}" + (f"?{query}" if query else "")
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", "replace")[:300]
        if error.code == 401:
            sys.exit("Granola не принял ключ (401): ключ отозван или сменился тариф")
        sys.exit(f"Granola ответил {error.code} на {path}: {body}")


def slugify(title: str) -> str:
    letters = "".join(TRANSLIT.get(ch, ch) for ch in title.lower())
    slug = re.sub(r"[^a-z0-9]+", "-", letters).strip("-")
    return slug[:60].rstrip("-") or "bez-nazvaniya"


def local_date(stamp: str) -> dt.date:
    moment = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    return moment.astimezone().date()


UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                  re.IGNORECASE)


def variants(value: str) -> set[str]:
    """Все имена, под которыми может быть записан один и тот же разговор."""
    value = (value or "").strip()
    if not value:
        return set()
    out = {value, value.removeprefix("granola:").strip()}
    out.update(match.group(0).lower() for match in UUID.finditer(value))
    return {one for one in out if one}


def known_refs() -> set[str]:
    """Всё, чем разговор Granola может быть назван в уже лежащих записях.

    Один и тот же разговор носит два разных имени: `not_cJDzFNKBPiIoK3` от
    коннектора и `granola:7617f751-…` у записей, заведённых руками из ссылки на
    заметку. 7 августа это стоило двух копий встреч 6 августа: пара
    source+source_ref совпасть не могла, сравнивать было нечем.

    Поэтому в набор известного идёт всё: сам `source_ref`, его чистый вид без
    приставки, адрес заметки и вытащенный из него идентификатор. Регистр
    источника тоже не различается — «Granola» и «granola» это один источник.
    """
    refs: set[str] = set()
    loaded = store.load(ROOT, "raw")
    for note in loaded.notes:
        if str(note.data.get("source") or "").strip().lower() != "granola":
            continue
        for field in ("source_ref", "granola_url"):
            refs |= variants(str(note.data.get(field) or ""))
    return refs


def is_known(note: dict, refs: set[str]) -> bool:
    """Сверка нормализует обе стороны: набор мог быть собран не нами."""
    title = (note.get("title") or "").strip()
    candidates = {note["id"], title, f"Granola: {title}"}
    candidates |= variants(str(note.get("web_url") or ""))
    known: set[str] = set()
    for ref in refs:
        known |= variants(ref)
    return bool(candidates & known)


def speaker_label(speaker: dict, me: str) -> str:
    if speaker.get("name"):
        return speaker["name"]
    attribution = speaker.get("attribution")
    if attribution == "me":
        return me
    if attribution == "them":
        return "Собеседник"
    return speaker.get("diarization_label") or "Голос"


def render_transcript(segments: list[dict], me: str) -> str:
    """Реплики подряд от одного голоса склеиваются: расшифровка Granola режет
    речь на куски по паузам, и без склейки файл нечитаем."""
    lines: list[str] = []
    last = None
    for segment in segments:
        label = speaker_label(segment.get("speaker") or {}, me)
        text = (segment.get("text") or "").strip()
        if not text:
            continue
        if label == last:
            lines[-1] += " " + text
        else:
            lines.append(f"**{label}:** {text}")
            last = label
    return "\n\n".join(lines)


def render(note: dict) -> str:
    title = (note.get("title") or "Без названия").strip()
    date = local_date(note["created_at"])
    me = (note.get("owner") or {}).get("name") or "Я"
    attendees = [person.get("name") or person.get("email") or "?"
                 for person in note.get("attendees") or []]

    head = [
        "---",
        "type: meeting",
        f"date: {date}",
        f"title: {json.dumps(title, ensure_ascii=False)}",
        "source: granola",
        f'source_ref: "{note["id"]}"',
    ]
    if attendees:
        listed = ", ".join(json.dumps(a, ensure_ascii=False) for a in attendees)
        head.append(f"attendees: [{listed}]")
    if note.get("web_url"):
        head.append(f'granola_url: "{note["web_url"]}"')
    head.append("---")

    body = [f"# {title}", ""]
    summary = (note.get("summary_markdown") or note.get("summary_text") or "").strip()
    if summary:
        body += ["## Сводка Granola", "", summary, ""]
    transcript = render_transcript(note.get("transcript") or [], me)
    if transcript:
        body += ["## Расшифровка", "", transcript, ""]
    if not summary and not transcript:
        body += ["Granola не вернул ни сводки, ни расшифровки.", ""]
    return "\n".join(head) + "\n\n" + "\n".join(body)


def target_path(date: dt.date, title: str) -> Path:
    base = f"{date}-{slugify(title)}"
    path = INBOX / f"{base}.md"
    counter = 2
    while path.exists():
        path = INBOX / f"{base}-{counter}.md"
        counter += 1
    return path


def list_notes(key: str, since: str) -> list[dict]:
    notes: list[dict] = []
    cursor = ""
    while True:
        page = api_get(key, "/notes", created_after=since, cursor=cursor, page_size="30")
        notes += page.get("notes") or []
        cursor = page.get("cursor") or ""
        if not page.get("hasMore"):
            return notes
        time.sleep(PAUSE)


def main() -> None:
    parser = argparse.ArgumentParser(description="Забрать встречи Granola в зону приёма")
    parser.add_argument("--since", default=None,
                        help="с какой даты забирать (ГГГГ-ММ-ДД); по умолчанию 7 дней назад")
    parser.add_argument("--dry-run", action="store_true",
                        help="показать, что было бы записано, ничего не записывая")
    arguments = parser.parse_args()
    since = arguments.since or str(dt.date.today() - dt.timedelta(days=7))

    key = api_key()
    refs = known_refs()
    notes = sorted(list_notes(key, since), key=lambda n: n["created_at"])

    taken, skipped = [], 0
    for note in notes:
        if is_known(note, refs):
            skipped += 1
            continue
        if arguments.dry_run:
            taken.append((note, None))
            continue
        time.sleep(PAUSE)
        detail = api_get(key, f"/notes/{note['id']}", include="transcript")
        # Вторая сверка — уже со ссылкой на заметку. В списке `web_url` не
        # приходит, он есть только в подробностях, поэтому первая сверка знает
        # лишь id и название. Записи, заведённые руками из ссылки, помнят
        # разговор по идентификатору внутри адреса — без этой проверки они
        # опознаются только на бумаге: 8 августа в 09:02 расписание завело две
        # копии встреч 6 августа, хотя сверка по адресу была написана в 03:31.
        if is_known(detail, refs):
            skipped += 1
            continue
        date = local_date(detail["created_at"])
        path = target_path(date, detail.get("title") or "Без названия")
        text = render(detail)
        path.write_text(text, encoding="utf-8")
        # Запись перечитывается, а не считается удавшейся по факту вызова: в
        # первом же живом прогоне две записи из семнадцати не оказались на диске,
        # а коннектор отчитался обо всех. Отчёт об успехе без данных — тот самый
        # класс отказа, ради которого написан verify.
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            sys.exit(f"запись не легла на диск: {path.relative_to(ROOT)} — "
                     f"остановился, чтобы отчёт не разошёлся со складом")
        refs.add(note["id"])
        taken.append((note, path))

    verb = "пришло бы" if arguments.dry_run else "принято"
    print(f"Granola с {since}: {verb} {len(taken)}, повторов отброшено {skipped}.")
    for note, path in taken:
        date = local_date(note["created_at"])
        where = f" → {path.relative_to(ROOT)}" if path else ""
        print(f"  · {date} «{note.get('title') or 'Без названия'}»{where}")


if __name__ == "__main__":
    main()
