#!/usr/bin/env python3
"""Проверка проверки: линтер обязан ловить то, ради чего написан.

Зачем это существует. В предыдущей системе гейт печатал «нарушений нет»,
провалидировав ноль заметок, и был зелёным независимо от состояния данных
(пункт 14 в docs/known-failure-modes.md). Здесь линтер прогоняется по складам,
собранным специально сломанными, и каждый случай обязан быть поймал.

Если этот файл зелёный, а lint.py при этом ничего не проверяет — такого быть не
может: последний случай проверяет именно вакуум.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import inspect
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

import activity
import advice as advice_mod
import agenda
import atlas
import capture
import attention
import capacity
import deepgram
import dialogue
import duty
import harvest
import index as index_mod
import intake as intake_mod
import interventions
import interview
import lint
import local_sync
import policy as policy_mod
import reading_map
import refer
import reflect
import skill_map
import source_sync
import store as store_mod
import sync
import telegram_bot
import ticktick
import today as today_mod
import tracker
import welcome
import workflow


class CaseCountingOutput:
    """Поток, который считает реально напечатанные успешные случаи.

    Проверки в этом файле намеренно неоднородны: часть живёт в словарях, часть
    стоит отдельными блоками. Поэтому источник числа — не устройство кода, а
    стандартные строки результата, которые действительно дошли до вывода.
    """

    def __init__(self, stream) -> None:
        self.stream = stream
        self.successes = 0
        self._pending = ""

    def write(self, value: str) -> int:
        combined = self._pending + value
        lines = combined.splitlines(keepends=True)
        self._pending = ""
        for line in lines:
            if line.endswith(("\n", "\r")):
                if line.rstrip("\r\n").startswith("✓ "):
                    self.successes += 1
            else:
                self._pending = line
        return self.stream.write(value)

    def flush(self) -> None:
        self.stream.flush()

    def __getattr__(self, name: str):
        return getattr(self.stream, name)


OK_NOTE = """---
type: person
name: Проверочный
---

# Проверочный
"""

SOURCE = """---
type: source
date: 2026-01-01
title: "Источник"
---

# источник
"""

DECISION = """---
type: decision
key: X-D-0
status: accepted
date: 2026-01-01
evidence: [raw/sources/2026-01-01-s.md]
---

# d
"""

SKILL = """---
type: skill
key: ASR-K-S01
skill_id: S01
title: "System Framing"
domains: [systems-thinking]
node: atomic
roles: [core]
target: 4
status: named
---

# S01. System Framing
"""

READING = """---
type: reading
key: ME-L-1
title: "Thinking in Systems"
author: "Донелла Медоуз"
kind: book
topics: [системное-мышление]
tier: ядро
status: queued
source: raw/sources/2026-01-01-s.md
---

# Thinking in Systems
"""


def reading(**fields: str) -> str:
    """Карточка чтения с изменёнными полями шапки. Тело — отдельным ключом `body`."""
    body = fields.pop("body", "")
    head = READING.split("---")[1].strip().splitlines()
    known = {line.split(":", 1)[0]: line for line in head}
    for name, value in fields.items():
        known[name] = f"{name}: {value}"
    title = known.get("title", 'title: "Книга"').split(": ", 1)[1].strip('"')
    return "---\n" + "\n".join(known.values()) + f"\n---\n\n# {title}\n{body}"


# случай → (файлы, ожидаемый фрагмент сообщения)
CASES: dict[str, tuple[dict[str, str], str]] = {
    "mirror указывает наружу и назван в теле — так и надо": (
        {"work/people/a.md":
            "---\ntype: person\nname: a\nmirror: https://example.com/live\n---\n\n"
            "# a\n\nЖивая версия: https://example.com/live"},
        None,
    ),
    "mirror есть, но в теле его нет — читатель не узнает про живую версию": (
        {"work/people/a.md":
            "---\ntype: person\nname: a\nmirror: https://example.com/live\n---\n\n# a"},
        "не назван в теле",
    ),
    "mirror путём внутрь склада — это не живая копия, а вторая правда": (
        {"work/people/a.md":
            "---\ntype: person\nname: a\nmirror: work/people/a.md\n---\n\n"
            "# a\n\nwork/people/a.md"},
        "не внешний адрес",
    ),
    "mirror пустой — указатель, которого нет": (
        {"work/people/a.md":
            "---\ntype: person\nname: a\nmirror: \"\"\n---\n\n# a"},
        "не внешний адрес",
    ),
    # Контакты человека. Заведены 9 августа 2026: телефон из письма жить в складе
    # было негде, и разбор почты не мог узнать отправителя.
    "человек с почтой, телефоном и мессенджером — так и надо": (
        {"work/people/a.md":
            "---\ntype: person\nname: a\nemail: [a@b.ru]\nphone: [\"+7 900 000-00-07\"]\n"
            "messengers: {telegram: \"@a\"}\ngoogle_contact: people/c1\n---\n\n# a"},
        None,
    ),
    "почта без собаки почтой не является": (
        {"work/people/a.md":
            "---\ntype: person\nname: a\nemail: [не-адрес]\n---\n\n# a"},
        "не похоже на адрес почты",
    ),
    "в номере семь цифр — по такому не позвонишь": (
        {"work/people/a.md":
            "---\ntype: person\nname: a\nphone: [\"444-44-07\"]\n---\n\n# a"},
        "не похоже на номер телефона",
    ),
    "один адрес у двух людей — кто-то заведён дважды": (
        {"work/people/a.md": "---\ntype: person\nname: a\nemail: [x@b.ru]\n---\n\n# a",
         "work/people/b.md": "---\ntype: person\nname: b\nemail: [x@b.ru]\n---\n\n# b"},
        "стоит у work/people/a.md",
    ),
    "тот же номер в разной записи — это один номер, а не два": (
        {"work/people/a.md": "---\ntype: person\nname: a\nphone: [\"+7 900 000-00-07\"]\n---\n\n# a",
         "work/people/b.md": "---\ntype: person\nname: b\nphone: [\"89000000007\"]\n---\n\n# b"},
        "стоит у work/people/a.md",
    ),
    "почта строкой вместо списка — у человека адресов бывает несколько": (
        {"work/people/a.md": "---\ntype: person\nname: a\nemail: a@b.ru\n---\n\n# a"},
        "должно быть списком",
    ),
    "мессенджер без имени — поле, по которому не написать": (
        {"work/people/a.md":
            "---\ntype: person\nname: a\nmessengers: {telegram: \"\"}\n---\n\n# a"},
        "без имени",
    ),
    "связка с адресной книгой не того вида": (
        {"work/people/a.md":
            "---\ntype: person\nname: a\ngoogle_contact: c123\n---\n\n# a"},
        "не ссылается на запись адресной книги",
    ),
    "документ не проверяется по схеме, но ссылки в нём — да": (
        {"work/people/a.md": OK_NOTE, "docs/b.md": "[в никуда](../nope.md)"},
        "ссылка в никуда",
    ),
    "файл вне трёх корней — нарушение инварианта сорта": (
        {"work/people/a.md": OK_NOTE, "somewhere/b.md": "# заметка сбоку"},
        "вне трёх корней",
    ),
    "контракт и своды правил ролей вне корней жить имеют право": (
        {"work/people/a.md": OK_NOTE, "AGENTS.md": "# контракт", "agents/tracker.md": "# трекер"},
        None,
    ),
    "шапка не разбирается — причина называется": (
        {"work/people/a.md": "---\ntype: person\nname: [не закрыт\n---\n\n# a"},
        "не разбирается",
    ),
    "нет шапки вовсе": (
        {"work/people/a.md": "# просто заголовок"},
        "нет шапки",
    ),
    "пустая шапка — тоже причина, а не заметка": (
        {"work/people/a.md": "---\n---\n\n# a"},
        "пуста",
    ),
    "вопрос открыт при указанном разрешении — нарушение": (
        {"raw/sources/2026-01-01-s.md": SOURCE,
         "work/clients/x/decisions/d.md": DECISION.replace("X-D-0", "X-D-1"),
         "work/clients/x/questions/q.md":
            "---\ntype: question\nkey: X-Q-1\ntitle: q\nstatus: open\n"
            "origin: work/clients/x/questions/q.md\n"
            "resolution: work/clients/x/decisions/d.md\n---\n\n# q"},
        "открыт, хотя указано разрешение",
    ),
    "открытый вопрос БЕЗ явной связи с решением не трогаем": (
        {"raw/sources/2026-01-01-s.md": SOURCE,
         "work/clients/x/decisions/d.md": DECISION.replace("X-D-0", "X-D-2"),
         "work/clients/x/questions/q.md":
            "---\ntype: question\nkey: X-Q-2\ntitle: q\nstatus: open\n"
            "origin: work/clients/x/questions/q.md\n---\n\n# q"},
        None,
    ),
    "принятое решение без доказательства — нарушение": (
        {"work/clients/x/decisions/d.md":
            "---\ntype: decision\nkey: X-D-3\nstatus: accepted\ndate: 2026-01-01\n"
            "evidence: []\n---\n\n# d"},
        "без evidence",
    ),
    "доказательство обязано быть событием, а не нашей же позицией": (
        {"work/clients/x/decisions/d.md":
            "---\ntype: decision\nkey: X-D-4\nstatus: accepted\ndate: 2026-01-01\n"
            "evidence: [work/clients/x/decisions/d.md]\n---\n\n# d"},
        "доказательством может быть только событие",
    ),
    "обязательство без владельца — нарушение": (
        {"raw/sources/2026-01-01-s.md": SOURCE,
         "work/clients/x/commitments/c.md":
            "---\ntype: commitment\nkey: X-C-1\ndirection: outbound\nstatus: open\n"
            "level: next\norigin: raw/sources/2026-01-01-s.md\n---\n\n# c"},
        "без владельца",
    ),
    "«сейчас» без срока и без зацепки — начать нечем": (
        {"raw/sources/2026-01-01-s.md": SOURCE,
         "work/clients/x/commitments/c.md":
            "---\ntype: commitment\nkey: X-C-2\ndirection: outbound\nstatus: open\n"
            "level: now\nowner: ivan\norigin: raw/sources/2026-01-01-s.md\n---\n\n# c"},
        "начать нечем",
    ),
    "«сейчас» с зацепкой претензий не вызывает": (
        {"raw/sources/2026-01-01-s.md": SOURCE,
         "work/clients/x/commitments/c.md":
            "---\ntype: commitment\nkey: X-C-3\ndirection: outbound\nstatus: open\n"
            "level: now\nowner: ivan\norigin: raw/sources/2026-01-01-s.md\n"
            "when_then:\n  cue: \"сяду за проект\"\n  action: \"сделаю шаг\"\n---\n\n# c"},
        None,
    ),
    "строка вместо списка не перебирается посимвольно": (
        {"work/people/a.md": "---\ntype: person\nname: Имя\nevidence: raw/a.md\n---\n\n# a"},
        "должно быть списком",
    ),
    "неизвестный тип": (
        {"work/people/a.md": "---\ntype: nonsense\n---\n\n# a"},
        "неизвестный тип",
    ),
    "нет обязательного поля": (
        {"work/clients/x/questions/a.md": "---\ntype: question\nkey: X-Q-1\nstatus: open\n---\n\n# a"},
        "нет обязательного поля",
    ),
    "статус вне словаря": (
        {"work/people/a.md": OK_NOTE,
         "work/programs/p/program.md": "---\ntype: program\nmode: летающий\nprefix: P\n---\n\n# p"},
        "вне словаря",
    ),
    "стадия этапа вне словаря": (
        {"work/people/a.md": OK_NOTE,
         "work/clients/x/client.md": "---\ntype: client\nmode: active\nprefix: X\n---\n\n# x",
         "work/clients/x/engagement-a.md":
            "---\ntype: engagement\nclient: work/clients/x/client.md\n"
            "stage: летающий\n---\n\n# a"},
        "вне словаря",
    ),
    "предложенный этап — законное состояние, а не опечатка": (
        {"work/people/a.md": OK_NOTE,
         "work/clients/x/client.md": "---\ntype: client\nmode: active\nprefix: X\n---\n\n# x",
         "work/clients/x/engagement-a.md":
            "---\ntype: engagement\nclient: work/clients/x/client.md\n"
            "stage: proposed\n---\n\n# a"},
        None,
    ),
    "дубликат ключа между контейнерами": (
        {"work/programs/a/questions/q.md":
            "---\ntype: question\nkey: DUP-1\ntitle: a\nstatus: open\norigin: work/programs/a/questions/q.md\n---\n\n# a",
         "work/programs/b/questions/q.md":
            "---\ntype: question\nkey: DUP-1\ntitle: b\nstatus: open\norigin: work/programs/b/questions/q.md\n---\n\n# b"},
        "уже занят",
    ),
    "имя не машиночитаемое": (
        {"work/people/Иван Петров.md": OK_NOTE},
        "имя не машиночитаемое",
    ),
    "событие без даты в имени": (
        {"raw/meetings/vstrecha.md":
            '---\ntype: meeting\ndate: 2026-07-24\ncontainer: work/x\nsource_ref: "s"\n---\n\n# m'},
        "ГГГГ-ММ-ДД",
    ),
    "вид без пометки о генерации": (
        {"wiki/attention.md": "---\ntype: attention\ngenerated: false\n---\n\n# a"},
        "вид без generated: true",
    ),
    "поле-путь указывает в никуда": (
        {"work/clients/x/risks/r.md":
            "---\ntype: risk\nkey: X-R-1\nstatus: open\norigin: raw/meetings/2026-01-01-nope.md\n---\n\n# r"},
        "origin указывает в никуда",
    ),
    "ссылка в списке указывает в никуда": (
        {"work/clients/x/decisions/d.md":
            "---\ntype: decision\nkey: X-D-1\nstatus: accepted\ndate: 2026-01-01\nevidence: [raw/sources/2026-01-01-nope.md]\n---\n\n# d"},
        "ссылка в никуда",
    ),
    "заявленный контейнер не совпадает с расположением": (
        {"work/programs/a/questions/q.md":
            "---\ntype: question\nkey: A-Q-1\ntitle: q\nstatus: open\n"
            "origin: work/programs/a/questions/q.md\ncontainer: work/programs/b\n---\n\n# q"},
        "не совпадает с расположением",
    ),
    "вид объявлен, но ни разу не собирался — это замечание, не нарушение": (
        {"wiki/attention.md": "---\ntype: attention\ngenerated: true\n---\n\n# a"},
        "никогда не собирался",
    ),
    "в работе без даты начала — потерян рабочий цикл": (
        {"work/programs/p/commitments/c.md":
            "---\ntype: commitment\nkey: P-C-1\ntitle: Дело\ndirection: outbound\n"
            "status: in-progress\nowner: ivan\nlevel: next\n"
            "origin: work/programs/p/commitments/c.md\n---\n\n# c"},
        "без started",
    ),
    "в работе с датой начала — законное состояние": (
        {"work/programs/p/commitments/c.md":
            "---\ntype: commitment\nkey: P-C-1\ntitle: Дело\ndirection: outbound\n"
            "status: in-progress\nstarted: 2026-07-31\nowner: ivan\nlevel: next\n"
            "origin: work/programs/p/commitments/c.md\n---\n\n# c"},
        None,
    ),
    "начатый цикл нельзя закрыть без даты конца": (
        {"work/programs/p/commitments/c.md":
            "---\ntype: commitment\nkey: P-C-1\ntitle: Дело\ndirection: outbound\n"
            "status: resolved\nstarted: 2026-07-30\n"
            "origin: work/programs/p/commitments/c.md\n---\n\n# c"},
        "без resolved",
    ),
    "историческое закрытие без выдуманных дат остаётся допустимым": (
        {"work/programs/p/commitments/c.md":
            "---\ntype: commitment\nkey: P-C-1\ntitle: Дело\ndirection: outbound\n"
            "status: resolved\norigin: work/programs/p/commitments/c.md\n---\n\n# c"},
        None,
    ),
    "книга в чтении без даты начала — цикл нечем измерить": (
        {"raw/sources/2026-01-01-s.md": SOURCE,
         "work/me/reading/k.md": reading(status="reading")},
        "в чтении без started",
    ),
    "прочитанное с началом, но без конца — длительность потеряна": (
        {"raw/sources/2026-01-01-s.md": SOURCE,
         "work/me/reading/k.md": reading(status="read", started="2026-01-02",
                                         body="Забрал: запасы и потоки.")},
        "закрыто без finished",
    ),
    "конец чтения раньше начала": (
        {"raw/sources/2026-01-01-s.md": SOURCE,
         "work/me/reading/k.md": reading(status="read", started="2026-02-10",
                                         finished="2026-01-02", body="что-то")},
        "раньше started",
    ),
    "брошенная книга без причины через месяц вернётся в очередь": (
        {"raw/sources/2026-01-01-s.md": SOURCE,
         "work/me/reading/k.md": reading(status="dropped", started="2026-01-02",
                                         finished="2026-01-20", body="бросил")},
        "брошено без причины",
    ),
    "книга взята на «сейчас», а начать её нечем": (
        {"raw/sources/2026-01-01-s.md": SOURCE,
         "work/me/reading/k.md": reading(level="now")},
        "ни зацепки, ни начала",
    ),
    "прочитано, а в записи пусто — отметка о посещении": (
        {"raw/sources/2026-01-01-s.md": SOURCE,
         "work/me/reading/k.md": reading(status="read", started="2026-01-02",
                                         finished="2026-01-20")},
        "отметка о посещении",
    ),
    "два направления об одном разъедутся на первой же новой книге": (
        {"raw/sources/2026-01-01-s.md": SOURCE,
         "work/me/reading/a.md": reading(topics="[процессы]"),
         "work/me/reading/b.md": reading(key="ME-L-2", title='"Другая книга"',
                                         topics="[бизнес-процессы]")},
        "похоже на",
    ),
    "книга без единой дисциплины не найдётся ни по одному направлению": (
        {"raw/sources/2026-01-01-s.md": SOURCE,
         "work/me/reading/a.md": reading(topics="[]")},
        "ни одной дисциплины",
    ),
    "одна книга того же автора, попавшая в каталог дважды": (
        {"raw/sources/2026-01-01-s.md": SOURCE,
         "work/me/reading/a.md": reading(title='"Business Process Change"',
                                         author='"Пол Хармон"'),
         "work/me/reading/b.md": reading(key="ME-L-2", author='"Пол Хармон"',
                                         title='"Business Process Change и процессный анализ"')},
        "похоже на ту же книгу",
    ),
    "похожие названия разных авторов книгами-близнецами не считаются": (
        {"raw/sources/2026-01-01-s.md": SOURCE,
         "work/me/reading/a.md": reading(title='"Networks"', author='"Mark Newman"'),
         "work/me/reading/b.md": reading(key="ME-L-2", title='"Network Science"',
                                         author='"Albert-Laszlo Barabasi"')},
        None,
    ),
    "один навык двумя записями — глубина и доказательства разойдутся": (
        {"work/programs/asr/skills/a.md": SKILL,
         "work/programs/asr/skills/b.md": SKILL.replace("key: ASR-K-S01",
                                                        "key: ASR-K-S02")},
        "уже занято",
    ),
    "прежнее имя навыка занято другим узлом": (
        {"work/programs/asr/skills/a.md":
            SKILL.replace("status: named", "aliases: [B03]\nstatus: named"),
         "work/programs/asr/skills/b.md":
            SKILL.replace("skill_id: S01", "skill_id: B03")
                 .replace("key: ASR-K-S01", "key: ASR-K-B03")},
        "держит его как прежнее имя",
    ),
    "предпосылки навыка образуют петлю": (
        {"work/programs/asr/skills/a.md":
            SKILL.replace("status: named", "requires: {S02: 2}\nstatus: named"),
         "work/programs/asr/skills/b.md":
            SKILL.replace("skill_id: S01", "skill_id: S02")
                 .replace("key: ASR-K-S01", "key: ASR-K-S02")
                 .replace("status: named", "requires: {S01: 2}\nstatus: named")},
        "петлю",
    ),
    "предпосылка на уровне «узнаёт» — не предпосылка": (
        {"work/programs/asr/skills/a.md":
            SKILL.replace("status: named", "requires: {S02: 1}\nstatus: named"),
         "work/programs/asr/skills/b.md":
            SKILL.replace("skill_id: S01", "skill_id: S02")
                 .replace("key: ASR-K-S01", "key: ASR-K-S02")},
        "не предпосылка",
    ),
    "предпосылка списком вместо пар «навык: уровень»": (
        {"work/programs/asr/skills/a.md":
            SKILL.replace("status: named", "requires: [S02]\nstatus: named")},
        "должны быть парами",
    ),
    "предпосылка ведёт в несуществующий навык": (
        {"work/programs/asr/skills/a.md":
            SKILL.replace("status: named", "requires: {ZZ9: 2}\nstatus: named")},
        "не найдена среди навыков",
    ),
    "целевой уровень навыка вне шкалы": (
        {"work/programs/asr/skills/a.md": SKILL.replace("target: 4", "target: 7")},
        "вне шкалы",
    ),
    "навык объявлен описанным, но уровни не расписаны": (
        {"work/programs/asr/skills/a.md": SKILL.replace("status: named",
                                                        "status: specified")},
        "не расписаны",
    ),
    "названный навык дескрипторов не требует": (
        {"work/programs/asr/skills/a.md": SKILL},
        None,
    ),
    # Обратная сторона: проверка обязана признавать расписанные уровни. Без этого
    # случая она однажды искала заголовки в тексте, из которого заголовки заранее
    # выброшены, и ругалась на любой описанный навык — ловила отсутствие, но не
    # умела увидеть наличие.
    "описанный навык с расписанными уровнями проходит": (
        {"work/programs/asr/skills/a.md":
            SKILL.replace("status: named", "status: specified")
            + "\n## Уровни\n\n### 1 — узнаёт\nа\n\n### 2 — объясняет\nб\n\n"
              "### 3 — применяет сам\nв\n\n### 4 — соединяет\nг\n"},
        None,
    ),
    "мета-узел описан с третьего уровня — так и надо": (
        {"work/programs/asr/skills/a.md":
            SKILL.replace("status: named", "node: meta\nstatus: specified")
                 .replace("node: atomic\n", "")
            + "\n### 3 — применяет сам\nа\n\n### 4 — соединяет\nб\n"},
        None,
    ),
    "мета-узел без уровня применения — нарушение": (
        {"work/programs/asr/skills/a.md":
            SKILL.replace("status: named", "node: meta\nstatus: specified")
                 .replace("node: atomic\n", "")
            + "\n### 4 — соединяет\nб\n"},
        "уровни 3 не расписаны",
    ),
    "описанный навык без верхнего уровня — нарушение": (
        {"work/programs/asr/skills/a.md":
            SKILL.replace("status: named", "status: specified")
            + "\n### 1 — узнаёт\nа\n\n### 2 — объясняет\nб\n\n### 3 — применяет\nв\n"},
        "уровни 4 не расписаны",
    ),
    "доказательство опирается на нашу же позицию, а не на событие": (
        {"work/programs/asr/skills/a.md": SKILL,
         "work/programs/asr/evidence/e.md":
            "---\ntype: evidence\nskill: work/programs/asr/skills/a.md\nlevel: 3\n"
            "date: 2026-08-05\norigin: work/programs/asr/skills/a.md\n---\n\n# e"},
        "не на событие",
    ),
    "доказательство не говорит, что именно сделано": (
        {"raw/sources/2026-01-01-s.md": SOURCE,
         "work/programs/asr/skills/a.md": SKILL,
         "work/programs/asr/evidence/e.md":
            "---\ntype: evidence\nskill: work/programs/asr/skills/a.md\nlevel: 3\n"
            "date: 2026-08-05\norigin: raw/sources/2026-01-01-s.md\n---\n\n# e"},
        "что именно сделано",
    ),
    "доказательство с названным результатом проходит": (
        {"raw/sources/2026-01-01-s.md": SOURCE,
         "work/programs/asr/skills/a.md": SKILL,
         "work/programs/asr/evidence/e.md":
            "---\ntype: evidence\nskill: work/programs/asr/skills/a.md\nlevel: 3\n"
            'result: "восстановил процесс из двух источников"\n'
            "date: 2026-08-05\norigin: raw/sources/2026-01-01-s.md\n---\n\n# e"},
        None,
    ),
    "уровень доказательства вне шкалы": (
        {"raw/sources/2026-01-01-s.md": SOURCE,
         "work/programs/asr/skills/a.md": SKILL,
         "work/programs/asr/evidence/e.md":
            "---\ntype: evidence\nskill: work/programs/asr/skills/a.md\nlevel: 9\n"
            "date: 2026-08-05\norigin: raw/sources/2026-01-01-s.md\n---\n\n# e"},
        "вне шкалы 1–5",
    ),
    "книга раскрывает навык, которого нет в графе": (
        {"raw/sources/2026-01-01-s.md": SOURCE,
         "work/programs/asr/skills/a.md": SKILL,
         "work/me/reading/k.md": reading(skills="{S01: 4, ZZ9: 2}")},
        "которого нет в графе",
    ),
    "книга с разметкой по существующему навыку — чисто": (
        {"raw/sources/2026-01-01-s.md": SOURCE,
         "work/programs/asr/skills/a.md": SKILL,
         "work/me/reading/k.md": reading(skills="{S01: 4}")},
        None,
    ),
    "глубина книги выше самой высокой описанной ступени навыка": (
        {"raw/sources/2026-01-01-s.md": SOURCE,
         "work/programs/asr/skills/a.md": SKILL.replace(
             "target: 4\nstatus: named", "target: 3\nstatus: specified")
            + "\n### 1 — узнаёт\nа\n\n### 2 — объясняет\nб\n\n### 3 — применяет сам\nв\n",
         "work/me/reading/k.md": reading(skills="{S01: 4}")},
        "ступени расписаны до 3",
    ),
    "глубина книги в пределах описанных ступеней — чисто": (
        {"raw/sources/2026-01-01-s.md": SOURCE,
         "work/programs/asr/skills/a.md": SKILL.replace(
             "target: 4\nstatus: named", "target: 3\nstatus: specified")
            + "\n### 1 — узнаёт\nа\n\n### 2 — объясняет\nб\n\n### 3 — применяет сам\nв\n",
         "work/me/reading/k.md": reading(skills="{S01: 3}")},
        None,
    ),
    "книга сразу в двух дисциплинах — так и надо": (
        {"raw/sources/2026-01-01-s.md": SOURCE,
         "work/me/reading/a.md":
            reading(topics="[системное-мышление, бизнес-архитектура]")},
        None,
    ),
    "книга, годами лежащая в очереди, — не нарушение: полка не стареет": (
        {"raw/sources/2026-01-01-s.md": SOURCE,
         "work/me/reading/k.md": reading(level="later")},
        None,
    ),
    "прочитанное с записью о том, что забрал, — чисто": (
        {"raw/sources/2026-01-01-s.md": SOURCE,
         "work/me/reading/k.md": reading(status="read", started="2026-01-02",
                                         finished="2026-01-20",
                                         body="Забрал: запасы, потоки, задержки.")},
        None,
    ),
}


def build(root: Path, files: dict[str, str]) -> None:
    for name, body in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")


def main() -> int:
    original_stdout = sys.stdout
    counted_stdout = CaseCountingOutput(original_stdout)
    sys.stdout = counted_stdout
    failures: list[str] = []

    # Одиночный случай не входит ни в один словарь или список. Если поток
    # результата перестанет считать такие строки, сам verify обязан покраснеть.
    before_standalone = counted_stdout.successes
    print("✓ одиночный случай вне коллекций входит в фактический счётчик")
    if counted_stdout.successes != before_standalone + 1:
        failures.append("счётчик пропустил одиночный выполненный случай")

    for title, (files, expected) in CASES.items():
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build(root, files)
            report = lint.run(root)
            messages = [m for _, m in report.problems + report.notices]
            if expected is None:
                # Случай-отрицание: нарушений быть НЕ должно вовсе. Раньше здесь
                # искались два конкретных слова — то есть отрицание проверяло
                # ровно то, что автор ожидал сломать, и молчало про остальное.
                noise = [m for _, m in report.problems]
                if noise:
                    failures.append(f"ЛОЖНАЯ ТРЕВОГА: {title}\n    получено: {noise}")
                else:
                    print(f"✓ {title}")
                continue
            if not any(expected in m for m in messages):
                failures.append(f"НЕ ПОЙМАН: {title}\n    ожидалось: {expected!r}\n"
                                f"    получено: {messages or '(тишина)'}")
            else:
                print(f"✓ {title}")

    # Ложных тревог быть не должно: пример синтаксиса в коде — не ссылка
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"docs/schema.md": "Ссылки вида `[Имя](../people/kto-to.md)` — пример.\n"
                                       "```\n[и в блоке](../nope.md)\n```\n"})
        report = lint.run(root)
        if report.problems:
            failures.append(f"ЛОЖНАЯ ТРЕВОГА на примере в коде: {report.problems}")
        else:
            print("✓ ссылка внутри кода не считается ссылкой")

    # Вакуум: пустой склад обязан быть провалом, а не «нарушений нет»
    with tempfile.TemporaryDirectory() as tmp:
        code = lint.main(["--root", tmp, "--quiet"])
        if code == 0:
            failures.append("НЕ ПОЙМАН: пустой склад вернул успех — это и есть вакуумный гейт")
        else:
            print("✓ пустой склад возвращает провал, а не «нарушений нет»")

    # Здоровый склад обязан проходить: проверка, которая ругается всегда, бесполезна
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"work/people/ivan-petrov.md": OK_NOTE})
        report = lint.run(root)
        if report.problems:
            failures.append(f"ЛОЖНАЯ ТРЕВОГА на здоровом складе: {report.problems}")
        else:
            print("✓ здоровый склад проходит без нарушений")

    # Имя события — часть схемы общей шины. Опечатка остаётся замечанием:
    # произошедшее не переписываем, но молча терять его читателям нельзя.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"raw/log.md":
                     "- 2026-07-01T10:00:00 · предстаавлен · кандидат · work/a.md\n"})
        report = lint.run(root)
        noticed = [message for where, message in report.notices
                   if where == "raw/log.md" and "предстаавлен" in message]
        if noticed:
            print("✓ событие вне словаря журнал не проглатывает — линтер замечает")
        else:
            failures.append("НЕ ПОЙМАНО: неизвестное событие журнала исчезло молча")

    # Восемь прежних толкований строки обязаны зависеть от activity.Entry/read,
    # а не возвращать себе локальный split при следующей доработке.
    journal_readers = {
        "история советов": advice_mod.read,
        "последнее показанное": refer.shown_recently,
        "показы и реакции": reflect.read_log,
        "касания": reflect.read_touches,
        "ночная работа": attention.night_log_entries,
        "выход наружу": attention.outbound_recent,
        "поправки профиля": attention.build_lines,
        "присутствие ноутбука": sync.laptop_active,
    }
    local_parsers = []
    for name, reader in journal_readers.items():
        source = inspect.getsource(reader)
        if ("activity." not in source
                or 'split(" · ")' in source
                or ".splitlines()" in source
                or ".read_text(" in source):
            local_parsers.append(name)
    if not local_parsers:
        print("✓ все восемь читателей используют единый разбор activity")
    else:
        failures.append("вернулся самостоятельный разбор журнала: "
                        + ", ".join(local_parsers))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        shown_at = dt.datetime(2026, 7, 1, 11, 50)
        build(root, {"raw/log.md":
                     f"- {shown_at:%Y-%m-%dT%H:%M:%S} · представлен · кандидат · work/a.md\n"})
        recent = refer.shown_recently(root, dt.datetime(2026, 7, 1, 12, 0))
        if recent == [(shown_at, "work/a.md")]:
            print("✓ разрешение «это» получает свежий показ из общего разбора")
        else:
            failures.append(f"НЕ СОШЛОСЬ: свежий показ прочитан как {recent}")

    # Живая реплика не команда: один классификатор обязан сохранить все её
    # независимые смыслы, а не остановиться на первом совпадении.
    utterance = ("Это уже сделали вчера, закрой; и вообще подобные мелочи "
                 "закрывай сам, если видишь результат в TickTick")
    intents = dialogue.classify(utterance, today=dt.date(2026, 8, 9))
    intent_kinds = {one.kind for one in intents}
    if {"state_change", "request", "preference"} <= intent_kinds:
        print("✓ одна живая реплика сохраняет факт, просьбу и правило работы")
    else:
        failures.append(f"ПОТЕРЯНЫ намерения сложной реплики: {intent_kinds}")

    intent_samples = {
        "confirmation": "Да, сделай",
        "refusal": "Нет, не надо",
        "correction": "Ты неправильно понял: речь о другом",
        "defer": "Вернись к этому в пятницу",
        "state_change": "Они уже прислали",
        "fact": "Тимур теперь отвечает за финансы",
        "commitment": "Я обещал отправить это завтра",
        "decision": "Идём по второму варианту",
        "preference": "Такие вещи не присылай утром",
        "feedback": "Не надо каждый раз об этом напоминать",
        "question": "Почему ты считаешь это важным?",
        "request": "Поставь это на завтра",
        "capture": "Запиши идею нового формата",
        "reflection": "Была такая мысль, но пока просто думаю",
    }
    missed_intents = [kind for kind, phrase in intent_samples.items()
                      if kind not in {one.kind for one in dialogue.classify(
                          phrase, today=dt.date(2026, 8, 9))}]
    if not missed_intents:
        print("✓ классификатор различает все четырнадцать намерений контракта")
    else:
        failures.append("НЕ РАСПОЗНАНЫ намерения: " + ", ".join(missed_intents))

    defer_intents = dialogue.classify(
        "Нет, они пока сами думают, вернись к этому в понедельник",
        today=dt.date(2026, 8, 9))
    defer_transition = dialogue.planned_transition(defer_intents)
    if (defer_transition is not None and defer_transition.action == "defer"
            and defer_transition.until == dt.date(2026, 8, 10)
            and defer_transition.reason == "ждём внешнего события или решения"):
        print("✓ отрицание, причина, отсрочка и день возврата разбираются вместе")
    else:
        failures.append(f"ПОТЕРЯНЫ части отсрочки: {defer_transition}")

    # Вес не равен уверенности: сильные, но близкие кандидаты неоднозначны.
    fake_a = store_mod.Note(Path("a.md"), "work/a.md", {"type": "person", "name": "Альфа"})
    fake_b = store_mod.Note(Path("b.md"), "work/b.md", {"type": "person", "name": "Бета"})
    high = refer.decide("это", [refer.Candidate(fake_a, "показано", 0.95)])
    medium = refer.decide("это", [refer.Candidate(fake_a, "показано", 0.95),
                                  refer.Candidate(fake_b, "тоже подходит", 0.90)])
    low = refer.decide("это", [])
    confidence_cases = [
        ("высокая уверенность ведёт к действию",
         high.confidence == refer.HIGH and high.action == "действовать"),
        ("неоднозначность ведёт к показу интерпретации",
         medium.confidence == refer.MEDIUM
         and medium.action == "показать интерпретацию"),
        ("низкая уверенность задаёт ровно один вопрос",
         low.confidence == refer.LOW and low.action == "уточнить"
         and low.question.count("?") == 1),
    ]
    for title, ok in confidence_cases:
        if ok:
            print(f"✓ {title}")
        else:
            failures.append(f"НЕ СОШЛОСЬ: {title}")

    # Календарь и недавняя встреча дают кандидатов, но не право молча действовать.
    calendar_holder = store_mod.Note(
        Path("client.md"), "work/clients/alpha/client.md",
        {"type": "client", "title": "Альфа"})
    calendar_event = agenda.Event(
        start=dt.datetime(2026, 8, 9, 12, 0), end=None, title="Альфа — разбор",
        container="work/clients/alpha")
    recent_meeting = store_mod.Note(
        Path("meeting.md"), "raw/meetings/2026-08-08-beta.md",
        {"type": "meeting", "title": "Вопрос Бета", "date": "2026-08-08"})
    calendar_resolution = refer.resolve_reference(
        Path("/nonexistent"), "это по Альфа", [calendar_holder],
        dt.datetime(2026, 8, 9, 10, 0), calendar_events=[calendar_event])
    meeting_resolution = refer.resolve_reference(
        Path("/nonexistent"), "тот вопрос вчера", [],
        dt.datetime(2026, 8, 9, 10, 0), recent_notes=[recent_meeting])
    if (calendar_resolution.target == calendar_holder
            and calendar_resolution.confidence != refer.HIGH
            and meeting_resolution.target == recent_meeting
            and meeting_resolution.confidence != refer.HIGH):
        print("✓ календарь и недавняя встреча помогают, но сами не дают высокой уверенности")
    else:
        failures.append("НЕ СОШЛОСЬ: ситуативный контекст ссылки стал фактом")

    policy_cases = [
        (dialogue.ActionContext("наблюдение", records_observation=True), dialogue.DO_NOW),
        (dialogue.ActionContext("черновик", prepares_draft=True), dialogue.DO_AND_TELL),
        (dialogue.ActionContext("закрытие", closes_commitment=True), dialogue.PROPOSE_AND_WAIT),
        (dialogue.ActionContext("сообщение", visible_to_other=True), dialogue.ALWAYS_ASK),
    ]
    policy_results = [dialogue.confirmation_policy(action).level
                      for action, _ in policy_cases]
    if policy_results == [expected for _, expected in policy_cases]:
        print("✓ политика различает все четыре уровня подтверждения")
    else:
        failures.append(f"НЕ СОШЛОСЬ: четыре уровня дали {policy_results}")

    # Ситуативная политика действует до ранжирования отдельных строк. Низкая
    # ёмкость вместе со свежим разбором снимает оба исследования целиком, но не
    # прячет предложение применить уже найденное.
    low_situation = policy_mod.Situation(
        today=dt.date(2026, 8, 9), capacity="low", recent_research=1,
    )
    proposals = [
        policy_mod.Proposal("research", "ещё одно интервью"),
        policy_mod.Proposal("research", "ещё один обзор источников"),
        policy_mod.Proposal("action", "применить найденное"),
    ]
    low_decision = policy_mod.decide(low_situation, "research")
    low_filtered = policy_mod.filter_proposals(low_situation, proposals)
    if (low_decision.status == policy_mod.BLOCKED
            and [one.text for one in low_filtered] == ["применить найденное"]
            and "ёмкость низкая" in low_decision.render()
            and "свежих разборов" in low_decision.render()):
        print("✓ низкая ёмкость и свежий ресёрч снимают весь класс исследований")
    else:
        failures.append("НЕ СОШЛОСЬ: исследование не снято целиком или причина скрыта: "
                        f"{low_decision}, осталось {low_filtered}")

    # При тех же свежих данных, но полной ёмкости и без остальных ограничений
    # исследование остаётся доступным и явно поднимается.
    full_situation = policy_mod.Situation(
        today=dt.date(2026, 8, 9), capacity="full", recent_research=1,
    )
    full_decision = policy_mod.decide(full_situation, "research")
    full_filtered = policy_mod.filter_proposals(full_situation, proposals)
    if (full_decision.status == policy_mod.RAISED
            and len(full_filtered) == len(proposals)
            and "ёмкость полная" in full_decision.render()):
        print("✓ при полной ёмкости исследование предлагается и причина названа")
    else:
        failures.append("НЕ СОШЛОСЬ: полная ёмкость скрыла исследование: "
                        f"{full_decision}, осталось {full_filtered}")

    # Снимок собирает исходные факты, а не читает готовый текст attention.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {
            "config/attention.yml": (
                "tracker:\n  max_active_containers: 3\n"
                "analysis_drift:\n  window_days: 2\n  min_new_analysis: 1\n"
                "night:\n  start: '23:00'\n  end: '06:00'\n"
                "  lookback_hours: 24\n  ignore_markers: [экран собран]\n"
                "situational_policy:\n  recent_research_days: 1\n"
                "  closure_window_days: 7\n  closure_response_days: 3\n"
                "  min_closure_samples: 2\n  min_closure_rate: 0.5\n"
            ),
            "raw/log.md": (
                "---\ntype: log\ntitle: журнал\n---\n\n# журнал\n"
                "- 2026-08-09T01:00:00 · решение подтверждено · x · y\n"
                "- 2026-08-09T02:00:00 · telegram обработано · 1 · принято\n"
                "- 2026-08-08T12:00:00 · представлен · кандидат · work/a.md\n"
                "- 2026-08-09T07:00:00 · представлен · кандидат · work/b.md\n"
            ),
            "work/clients/a/client.md": (
                "---\ntype: client\ntitle: Альфа\nmode: active\n---\n\n# Альфа\n"
            ),
            "work/clients/a/digests/research.md": (
                "---\ntype: digest\ntitle: Разбор\ndate: 2026-08-08\n"
                "container: work/clients/a\n---\n\n# Разбор\n"
            ),
            "work/clients/a/commitments/a.md": (
                "---\ntype: commitment\ntitle: Обещание\nstatus: open\n"
                "direction: outbound\n---\n\n# Обещание\n"
            ),
        })
        situation = policy_mod.snapshot(
            root, dt.date(2026, 8, 9), capacity="low",
            now=dt.datetime(2026, 8, 9, 8, 0),
        )
        snapshot_ok = (
            situation.active_containers == 1
            and situation.open_promises == 1
            and situation.recent_research == 1
            and situation.analysis_drift
            and situation.night_entries == 1
            and situation.closure_samples == 2
            and situation.closure_rate == 0
        )
        if snapshot_ok:
            print("✓ политика собирает семь входов состояния из исходных фактов")
        else:
            failures.append(f"НЕ СОШЛОСЬ: снимок состояния неполон: {situation}")

    # Отрицательная реакция без причины непригодна для записи; три причины
    # маршрутизируются по-разному и остаются в журнале рядом с реакцией.
    refusal_samples = [
        ("Нет, они пока сами думают", "зависимость", "wait"),
        ("Не могу сейчас, позже", "неготовность", "defer"),
        ("Не надо, это больше неактуально", "неактуальность", "dismiss"),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"raw/log.md": "# журнал\n"})
        routed = []
        for phrase, expected_reason, expected_action in refusal_samples:
            refusal = next(one for one in dialogue.classify(
                phrase, today=dt.date(2026, 8, 9)) if one.kind == "refusal")
            action, reason = dialogue.refusal_transition(refusal)
            routed.append((refusal.reason_kind, action, expected_reason, expected_action))
            dialogue.record_refusal(root, refusal, "work/a.md",
                                    now=dt.datetime(2026, 8, 9, 10, 0))
        bare = next(one for one in dialogue.classify("Нет") if one.kind == "refusal")
        bare_rejected = False
        try:
            dialogue.refusal_transition(bare)
        except ValueError:
            bare_rejected = True
        recorded = activity.read(root, events={"реакция"}, future_ok=True)
        reasons_present = all(len(one.parts) >= 3 and ":" in one.parts[2]
                              for one in recorded)
        if (all(actual_reason == expected_reason and actual_action == expected_action
                for actual_reason, actual_action, expected_reason, expected_action in routed)
                and bare_rejected and len(recorded) == 3 and reasons_present):
            print("✓ отказ хранит зависимость, неготовность или неактуальность — не голое «нет»")
        else:
            failures.append(f"НЕ СОШЛОСЬ: причины отказа {routed}, журнал {recorded}")

    # Поправка отменяет только предложенное понимание, оставляет след и снова
    # пропускает уточнённую реплику через тот же интерпретатор.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        shown_at = dt.datetime(2026, 8, 9, 9, 55)
        build(root, {
            "raw/log.md": (f"- {shown_at:%Y-%m-%dT%H:%M:%S} · представлен · кандидат · "
                           "work/people/alpha.md\n"),
            "work/people/alpha.md": "---\ntype: person\nname: Альфа\n---\n\n# Альфа\n",
            "work/people/beta.md": "---\ntype: person\nname: Бета\n---\n\n# Бета\n",
        })
        notes = store_mod.load(root, "work").notes
        confirmed = dialogue.interpret(root, "это закрой", notes,
                                       now=dt.datetime(2026, 8, 9, 10, 0))
        dialogue.confirm_reference(root, confirmed,
                                   now=dt.datetime(2026, 8, 9, 10, 1))
        previous = dialogue.interpret(root, "это закрой", notes,
                                      now=dt.datetime(2026, 8, 9, 10, 1))
        corrected = dialogue.correct(
            root, previous,
            "Ты неправильно понял: это про Бета, подготовь черновик",
            notes, actual_target="work/people/beta.md",
            now=dt.datetime(2026, 8, 9, 10, 2))
        events = activity.read(root, events={"ссылка разрешена", "поправка"},
                               future_ok=True)
        outcomes = [one.parts[0] for one in events if one.event == "ссылка разрешена"]
        corrected_target = [one.parts[2] for one in events
                            if one.event == "ссылка разрешена"
                            and one.parts[0] == "человек поправил"]
        replacement_kinds = ({one.kind for one in corrected.replacement.intents}
                             if corrected.replacement is not None else set())
        replacement_target = (corrected.replacement.reference.target.rel
                              if corrected.replacement is not None
                              and corrected.replacement.reference is not None
                              and corrected.replacement.reference.target is not None else "")
        if (previous.cancelled and corrected.cancelled
                and {"человек подтвердил", "человек поправил"} <= set(outcomes)
                and corrected_target == ["work/people/beta.md"]
                and replacement_target == "work/people/beta.md"
                and "request" in replacement_kinds
                and any(one.event == "поправка" for one in events)):
            print("✓ «ты неправильно понял» отменяет догадку, сохраняет поправку и запускает разбор снова")
        else:
            failures.append("НЕ СОШЛОСЬ: поправка не замкнула цикл разрешения ссылки")

    # Deepgram получает русский Nova-3, умное форматирование и отдельные
    # keyterm-параметры — не строку через запятую, которую сервис молча не усилит.
    dg_person = store_mod.Note(
        Path("person.md"), "work/people/ivan.md",
        {"type": "person", "name": "Иван Петров",
         "aliases": ["владелец"]})
    dg_client = store_mod.Note(
        Path("client.md"), "work/clients/standard/client.md",
        {"type": "client", "title": "Стандарт Управление",
         "org": "Стандарт Управление", "prefix": "SU"})
    dg_terms = deepgram.keyterms([dg_client, dg_person])
    dg_seen: dict[str, object] = {}

    def fake_deepgram(url: str, headers: dict[str, str], body: bytes,
                      timeout: int) -> bytes:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        dg_seen.update(query=query, authorization=headers.get("Authorization"),
                       body=body, timeout=timeout)
        return json.dumps({
            "metadata": {"request_id": "dg-test"},
            "results": {"channels": [{"alternatives": [{
                "transcript": "Стандарт Управление: до пятницы — 250.",
                "confidence": 0.97,
            }]}]},
        }, ensure_ascii=False).encode()

    dg_result = deepgram.transcribe(
        b"voice", api_key="test-key", terms=dg_terms, requester=fake_deepgram)
    dg_query = dg_seen.get("query") or {}
    if (dg_result.text == "Стандарт Управление: до пятницы — 250."
            and dg_query.get("model") == ["nova-3"]
            and dg_query.get("language") == ["ru"]
            and dg_query.get("smart_format") == ["true"]
            and dg_query.get("keyterm") == dg_terms
            and dg_seen.get("authorization") == "Token test-key"):
        print("✓ Deepgram получает русский Nova-3, числа и отдельные подсказки склада")
    else:
        failures.append(f"НЕ СОШЛОСЬ: запрос Deepgram {dg_seen}, ответ {dg_result}")

    many_terms = [store_mod.Note(Path(f"p{i}.md"), f"work/people/p{i}.md",
                                 {"type": "person", "name": f"Человек {i}"})
                  for i in range(130)]
    limited_terms = deepgram.keyterms(many_terms)
    if len(limited_terms) == deepgram.MAX_KEYTERMS and len(set(limited_terms)) == 100:
        print("✓ словарь распознавания ограничен ста уникальными именами")
    else:
        failures.append(f"НЕ СОШЛОСЬ: словарь Deepgram дал {len(limited_terms)} терминов")

    class FakeTelegram:
        def __init__(self, audio: bytes = b"OGG-original") -> None:
            self.audio = audio
            self.sent: list[tuple[int, str]] = []
            self.next_message = 500

        def get_file(self, file_id: str) -> str:
            return f"voice/{file_id}.oga"

        def download(self, file_path: str) -> bytes:
            return self.audio

        def send_message(self, chat_id: int, text: str) -> int:
            self.sent.append((chat_id, text))
            self.next_message += 1
            return self.next_message

    TELEGRAM_NOTE = (
        "---\ntype: commitment\nkey: TG-C-1\ntitle: Проверить Стандарт Управление\n"
        "direction: outbound\nstatus: open\nowner: ivan\nlevel: now\n"
        "when_then:\n  cue: открыть\n  action: проверить\n"
        "origin: raw/inbox/2026-08-09-origin.md\n---\n\n# дело\n")
    TELEGRAM_SOURCE = (
        "---\ntype: source\ndate: 2026-08-09\ntitle: Источник\n"
        "source: test\nsource_ref: tg-origin\n---\n\n# источник\n")
    telegram_now = dt.datetime(2026, 8, 9, 12, 0)

    # Оригинал пишется первым и побайтно; Markdown рядом содержит только
    # дословную расшифровку, но не производную интерпретацию.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {
            "raw/log.md": "# журнал\n",
            "raw/inbox/2026-08-09-origin.md": TELEGRAM_SOURCE,
            "work/commitments/tg.md": TELEGRAM_NOTE,
            "work/people/ivan.md": (
                "---\ntype: person\nname: Иван Петров\n---\n\n# владелец\n"),
            "work/clients/standard/client.md": (
                "---\ntype: client\ntitle: Стандарт Управление\nmode: active\n"
                "prefix: SU\n---\n\n# Стандарт Управление\n"),
        })
        api = FakeTelegram()
        voice_terms: list[str] = []

        def fake_transcribe(audio: bytes, **kwargs) -> deepgram.Transcript:
            voice_terms.extend(kwargs.get("terms") or [])
            return deepgram.Transcript("Стандарт Управление: до пятницы — 250.", 0.98,
                                       "voice-test")

        bot = telegram_bot.Bot(
            root, api, allowed_user_id=7, deepgram_key="test-key",
            transcriber=fake_transcribe, now=lambda: telegram_now,
            writer_active=lambda moment: False)
        voice_update = {
            "update_id": 101,
            "message": {"message_id": 11, "date": int(telegram_now.timestamp()),
                        "from": {"id": 7},
                        "chat": {"id": 7, "type": "private"},
                        "voice": {"file_id": "voice-11"}},
        }
        voice_result = bot.process(voice_update)
        originals = list((root / "raw" / "inbox").glob("*telegram-7-11.oga"))
        transcripts = list((root / "raw" / "inbox").glob("*telegram-7-11.md"))
        transcript_text = transcripts[0].read_text(encoding="utf-8") if transcripts else ""
        voice_events = activity.read(
            root, events={"telegram получено", "голос расшифрован",
                          "telegram отправлено", "telegram обработано"},
            future_ok=True)
        if (len(originals) == 1 and originals[0].read_bytes() == b"OGG-original"
                and len(transcripts) == 1
                and "Стандарт Управление: до пятницы — 250." in transcript_text
                and "Намерения:" not in transcript_text
                and "Стандарт Управление" in voice_terms
                and voice_result.interpretation is not None
                and {one.event for one in voice_events}
                == {"telegram получено", "голос расшифрован",
                    "telegram отправлено", "telegram обработано"}):
            print("✓ голос сохраняется оригиналом, расшифровывается и идёт тем же разговорным путём")
        else:
            failures.append("НЕ СОШЛОСЬ: оригинал, расшифровка и интерпретация голоса смешались")

    # Отказ распознавания не имеет права удалить уже скачанный оригинал.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"raw/log.md": "# журнал\n"})
        api = FakeTelegram(b"keep-me")
        bot = telegram_bot.Bot(root, api, allowed_user_id=7, deepgram_key="",
                               now=lambda: telegram_now,
                               writer_active=lambda moment: False)
        result = bot.process({
            "update_id": 102,
            "message": {"message_id": 12, "date": int(telegram_now.timestamp()),
                        "from": {"id": 7},
                        "chat": {"id": 7, "type": "private"},
                        "voice": {"file_id": "voice-12"}},
        })
        originals = list((root / "raw" / "inbox").glob("*telegram-7-12.oga"))
        transcripts = list((root / "raw" / "inbox").glob("*telegram-7-12.md"))
        if (len(originals) == 1 and originals[0].read_bytes() == b"keep-me"
                and not transcripts and "сохранил" in result.response.lower()):
            print("✓ без ключа Deepgram оригинал голоса остаётся, незнание названо")
        else:
            failures.append("НЕ СОШЛОСЬ: недоступный Deepgram потерял или подменил голос")

    # Reply несёт точную связь, но только два часа. После срока прежняя цель
    # намеренно не попадает обратно в интерпретатор даже как слабая догадка.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = "work/commitments/tg.md"
        build(root, {
            "raw/log.md": "# журнал\n",
            "raw/inbox/2026-08-09-origin.md": TELEGRAM_SOURCE,
            target: TELEGRAM_NOTE,
        })
        telegram_bot.record_outgoing(root, 7, 90, [target],
                                     now=telegram_now - dt.timedelta(minutes=10))
        telegram_bot.record_outgoing(root, 7, 80, [target],
                                     now=telegram_now - dt.timedelta(minutes=121))
        active = telegram_bot.reply_context(root, 7, 90, now=telegram_now)
        expired = telegram_bot.reply_context(root, 7, 80, now=telegram_now)
        bot = telegram_bot.Bot(root, FakeTelegram(), allowed_user_id=7,
                               now=lambda: telegram_now,
                               writer_active=lambda moment: False)
        active_result = bot.handle({
            "update_id": 103,
            "message": {"message_id": 13, "date": int(telegram_now.timestamp()),
                        "from": {"id": 7},
                        "chat": {"id": 7, "type": "private"},
                        "text": "перенеси это на пятницу",
                        "reply_to_message": {"message_id": 90}},
        })
        expired_result = bot.handle({
            "update_id": 104,
            "message": {"message_id": 14, "date": int(telegram_now.timestamp()),
                        "from": {"id": 7},
                        "chat": {"id": 7, "type": "private"},
                        "text": "перенеси это на пятницу",
                        "reply_to_message": {"message_id": 80}},
        })
        if (active.state == "active" and active.targets == (target,)
                and expired.state == "expired"
                and active_result.targets == (target,)
                and active_result.interpretation is not None
                and active_result.interpretation.reference is not None
                and active_result.interpretation.reference.confidence == refer.HIGH
                and expired_result.targets == ()
                and expired_result.response.count("?") == 1):
            print("✓ reply разрешает свежую связь точно, а через два часа задаёт один вопрос")
        else:
            failures.append("НЕ СОШЛОСЬ: срок жизни Telegram-контекста не соблюдён")

    # Повтор одного update не создаёт второе сырьё и не проходит обработку снова.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"raw/log.md": "# журнал\n"})
        update = {
            "update_id": 105,
            "message": {"message_id": 15, "date": int(telegram_now.timestamp()),
                        "from": {"id": 7},
                        "chat": {"id": 7, "type": "private"},
                        "text": "Запиши идею нового формата"},
        }

        class PollingTelegram(FakeTelegram):
            def get_updates(self, offset=None, timeout=0):
                return [update, update]

        bot = telegram_bot.Bot(root, PollingTelegram(), allowed_user_id=7,
                               now=lambda: telegram_now,
                               writer_active=lambda moment: False)
        bot.poll(once=True)
        raw_messages = list((root / "raw" / "inbox").glob("*telegram-7-15.md"))
        receipts = activity.read(root, events={"telegram получено"}, future_ok=True)
        processed = activity.read(root, events={"telegram обработано"}, future_ok=True)
        if len(raw_messages) == len(receipts) == len(processed) == 1:
            print("✓ повтор Telegram-update не создаёт второе сырьё и вторую обработку")
        else:
            failures.append("НЕ СОШЛОСЬ: повтор Telegram прошёл как новое событие")

    # Живой провал: «это 10-е» — часть факта, не ссылка на показанное раньше.
    # Явно названный контейнер сильнее старого дела с теми же словами, а ответ
    # обязан быть проверяем дословно после отправки.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        program = "work/programs/standart/program.md"
        stale = "work/commitments/old-standard.md"
        build(root, {
            "raw/log.md": "# журнал\n",
            program: ("---\ntype: program\ntitle: Стандарт Управление\n"
                      "mode: active\n---\n\n# Стандарт Управление\n"),
            stale: ("---\ntype: commitment\nkey: OLD-C-1\n"
                    "title: Дать решение Стандарт Управление\n"
                    "direction: outbound\nstatus: open\nowner: ivan\nlevel: now\n"
                    "when_then:\n  cue: проверить\n  action: решить\n"
                    "origin: raw/inbox/origin.md\n---\n\n# Старое дело\n"),
            "raw/inbox/origin.md": TELEGRAM_SOURCE,
        })
        api = FakeTelegram()
        bot = telegram_bot.Bot(root, api, allowed_user_id=7,
                               now=lambda: telegram_now,
                               writer_active=lambda moment: False)
        phrase = ("Стандарт Управление это 10-ое. До пятницы нужно подготовить "
                  "250 документов.")
        result = bot.process({
            "update_id": 109,
            "message": {"message_id": 19, "date": int(telegram_now.timestamp()),
                        "from": {"id": 7},
                        "chat": {"id": 7, "type": "private"},
                        "text": phrase},
        })
        sent_id = api.next_message
        recorded = telegram_bot.outgoing_text(root, 7, sent_id)
        if (not refer.has_pointer(phrase)
                and result.targets == (program,)
                and result.interpretation is not None
                and result.interpretation.reference is not None
                and result.interpretation.reference.confidence == refer.HIGH
                and "сохранил дословно" in result.response.lower()
                and "10-ое" not in result.response
                and "смысл не распознан" not in result.response
                and recorded == result.response == api.sent[-1][1]):
            print("✓ «это 10-е» не цепляет старое дело, а точный ответ остаётся в журнале")
        else:
            failures.append("НЕ СОШЛОСЬ: живой провал «это 10-е» повторился")

    # Вход закрыт не только по пользователю, но и по месту: бот не обсуждает
    # личный склад в группе, даже если сообщение туда отправил владелец.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"raw/log.md": "# журнал\n"})
        api = FakeTelegram()
        bot = telegram_bot.Bot(root, api, allowed_user_id=7,
                               now=lambda: telegram_now,
                               writer_active=lambda moment: False)
        foreign = bot.process({
            "update_id": 106,
            "message": {"message_id": 16, "date": int(telegram_now.timestamp()),
                        "from": {"id": 8},
                        "chat": {"id": 8, "type": "private"},
                        "text": "Покажи дела"},
        })
        group = bot.process({
            "update_id": 107,
            "message": {"message_id": 17, "date": int(telegram_now.timestamp()),
                        "from": {"id": 7},
                        "chat": {"id": -1007, "type": "supergroup"},
                        "text": "Покажи дела"},
        })
        inbox = list((root / "raw" / "inbox").glob("*"))
        processed = activity.read(root, events={"telegram обработано"}, future_ok=True)
        if (foreign.ignored and group.ignored and not api.sent and not inbox
                and len(processed) == 2):
            print("✓ Telegram принимает только личный чат владельца")
        else:
            failures.append("НЕ СОШЛОСЬ: чужой пользователь или группа прошли во вход")

    # Если Deepgram временно упал, оригинал уже лежит в raw, а update не
    # помечается обработанным: после перезапуска тот же голос можно повторить.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"raw/log.md": "# журнал\n"})
        api = FakeTelegram(b"survive-deepgram")

        def unavailable_transcriber(audio: bytes, **kwargs) -> deepgram.Transcript:
            raise deepgram.TranscriptionError("временный отказ")

        bot = telegram_bot.Bot(
            root, api, allowed_user_id=7, deepgram_key="test-key",
            transcriber=unavailable_transcriber, now=lambda: telegram_now,
            writer_active=lambda moment: False)
        failed = False
        try:
            bot.process({
                "update_id": 108,
                "message": {"message_id": 18,
                            "date": int(telegram_now.timestamp()),
                            "from": {"id": 7},
                            "chat": {"id": 7, "type": "private"},
                            "voice": {"file_id": "voice-18"}},
            })
        except deepgram.TranscriptionError:
            failed = True
        originals = list((root / "raw" / "inbox").glob("*telegram-7-18.oga"))
        processed = activity.read(root, events={"telegram обработано"}, future_ok=True)
        if (failed and len(originals) == 1
                and originals[0].read_bytes() == b"survive-deepgram"
                and not processed):
            print("✓ временный отказ Deepgram не теряет голос и оставляет его для повтора")
        else:
            failures.append("НЕ СОШЛОСЬ: временный отказ Deepgram потерял голос или update")

    # Библиотеки инструментов: тысячи чужих README не имеют отношения к складу.
    # Одна папка с ними валила весь гейт — 66 нарушений на пустом месте.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"work/people/ivan-petrov.md": OK_NOTE,
                     "tools/diagrams/node_modules/pkg/README.md": "[битая](../nope.md)\n"})
        report = lint.run(root)
        if report.problems:
            failures.append(f"НЕ ПОЙМАН: чужой файл из node_modules попал в гейт: {report.problems}")
        else:
            print("✓ чужой код в node_modules не проверяется")

    # Виртуальное окружение python ставится рядом со складом при первой установке
    # и приносит чужие лицензии в markdown: на свежем клоне гейт краснел двумя
    # файлами pip, и человек читал это как поломку своей системы.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"work/people/ivan-petrov.md": OK_NOTE,
                     ".venv/lib/python3.14/site-packages/pip/LICENSE.md": "лицензия\n",
                     "venv/lib/site-packages/other/README.md": "[битая](../nope.md)\n"})
        report = lint.run(root)
        if report.problems:
            failures.append(f"НЕ ПОЙМАН: файл из .venv попал в гейт: {report.problems}")
        else:
            print("✓ виртуальное окружение python не проверяется")

    # Обратная сторона: исключение обязано быть узким, иначе оно прячет наши же ошибки
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"work/people/ivan-petrov.md": OK_NOTE,
                     "tools/diagrams/README.md": "[битая](../nope.md)\n"})
        report = lint.run(root)
        if not report.problems:
            failures.append("НЕ ПОЙМАН: битая ссылка рядом с node_modules прошла молча")
        else:
            print("✓ вне node_modules ссылки проверяются по-прежнему")

    # Экран внимания: каждый класс сигнала обязан срабатывать, и обязан молчать без повода
    CONFIG = {"limit": 7, "stuck_top": 5, "due_soon_days": 3,
              "stale_days": {"commitment": 14, "spec": 90, "reading": 30},
              "aging_modes": ["active"],
              "reading": {"wip_limit": 2, "top": 2}}
    TODAY = dt.date(2026, 7, 26)
    holder = "---\ntype: program\nmode: active\nprefix: P\n---\n\n# P"
    SELF_HOLDER = ("---\ntype: self\nmode: active\nprefix: ME\ntitle: Я\n---\n\n# я\n")
    screens = {
        "просроченное обещание попадает в экран": (
            {"work/programs/p/program.md": holder,
             "work/programs/p/commitments/c.md":
                "---\ntype: commitment\nkey: P-C-1\ntitle: Отдать отчёт\ndirection: outbound\n"
                "status: open\ndue: 2026-07-20\norigin: work/programs/p/program.md\n---\n\n# c"},
            "просрочено"),
        "чужое обещание без срока попадает в экран": (
            {"work/programs/p/program.md": holder,
             "work/programs/p/commitments/c.md":
                "---\ntype: commitment\nkey: P-C-2\ntitle: Назначить представителя\ndirection: inbound\n"
                "status: open\norigin: work/programs/p/program.md\nopened: 2026-06-01\n---\n\n# c"},
            "чужое обещание без срока"),
        "застоявшееся считается от события, а не от файла": (
            {"work/programs/p/program.md": holder,
             "work/programs/p/specs/s.md":
                "---\ntype: spec\nkey: P-S-1\ntitle: Спека\nstatus: draft\ncreated: 2025-09-15\n"
                "source: work/programs/p/program.md\n---\n\n# s"},
            "без движения"),
        "контейнер idle не стареет": (
            {"work/programs/p/program.md": holder.replace("mode: active", "mode: idle"),
             "work/programs/p/specs/s.md":
                "---\ntype: spec\nkey: P-S-1\ntitle: Спека\nstatus: draft\ncreated: 2025-09-15\n"
                "source: work/programs/p/program.md\n---\n\n# s"},
            None),
        "ждущее чужого ответа называет, от кого ждут": (
            {"work/programs/p/program.md": holder,
             "work/people/shamil.md": "---\ntype: person\nname: Иван Сидоров\n---\n\n# ш",
             "work/programs/p/commitments/c.md":
                "---\ntype: commitment\nkey: P-C-9\ntitle: Ответ по условиям\ndirection: inbound\n"
                "counterpart: work/people/shamil.md\nstatus: waiting\n"
                "origin: work/programs/p/program.md\n---\n\n# c"},
            "ждёт ответа: Иван Сидоров"),
        "своё обещание в ожидании чужим ответом не подписывается": (
            {"work/programs/p/program.md": holder,
             "work/programs/p/commitments/c.md":
                "---\ntype: commitment\nkey: P-C-10\ntitle: Отдать смету\ndirection: outbound\n"
                "status: waiting\norigin: work/programs/p/program.md\n---\n\n# c"},
            "Отдать смету — ждёт ответа"),
        "вопросы без обязательств — противоречие": (
            {"work/programs/p/program.md": holder,
             "work/programs/p/questions/q.md":
                "---\ntype: question\nkey: P-Q-1\ntitle: Что делаем\nstatus: open\n"
                "origin: work/programs/p/program.md\n---\n\n# q"},
            "ни одного обязательства"),
        "закрытое без результата — противоречие": (
            {"work/programs/p/program.md": holder,
             "work/programs/p/questions/q.md":
                "---\ntype: question\nkey: P-Q-2\ntitle: Цена\nstatus: resolved\n"
                "origin: work/programs/p/program.md\n---\n\n# q"},
            "без ссылки на результат"),
        "цель без единого шага попадает в экран": (
            {"work/me/me.md": SELF_HOLDER,
             "work/me/goals/g.md":
                "---\ntype: goal\nkind: result\nhorizon: 2026-12-31\nstatus: active\n"
                "title: Выучить английский\ncreated: 2026-01-01\n---\n\n# g"},
            "цель без единого шага"),
        "цель с привязанным обязательством молчит": (
            {"work/me/me.md": SELF_HOLDER,
             "work/me/goals/g.md":
                "---\ntype: goal\nkind: result\nhorizon: 2026-12-31\nstatus: active\n"
                "title: Выучить английский\ncreated: 2026-01-01\n---\n\n# g",
             "work/me/commitments/c.md":
                "---\ntype: commitment\nkey: ME-C-1\ntitle: Урок\ndirection: outbound\n"
                "status: open\nowner: ivan\ngoal: work/me/goals/g.md\n"
                "origin: work/me/me.md\nopened: 2026-07-25\n---\n\n# c"},
            None),
        "закрытый горизонт при активной цели — противоречие": (
            {"work/me/me.md": SELF_HOLDER,
             "work/me/goals/g.md":
                "---\ntype: goal\nkind: skill\nhorizon: 2026-Q1\nstatus: active\n"
                "title: Навык\ncreated: 2026-07-25\n---\n\n# g",
             "work/me/commitments/c.md":
                "---\ntype: commitment\nkey: ME-C-2\ntitle: Шаг\ndirection: outbound\n"
                "status: open\nowner: ivan\ngoal: work/me/goals/g.md\n"
                "origin: work/me/me.md\nopened: 2026-07-25\n---\n\n# c"},
            "горизонт 2026-Q1 закрылся 31.03"),
        "текущий горизонт претензий не вызывает": (
            {"work/me/me.md": SELF_HOLDER,
             "work/me/goals/g.md":
                "---\ntype: goal\nkind: skill\nhorizon: 2026-Q3\nstatus: active\n"
                "title: Навык\ncreated: 2026-07-25\n---\n\n# g",
             "work/me/commitments/c.md":
                "---\ntype: commitment\nkey: ME-C-3\ntitle: Шаг\ndirection: outbound\n"
                "status: open\nowner: ivan\ngoal: work/me/goals/g.md\n"
                "origin: work/me/me.md\nopened: 2026-07-25\n---\n\n# c"},
            None),
        "начатая и заброшенная книга попадает в экран": (
            {"work/me/me.md": SELF_HOLDER,
             "work/me/reading/k.md":
                "---\ntype: reading\nkey: ME-L-1\ntitle: Thinking in Systems\nkind: book\n"
                "topic: sistemnoe-myshlenie\nstatus: reading\nstarted: 2026-06-01\n"
                "source: work/me/me.md\n---\n\n# k"},
            "не двигается"),
        "очередь не стареет: полка имеет право лежать годами": (
            {"work/me/me.md": SELF_HOLDER,
             "work/me/reading/k.md":
                "---\ntype: reading\nkey: ME-L-2\ntitle: Лежит в очереди\nkind: book\n"
                "topic: finansy\nstatus: queued\nlevel: later\ncreated: 2020-01-01\n"
                "source: work/me/me.md\n---\n\n# k"},
            None),
        "начато больше книг, чем помещается": (
            {"work/me/me.md": SELF_HOLDER,
             **{f"work/me/reading/k{i}.md":
                f"---\ntype: reading\nkey: ME-L-{i}\ntitle: Книга {i}\nkind: book\n"
                f"topic: processy\nstatus: reading\nstarted: 2026-07-25\n"
                f"source: work/me/me.md\n---\n\n# k{i}" for i in range(1, 4)}},
            "при пределе 2"),
        "две книги разом предела не превышают": (
            {"work/me/me.md": SELF_HOLDER,
             **{f"work/me/reading/k{i}.md":
                f"---\ntype: reading\nkey: ME-L-{i}\ntitle: Книга {i}\nkind: book\n"
                f"topic: processy\nstatus: reading\nstarted: 2026-07-25\n"
                f"source: work/me/me.md\n---\n\n# k{i}" for i in range(1, 3)}},
            None),
        "здоровый склад не даёт сигналов": (
            {"work/programs/p/program.md": holder,
             "work/programs/p/commitments/c.md":
                "---\ntype: commitment\nkey: P-C-9\ntitle: Свежее\ndirection: outbound\n"
                "status: open\ndue: 2026-12-31\norigin: work/programs/p/program.md\n"
                "opened: 2026-07-25\n---\n\n# c"},
            None),
    }
    for title, (files, expected) in screens.items():
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build(root, files)
            notes = store_mod.load(root, 'work').notes
            texts = [line.text for line in attention.build_lines(notes, CONFIG, TODAY)]
            if expected is None:
                if texts:
                    failures.append(f"ЛОЖНАЯ ТРЕВОГА: {title}\n    показано: {texts}")
                else:
                    print(f"✓ {title}")
            elif any(expected in text for text in texts):
                print(f"✓ {title}")
            else:
                failures.append(f"НЕ ПОЙМАН: {title}\n    ожидалось {expected!r}, получено {texts or '(тишина)'}")

    # Таймбокс решений, перегрев контуров, анализ без действия — механики из
    # профиля человека (work/me/profil.md)
    DECIDE_CONF = dict(CONFIG, decisions={"timebox_days": {"small": 1, "medium": 2, "large": 5},
                                          "default_weight": "medium"})
    HEAT_CONF = dict(CONFIG, tracker={"max_active_containers": 1})
    DRIFT_CONF = dict(CONFIG, analysis_drift={"window_days": 14, "min_new_analysis": 2})
    second_holder = "---\ntype: program\nmode: active\nprefix: Q\ntitle: Кью\n---\n\n# Q"
    fresh_question = ("---\ntype: question\nkey: P-Q-{i}\ntitle: Вопрос {i}\nstatus: open\n"
                      "origin: work/programs/p/program.md\nopened: 2026-07-20\n---\n\n# q")
    mech_cases = {
        "подвешенное решение старше срока просится решаться": (
            {"work/programs/p/program.md": holder,
             "work/programs/p/decisions/d.md":
                "---\ntype: decision\nkey: P-D-1\ntitle: Выбрать формат\nstatus: proposed\n"
                "date: 2026-07-20\n---\n\n# d"},
            DECIDE_CONF, "пора решать", True),
        "принятое решение таймбоксом не поднимается": (
            {"work/programs/p/program.md": holder,
             "work/programs/p/decisions/d.md":
                "---\ntype: decision\nkey: P-D-1\ntitle: Выбрать формат\nstatus: accepted\n"
                "date: 2026-07-01\n---\n\n# d"},
            DECIDE_CONF, "пора решать", False),
        "крупному решению дают больше времени": (
            {"work/programs/p/program.md": holder,
             "work/programs/p/decisions/d.md":
                "---\ntype: decision\nkey: P-D-1\ntitle: Сменить нишу\nstatus: proposed\n"
                "weight: large\ndate: 2026-07-23\n---\n\n# d"},
            DECIDE_CONF, "пора решать", False),
        "контуров больше предела — перегрев": (
            {"work/programs/p/program.md": holder,
             "work/programs/q/program.md": second_holder},
            HEAT_CONF, "что замораживаем", True),
        "контуры в пределе — перегрева нет": (
            {"work/programs/p/program.md": holder},
            HEAT_CONF, "что замораживаем", False),
        "анализ копится без единого закрытого дела": (
            {"work/programs/p/program.md": holder,
             "work/programs/p/questions/q1.md": fresh_question.replace("{i}", "1"),
             "work/programs/p/questions/q2.md": fresh_question.replace("{i}", "2")},
            DRIFT_CONF, "анализ без действия", True),
        "закрытое на неделе дело гасит сигнал анализа": (
            {"work/programs/p/program.md": holder,
             "work/programs/p/questions/q1.md": fresh_question.replace("{i}", "1"),
             "work/programs/p/questions/q2.md": fresh_question.replace("{i}", "2"),
             "work/programs/p/commitments/c.md":
                "---\ntype: commitment\nkey: P-C-7\ntitle: Сделано\ndirection: outbound\n"
                "status: resolved\nstarted: 2026-07-20\nresolved: 2026-07-22\n"
                "resolution: work/programs/p/program.md\n"
                "origin: work/programs/p/program.md\n---\n\n# c"},
            DRIFT_CONF, "анализ без действия", False),
        "старый анализ сигнала не даёт": (
            {"work/programs/p/program.md": holder,
             "work/programs/p/questions/q1.md":
                fresh_question.replace("{i}", "1").replace("2026-07-20", "2026-05-01"),
             "work/programs/p/questions/q2.md":
                fresh_question.replace("{i}", "2").replace("2026-07-20", "2026-05-01")},
            DRIFT_CONF, "анализ без действия", False),
    }
    for title, (files, conf, needle, expect) in mech_cases.items():
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build(root, files)
            got_lines = attention.build_lines(store_mod.load(root, "work").notes, conf, TODAY)
            got = any(needle in line.text for line in got_lines)
            if got == expect:
                print(f"✓ {title}")
            else:
                label = "НЕ ПОЙМАН" if expect else "ЛОЖНАЯ ТРЕВОГА"
                failures.append(f"{label}: {title}\n    показано: "
                                f"{[line.text for line in got_lines] or '(тишина)'}")

    # Свежее событие контейнера в мире гасит «анализ без действия»: вопросы из
    # живой сессии с клиентом — работа, а не зависание
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"work/programs/p/program.md": holder,
                     "work/programs/p/questions/q1.md": fresh_question.replace("{i}", "1"),
                     "work/programs/p/questions/q2.md": fresh_question.replace("{i}", "2"),
                     "raw/meetings/2026-07-24-m.md":
                        "---\ntype: meeting\ndate: 2026-07-24\ncontainer: work/programs/p\n"
                        "source: plaud\nsource_ref: m\n---\n\n# m"})
        got_lines = attention.build_lines(store_mod.load(root, "work").notes, DRIFT_CONF,
                                          TODAY, store_mod.load(root, "raw").notes)
        if any("анализ без действия" in line.text for line in got_lines):
            failures.append("ЛОЖНАЯ ТРЕВОГА: свежая встреча контейнера не погасила «анализ без действия»")
        else:
            print("✓ свежая встреча контейнера гасит «анализ без действия»")

    # Ночной гейт и «наружу»: сигналы из журнала, а не из позиций. Профиль
    # человека (work/me/profil.md): ночная работа помечается на утренний
    # пересмотр, окно без внешнего события — сигнал «работа копится в столе».
    NIGHT_CONF = dict(CONFIG, night={"start": "23:00", "end": "06:00",
                                     "lookback_hours": 24,
                                     "ignore_markers": ["экран собран"]})
    OUT_CONF = dict(CONFIG, outbound={"window_days": 7,
                                      "log_markers": ["опубликовано", "отправлено"]})
    NOW = dt.datetime(2026, 7, 26, 9, 0)
    journal_cases = {
        "ночная запись поднимает пересмотр утром": (
            "- 2026-07-26T02:30:00 · источник принят · a.md\n",
            NIGHT_CONF, "пересмотреть утром", True),
        "дневная запись ночного сигнала не даёт": (
            "- 2026-07-25T15:00:00 · разобрано · a.md\n",
            NIGHT_CONF, "пересмотреть утром", False),
        "вечер после 23:00 — тоже ночь": (
            "- 2026-07-25T23:30:00 · разобрано · a.md\n",
            NIGHT_CONF, "пересмотреть утром", True),
        "ночная запись неделю назад уже не поднимается": (
            "- 2026-07-20T02:30:00 · источник принят · a.md\n",
            NIGHT_CONF, "пересмотреть утром", False),
        "техническая сборка ночью не считается ночной работой": (
            "- 2026-07-26T03:00:00 · экран собран · сигналов 4\n",
            NIGHT_CONF, "пересмотреть утром", False),
        "неделя без внешнего события — работа копится в столе": (
            "- 2026-07-10T10:00:00 · опубликовано · пост · a.md\n",
            OUT_CONF, "копится в столе", True),
        "свежая публикация гасит сигнал о столе": (
            "- 2026-07-25T10:00:00 · опубликовано · пост · a.md\n",
            OUT_CONF, "копится в столе", False),
        "дата в пути внутри строки не считается датой события": (
            "- 2026-07-10T10:00:00 · опубликовано · raw/2026-07-25-a.md\n",
            OUT_CONF, "копится в столе", True),
    }
    for title, (journal, conf, needle, expect) in journal_cases.items():
        got_lines = attention.build_lines([], conf, TODAY, [], None,
                                          log_entries=activity.parse_text(journal),
                                          now=NOW)
        got = any(needle in line.text for line in got_lines)
        if got == expect:
            print(f"✓ {title}")
        else:
            label = "НЕ ПОЙМАН" if expect else "ЛОЖНАЯ ТРЕВОГА"
            failures.append(f"{label}: {title}\n    показано: "
                            f"{[line.text for line in got_lines] or '(тишина)'}")

    # Встреча — тоже событие наружу, и она живёт в raw/, а не в журнале
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"raw/meetings/2026-07-24-m.md":
                     "---\ntype: meeting\ndate: 2026-07-24\ncontainer: work/programs/p\n"
                     "source: plaud\nsource_ref: m\n---\n\n# m"})
        got_lines = attention.build_lines([], OUT_CONF, TODAY,
                                          store_mod.load(root, "raw").notes, None,
                                          log_entries=[], now=NOW)
        if any("копится в столе" in line.text for line in got_lines):
            failures.append("ЛОЖНАЯ ТРЕВОГА: свежая встреча в raw/ не погасила сигнал о столе")
        else:
            print("✓ свежая встреча в raw/ гасит сигнал о столе")

    # Самоанализ: только реальное представление и явная реакция. Техническая
    # пересборка и правка файла больше не притворяются использованием.
    SHOW = "- 2026-07-01T08:30:00 · представлен · обещание · work/a.md\n"
    reflects = {
        "явная реакция после представления считается отдачей": (
            SHOW + "- 2026-07-01T09:00:00 · реакция · взято · work/a.md\n",
            "обещание", 1, 1, 1),
        "реакция в ту же секунду считается по порядку строк журнала": (
            SHOW + "- 2026-07-01T08:30:00 · реакция · взято · work/a.md\n",
            "обещание", 1, 1, 1),
        "реакция до представления отдачей не считается": (
            "- 2026-07-01T07:00:00 · реакция · взято · work/a.md\n" + SHOW,
            "обещание", 1, 0, 0),
        "реакция позже окна отдачей не считается": (
            SHOW + "- 2026-07-10T09:00:00 · реакция · взято · work/a.md\n",
            "обещание", 1, 0, 0),
        "одна реакция не оплачивает два повторных представления": (
            SHOW
            + "- 2026-07-02T08:30:00 · представлен · обещание · work/a.md\n"
            + "- 2026-07-02T09:00:00 · реакция · взято · work/a.md\n",
            "обещание", 2, 1, 1),
        "отложено — реакция, но не полезное действие": (
            SHOW + "- 2026-07-01T09:00:00 · реакция · отложено · work/a.md\n",
            "обещание", 1, 0, 1),
        "старое техническое «показано» не входит в новую выборку": (
            "- 2026-07-01T08:30:00 · показано · обещание · work/a.md\n",
            "обещание", 0, 0, 0),
    }
    for title, (log, kind, shown, acted, responded) in reflects.items():
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build(root, {"raw/log.md": log})
            presentations, reactions = reflect.read_log(root)
            signals = reflect.analyse(presentations, reactions, window_days=3)
            got = signals.get(kind)
            if shown == 0:
                ok = got is None
            else:
                ok = bool(got and got.shown == shown and got.acted == acted
                          and got.responded == responded)
            if ok:
                print(f"✓ {title}")
            else:
                failures.append(f"НЕ СОШЛОСЬ: {title}\n    ожидалось представлений "
                                f"{shown}, действий {acted}, реакций {responded}; "
                                f"получено {got}")

    # Трекер: уровень обязательности, лимит, зацепка, ёмкость
    TCONF = {"tracker": {"wip_limit": 2, "self_slot": True, "candidates": 3,
                         "pickup_window_days": 7},
             "capacity": {"full": 3, "half": 2, "low": 1}}

    def commit(key, level, status="open", when_then=True, due=None):
        wt = ('when_then:\n  cue: "сяду за проект"\n  action: "сделаю первый шаг"\n'
              if when_then else "")
        return (f"---\ntype: commitment\nkey: {key}\ntitle: Дело {key}\ndirection: outbound\n"
                f"level: {level}\nstatus: {status}\n{wt}"
                f"{'due: ' + due + chr(10) if due else ''}"
                "origin: work/programs/p/program.md\n---\n\n# c")

    tracks = {
        "«дальше» и «когда-нибудь» не предлагаются": (
            {"work/programs/p/commitments/a.md": commit("A", "next"),
             "work/programs/p/commitments/b.md": commit("B", "later")},
            "pick", "В «сейчас» пусто"),
        "с зацепкой предлагается впереди без зацепки": (
            {"work/programs/p/commitments/a.md": commit("A", "now", when_then=False),
             "work/programs/p/commitments/b.md": commit("B", "now")},
            "pick", "Дело B"),
        "без зацепки система говорит, чего не хватает": (
            {"work/programs/p/commitments/a.md": commit("A", "now", when_then=False)},
            "pick", "нет зацепки"),
        "лимит незавершённого останавливает набор": (
            {"work/programs/p/commitments/a.md": commit("A", "now", status="in-progress"),
             "work/programs/p/commitments/b.md": commit("B", "now", status="in-progress"),
             "work/programs/p/commitments/c.md": commit("C", "now")},
            "pick", "Ничего не предлагаю"),
        "ёмкость low сужает до одного": (
            {"work/programs/p/commitments/a.md": commit("A", "now"),
             "work/programs/p/commitments/b.md": commit("B", "now"),
             "work/programs/p/commitments/c.md": commit("C", "now")},
            "pick-low", "ещё 2 в «сейчас»"),
        "пустое место за собой заметно": (
            {"work/programs/p/commitments/a.md": commit("A", "now", status="in-progress")},
            "status", "Место за собой свободно"),
        "ждущее ответа показано отдельно": (
            {"work/programs/p/commitments/a.md": commit("A", "now", status="waiting")},
            "status", "ждёт ответа"),
    }
    for title, (files, mode, expected) in tracks.items():
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build(root, dict(files, **{"work/programs/p/program.md": holder}))
            notes = store_mod.load(root, 'work').notes
            if mode == "status":
                lines = tracker.status(notes, TCONF)
            else:
                cap = "low" if mode == "pick-low" else "full"
                lines = tracker.pick(notes, TCONF, dt.date(2026, 7, 26), cap)
            if any(expected in line for line in lines):
                print(f"✓ {title}")
            else:
                failures.append(f"НЕ СОШЛОСЬ: {title}\n    ожидалось {expected!r}, получено {lines}")

    # Досье: наблюдение наследует контейнер от источника, иначе «первое интервью» при полном складе
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {
            "work/clients/x/client.md": "---\ntype: client\nmode: active\nprefix: X\ntitle: Клиент X\n---\n\n# x",
            "raw/meetings/2026-07-24-vstrecha.md":
                '---\ntype: meeting\ndate: 2026-07-24\ncontainer: work/clients/x\n'
                'source_ref: "s"\n---\n\n# m',
            "raw/observations/2026-07-24-nablyudenie.md":
                "---\ntype: observation\nsource: raw/meetings/2026-07-24-vstrecha.md\n"
                "kind: behaviour\ncodes: [ручной-перенос]\n---\n\n# o",
        })
        lines = interview.brief(root, "work/clients/x", None)
        text = "\n".join(lines)
        if "наблюдений: 1" in text and "ручной-перенос" in text:
            print("✓ наблюдение наследует контейнер от источника")
        else:
            failures.append("НЕ СОШЛОСЬ: досье не увидело наблюдение через источник\n"
                            f"    получено: {text[-300:]}")

    # Ёмкость: предохранитель на устаревшие данные важнее самой оценки
    def rows(days_ago_from, days_ago_to, value, today):
        return [(today - dt.timedelta(days=d), value)
                for d in range(days_ago_from, days_ago_to)]

    TODAY = dt.date(2026, 7, 26)
    caps = {
        "устаревшая выгрузка не даёт ёмкость, а называет причину": (
            rows(130, 190, 50.0, TODAY), rows(130, 190, 60.0, TODAY), "unknown", "устарела"),
        "свежие данные на своём уровне — полная ёмкость": (
            rows(1, 60, 50.0, TODAY), rows(1, 60, 60.0, TODAY), "full", None),
        "вариабельность ниже базы и пульс выше — низкая ёмкость": (
            rows(1, 8, 30.0, TODAY) + rows(8, 60, 50.0, TODAY),
            rows(1, 8, 70.0, TODAY) + rows(8, 60, 60.0, TODAY), "low", None),
        "хуже только один показатель — половина": (
            rows(1, 8, 30.0, TODAY) + rows(8, 60, 50.0, TODAY),
            rows(1, 60, 60.0, TODAY), "half", None),
        "нет источника — честное «не определена»": (
            [], [], "unknown", "недоступен"),
        "одна-две точки не считаются личной базой": (
            rows(1, 3, 50.0, TODAY), rows(1, 3, 60.0, TODAY), "unknown", "меньше"),
    }
    for title, (hrv, rhr, expected, fragment) in caps.items():
        got = capacity.assess(hrv, rhr, TODAY)
        ok = got.level == expected and (fragment is None or fragment in got.reason)
        if ok:
            print(f"✓ {title}")
        else:
            failures.append(f"НЕ СОШЛОСЬ: {title}\n    ожидалось {expected}"
                            f"{'/' + fragment if fragment else ''}, получено {got.level}: {got.reason}")

    # Сон участвует в оценке и называется в объяснении
    sleep_caps = {
        "недосып при нормальных прочих — половина": (
            rows(1, 60, 50.0, TODAY), rows(1, 60, 60.0, TODAY),
            rows(1, 8, 5.0, TODAY) + rows(8, 60, 7.5, TODAY), "half", "сон"),
        "недосып плюс низкая вариабельность — низкая ёмкость": (
            rows(1, 8, 30.0, TODAY) + rows(8, 60, 50.0, TODAY), rows(1, 60, 60.0, TODAY),
            rows(1, 8, 5.0, TODAY) + rows(8, 60, 7.5, TODAY), "low", "сон"),
        "сон на своём уровне — полная, и он назван": (
            rows(1, 60, 50.0, TODAY), rows(1, 60, 60.0, TODAY),
            rows(1, 60, 7.0, TODAY), "full", "сон"),
    }
    for title, (hrv, rhr, sleep, expected, fragment) in sleep_caps.items():
        got = capacity.assess(hrv, rhr, TODAY, sleep=sleep)
        if got.level == expected and fragment in got.reason:
            print(f"✓ {title}")
        else:
            failures.append(f"НЕ СОШЛОСЬ: {title}\n    ожидалось {expected}/{fragment}, "
                            f"получено {got.level}: {got.reason}")

    # Склейка выгрузок: история из разовой + свежее из дневных, переименование папки не ломает
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        big = root / "history"
        daily = root / "sync" / "Как угодно названная автоматизация"
        big.mkdir(parents=True)
        daily.mkdir(parents=True)

        def export(days):
            return json.dumps({"data": {"metrics": [
                {"name": "heart_rate_variability",
                 "data": [{"date": f"{d} 00:00:00 +0300", "qty": q} for d, q in days]}]}})

        (big / "HealthAutoExport-2026-04-01-2026-07-01.json").write_text(
            export([("2026-06-30", 40.0), ("2026-07-01", 41.0)]), encoding="utf-8")
        (daily / "HealthAutoExport-2026-07-26.json").write_text(
            export([("2026-07-26", 55.0)]), encoding="utf-8")
        (daily / "HealthAutoExport-broken.json").write_text("{не json", encoding="utf-8")

        merged = capacity._from_export(big, root / "sync")
        days = [d for d, _ in merged["hrv"]]
        if len(days) == 3 and days[-1] == dt.date(2026, 7, 26):
            print("✓ выгрузки склеиваются, битый файл не роняет оценку")
        else:
            failures.append(f"НЕ СОШЛОСЬ: склейка выгрузок, получено {merged['hrv']}")

    # Самоанализ больше не зависит от git: реакция человека наблюдается напрямую.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"raw/log.md":
                         "- 2026-07-01T08:30:00 · представлен · кандидат · work/a.md\n"
                         "- 2026-07-01T09:00:00 · реакция · взято · work/a.md\n"})
        presentations, reactions = reflect.read_log(root)
        signals = reflect.analyse(presentations, reactions, 3)
        if signals["кандидат"].acted == 1:
            print("✓ самоанализ работает вне git по явным реакциям")
        else:
            failures.append("НЕ СОШЛОСЬ: самоанализ всё ещё зависит от git")

    # Заход и ответ человека — разные факты. Молчание канала (задача не дошла до
    # склада) обязано отличаться и от спокойного дня, и от дня без ответа.
    TOUCH_TODAY = dt.date(2026, 7, 3)
    MORNING = "- 2026-07-02T10:30:00 · касание · утро\n"
    EVENING = "- 2026-07-02T22:30:00 · касание · вечер\n"
    SHOWN = "- 2026-07-02T10:31:00 · представлен · кандидат · work/a.md\n"
    # Ожидание: (пропущенные касания, был ли ответ) либо None — дня нет в окне.
    touch_cases = {
        "день с обоими касаниями пропуском не считается":
            (MORNING + EVENING, ([], False)),
        "вечер не состоялся — день назван поимённо":
            (MORNING, (["вечер"], False)),
        "заход состоялся, но человек не ответил — это видно отдельно":
            (MORNING + EVENING + SHOWN, ([], False)),
        "ответ человека засчитан тому дню, в который он прозвучал":
            (MORNING + EVENING + SHOWN
             + "- 2026-07-02T11:00:00 · реакция · взято · work/a.md\n",
             ([], True)),
        "сегодняшний день в окно не входит: вечер ещё не наступил":
            ("- 2026-07-03T10:30:00 · касание · утро\n", None),
    }
    for title, (log, expected) in touch_cases.items():
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build(root, {"raw/log.md": log})
            _, reactions = reflect.read_log(root)
            rows = reflect.touch_days(reflect.read_touches(root), reactions,
                                      TOUCH_TODAY)
            row = next((one for one in rows if one.day == dt.date(2026, 7, 2)), None)
            got = None if row is None else (row.missing, row.answered)
            if got == expected:
                print(f"✓ {title}")
            else:
                failures.append(f"НЕ СОШЛОСЬ: {title}\n    ожидалось {expected}, "
                                f"получено {got}")

    # Дни до первой отметки пропусками не объявляются: наблюдения тогда не было.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"raw/log.md": "- 2026-07-02T10:30:00 · касание · утро\n"})
        _, reactions = reflect.read_log(root)
        rows = reflect.touch_days(reflect.read_touches(root), reactions, TOUCH_TODAY)
        if [row.day for row in rows] == [dt.date(2026, 7, 2)]:
            print("✓ дни до первого касания пропуском не считаются")
        else:
            failures.append("ЛОЖНАЯ ТРЕВОГА: пропуски выдуманы за дни без наблюдения "
                            f"— {[str(row.day) for row in rows]}")

    # Пустой журнал не должен притворяться исправной работой канала.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"raw/log.md": ""})
        _, reactions = reflect.read_log(root)
        line = reflect.touch_line(
            reflect.touch_days(reflect.read_touches(root), reactions, TOUCH_TODAY))
        if "ещё не отмечались" in line:
            print("✓ без единого касания система говорит «не отмечались», а не «всё хорошо»")
        else:
            failures.append(f"НЕ СОШЛОСЬ: пустой журнал дал строку «{line}»")

    # Одна работа кормит несколько контуров: закрытое дело — ещё и доказательство
    # навыка, и материал наружу. Но предлагать надо по основанию, а не по щупу.
    skill_card = ("---\ntype: skill\nkey: ASR-K-BP02\nskill_id: BP02\n"
                  "title: \"Process Mapping\"\ndomains: [business-processes]\n"
                  "target: 4\nstatus: specified\n---\n\n# BP02. Карта процесса\n")
    other_skill = ("---\ntype: skill\nkey: ASR-K-X\nskill_id: X01\n"
                   "title: \"Answering for Another\"\ndomains: [leadership]\n"
                   "target: 3\nstatus: specified\n---\n\n# X01. Ответ за чужой провал\n")

    def closed(title: str, resolution: str = "raw/sources/2026-08-01-x.md") -> str:
        return (f"---\ntype: commitment\nkey: P-C-9\ntitle: \"{title}\"\n"
                "direction: outbound\nlevel: now\nstatus: resolved\n"
                "started: 2026-08-01\nresolved: 2026-08-05\n"
                f"resolution: {resolution}\n"
                "origin: work/programs/p/program.md\n---\n\n# c")

    harvest_files = {
        "work/programs/p/program.md": holder,
        "work/programs/p/skills/bp02.md": skill_card,
        "work/programs/p/skills/x01.md": other_skill,
    }
    harvest_cases = {
        "два совпавших слова дают кандидата в доказательства": (
            closed("Построил карту процесса платежей"), ["Process Mapping"]),
        "одно совпавшее слово кандидатом не считается": (
            closed("Ответ Кофейня по деньгам"), []),
        "закрытое наружу предлагает материал": (
            closed("Построил карту процесса платежей",
                   "raw/meetings/2026-08-05-m.md"), ["Process Mapping"]),
    }
    for title, (body, expected) in harvest_cases.items():
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build(root, dict(harvest_files,
                             **{"work/programs/p/commitments/done.md": body}))
            notes = store_mod.load(root, "work").notes
            note = next(n for n in notes if n.rel.endswith("done.md"))
            got = harvest.collect(note, notes)
            names = [hit.skill.title for hit in got.skills]
            if names == expected:
                print(f"✓ {title}")
            else:
                failures.append(f"НЕ СОШЛОСЬ: {title}\n    ожидалось {expected}, "
                                f"получено {names}")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, dict(harvest_files, **{
            "work/programs/p/commitments/done.md":
                closed("Построил карту процесса платежей", "raw/meetings/2026-08-05-m.md"),
        }))
        notes = store_mod.load(root, "work").notes
        note = next(n for n in notes if n.rel.endswith("done.md"))
        text = "\n".join(harvest.render(harvest.collect(note, notes)))
        checks = [
            ("работа наружу предлагает материал", "материал для внешнего" in text),
            ("названо, чем совпало", "совпало:" in text),
        ]
        for name, ok in checks:
            if ok:
                print(f"✓ {name}")
            else:
                failures.append(f"НЕ СОШЛОСЬ: {name}\n    {text}")

    # С одной работы урожай собирается один раз: заведённое доказательство
    # закрывает вопрос, иначе система будет предлагать то же каждую неделю.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        rel = "work/programs/p/commitments/done.md"
        build(root, dict(harvest_files, **{
            rel: closed("Построил карту процесса платежей"),
            "work/programs/p/evidence/e1.md":
                ("---\ntype: evidence\nskill: work/programs/p/skills/bp02.md\n"
                 f"level: 3\nresult: собрал карту\ndate: 2026-08-05\norigin: {rel}\n"
                 "context: real-world\n---\n\n# e"),
        }))
        notes = store_mod.load(root, "work").notes
        note = next(n for n in notes if n.rel == rel)
        got = harvest.collect(note, notes)
        if got.already and not got.skills:
            print("✓ с одной работы урожай собирается один раз")
        else:
            failures.append(f"НЕ СОШЛОСЬ: повторный урожай — {got.skills}")

    # Два рабочих места: конфликтов не должно быть, а не «должны красиво
    # разрешаться». Всё держится на том, что в каждый момент пишет один.
    SYNC_NOW = dt.datetime(2026, 8, 9, 12, 0, 0)
    presence_cases = {
        "свежая отметка ноутбука — сервер в позиции не пишет": (
            "- 2026-08-09T11:50:00 · присутствие · ноутбук\n", True),
        "отметка получасовой давности — ноутбук считается закрытым": (
            "- 2026-08-09T11:20:00 · присутствие · ноутбук\n", False),
        "отметок нет вовсе — сервер волен работать": ("", False),
        "чужие строки журнала присутствием не считаются": (
            "- 2026-08-09T11:55:00 · экран собран · сигналов 7\n", False),
        "берётся последняя отметка, а не первая": (
            ("- 2026-08-09T09:00:00 · присутствие · ноутбук\n"
             "- 2026-08-09T11:55:00 · присутствие · ноутбук\n"), True),
    }
    for title, (log, expected) in presence_cases.items():
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build(root, {"raw/log.md": log})
            было = sync.LOG
            sync.LOG = root / "raw" / "log.md"
            try:
                got = sync.laptop_active(now=SYNC_NOW)
            finally:
                sync.LOG = было
            if got == expected:
                print(f"✓ {title}")
            else:
                failures.append(f"НЕ СОШЛОСЬ: {title}\n    ожидалось {expected}, "
                                f"получено {got}")

    # Отложенное намерение — отдельный файл, а не правка позиции: конфликт
    # возможен там, где двое трогают одно, а новый файл не трогает ничего.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"raw/log.md": ""})
        было = sync.INTENTS
        sync.INTENTS = root / "raw" / "intents"
        try:
            path = sync.park_intent("закрыть обязательство work/a.md", now=SYNC_NOW)
            text = path.read_text(encoding="utf-8")
        finally:
            sync.INTENTS = было
        checks = [
            ("намерение легло отдельным файлом", path.is_file()),
            ("намерение — событие, а не позиция", "type: source" in text),
            ("сказано, почему не применено сразу", "работал человек" in text),
        ]
        for name, ok in checks:
            if ok:
                print(f"✓ {name}")
            else:
                failures.append(f"НЕ СОШЛОСЬ: {name}")

    # Виды при расхождении не сливаются, а пересобираются — иначе выйдет
    # таблица, которой не соответствует ни одно состояние склада.
    view_cases = {
        "только виды — можно пересобрать": (["wiki/attention.md",
                                             "raw/index.md"], True),
        "виды вперемешку с позицией — пересобрать нельзя": (
            ["wiki/attention.md", "work/me/commitments/a.md"], False),
        "пусто — пересобирать нечего": ([], False),
    }
    for title, (files, expected) in view_cases.items():
        if sync.derived_only(files) == expected:
            print(f"✓ {title}")
        else:
            failures.append(f"НЕ СОШЛОСЬ: {title} — {files}")

    # TickTick — канал действия. Из приложения возвращается факт, но не всегда
    # разрешение: одинаковый след может значить четыре разных вещи.
    TT_TODAY = dt.date(2026, 8, 8)

    def commitment(key: str, *, level: str = "now", status: str = "open",
                   due: str | None = None, external: str | None = None,
                   review: str | None = None,
                   scheduled_start: str | None = None,
                   scheduled_end: str | None = None,
                   calendar_event: str | None = None) -> str:
        head = (f"---\ntype: commitment\nkey: {key}\ntitle: Дело {key}\n"
                f"direction: outbound\nlevel: {level}\nstatus: {status}\n")
        for name, value in (
            ("due", due), ("ticktick", external), ("review", review),
            ("scheduled_start", scheduled_start),
            ("scheduled_end", scheduled_end),
            ("calendar_event", calendar_event),
        ):
            if value:
                head += f"{name}: {value}\n"
        return head + "origin: work/programs/p/program.md\n---\n\n# c"

    outgoing_cases = {
        "обязательство «сейчас» уходит в канал действия":
            (commitment("A"), 1),
        "уже связанное второй раз не отправляется":
            (commitment("A", external="tt-1"), 0),
        "«дальше» в канал действия не попадает":
            (commitment("A", level="next"), 0),
        "«дальше» с близким сроком — попадает":
            (commitment("A", level="next", due="2026-08-10"), 1),
        "отложенное человеком в приложение не тащим":
            (commitment("A", review="2026-08-20"), 0),
        "закрытое не отправляется":
            (commitment("A", status="resolved"), 0),
        "ожидание чужого ответа — не дело, в трекер не уходит":
            (commitment("A", status="waiting"), 0),
    }
    for title, (body, expected) in outgoing_cases.items():
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build(root, {"work/programs/p/program.md": holder,
                         "work/programs/p/commitments/a.md": body})
            got = ticktick.outgoing(store_mod.load(root, "work").notes, TT_TODAY)
            if len(got) == expected:
                print(f"✓ {title}")
            else:
                failures.append(f"НЕ СОШЛОСЬ: {title}\n    ожидалось {expected}, "
                                f"получено {len(got)}")

    # Постоянная сверка сама заводит актуальные обязательства. Второй проход
    # видит записанную связь и не создаёт копию; next без близкого срока
    # остаётся только в складе.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        rel_now = "work/programs/p/commitments/now.md"
        rel_soon = "work/programs/p/commitments/soon.md"
        rel_next = "work/programs/p/commitments/next.md"
        build(root, {
            "work/programs/p/program.md": holder,
            rel_now: commitment("NOW"),
            rel_soon: commitment("SOON", level="next", due="2026-08-10"),
            rel_next: commitment("NEXT", level="next"),
        })
        external: dict[str, dict] = {}
        original_project = ticktick.project_id
        original_tasks = ticktick.fetch_tasks
        original_call = ticktick.call

        ticktick.project_id = lambda _root: "project-1"
        ticktick.fetch_tasks = lambda _project: external

        def create_task(path, *, method="GET", body=None):
            if path != "/task" or method != "POST" or body is None:
                raise AssertionError(f"неожиданный вызов TickTick: {method} {path}")
            task_id = f"tt-{len(external) + 1}"
            external[task_id] = dict(body, id=task_id, status=0)
            return {"id": task_id}

        ticktick.call = create_task
        try:
            first = source_sync.ticktick_once(root, TT_TODAY)
            second = source_sync.ticktick_once(root, TT_TODAY)
        finally:
            ticktick.project_id = original_project
            ticktick.fetch_tasks = original_tasks
            ticktick.call = original_call
        by_rel = store_mod.load(root, "work").by_rel
        if (first.ok and first.pushed == 2 and second.ok and second.pushed == 0
                and len(external) == 2
                and by_rel[rel_now].data.get("ticktick")
                and by_rel[rel_soon].data.get("ticktick")
                and not by_rel[rel_next].data.get("ticktick")):
            print("✓ постоянная сверка отправляет актуальное в TickTick один раз")
        else:
            failures.append("НЕ СОШЛОСЬ: автоматическая отправка создала дубль или "
                            f"потеряла отбор — {first}/{second}/{external}/{by_rel}")

    # Связанная задача не замирает после первого создания: склад продолжает
    # владеть её формулировкой и сроком, а обновление идёт в тот же id.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        rel = "work/programs/p/commitments/a.md"
        build(root, {"work/programs/p/program.md": holder,
                     rel: commitment("A", due="2026-08-12", external="tt-1")})
        note = store_mod.load(root, "work").by_rel[rel]
        tasks = {"tt-1": {
            "id": "tt-1", "projectId": "project-1", "title": "Старый текст",
            "content": "старое", "status": 0, "isAllDay": True,
            "dueDate": "2026-08-10T09:00:00+0300",
        }}
        calls: list[tuple[str, str, dict]] = []
        original_call = ticktick.call

        def update_call(path, *, method="GET", body=None):
            calls.append((path, method, body or {}))
            return body or {}

        ticktick.call = update_call
        try:
            changed = ticktick.sync_existing("project-1", [note], tasks)
            repeated = ticktick.sync_existing("project-1", [note], tasks)
        finally:
            ticktick.call = original_call
        body = calls[0][2] if calls else {}
        if (changed == [rel] and not repeated and len(calls) == 1
                and calls[0][0] == "/task/tt-1"
                and body.get("title") == "Дело A"
                and str(body.get("dueDate", "")).startswith("2026-08-12")
                and ticktick.MARK + rel in str(body.get("content") or "")):
            print("✓ связанная задача TickTick обновляется на месте и второй раз молчит")
        else:
            failures.append("НЕ СОШЛОСЬ: связанная задача замерла или обновилась дважды — "
                            f"{changed}/{repeated}/{calls}")

    # Ушедшее в ожидание снимается с трекера: чужой ответ, показанный красной
    # датой, читается как собственная просрочка. Связь стирается, поэтому
    # возврат командой `resume` заводит задачу заново, а не воскрешает старую.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        rel = "work/programs/p/commitments/a.md"
        build(root, {"work/programs/p/program.md": holder,
                     rel: commitment("A", status="waiting", due="2026-08-08",
                                     external="tt-1")})
        note = store_mod.load(root, "work").by_rel[rel]
        tasks = {"tt-1": {"id": "tt-1", "projectId": "project-1",
                          "title": "Дело A", "status": 0}}
        calls: list[tuple[str, str]] = []
        original_call = ticktick.call

        def drop_call(path, *, method="GET", body=None):
            calls.append((path, method))
            return {}

        ticktick.call = drop_call
        try:
            gone = ticktick.withdraw_waiting(root, "project-1", [note], tasks)
            again = ticktick.withdraw_waiting(
                root, "project-1", store_mod.load(root, "work").notes, tasks)
        finally:
            ticktick.call = original_call
        after = store_mod.load(root, "work").by_rel[rel]
        back = ticktick.outgoing([after], TT_TODAY)
        resumed = commitment("A", status="in-progress", due="2026-08-08")
        (root / rel).write_text(resumed, encoding="utf-8")
        returned = ticktick.outgoing(store_mod.load(root, "work").notes, TT_TODAY)
        if (gone == [rel] and not again and calls == [("/project/project-1/task/tt-1",
                                                       "DELETE")]
                and not after.data.get("ticktick") and not tasks
                and not back and len(returned) == 1):
            print("✓ ожидание снимается с трекера, возврат заводит задачу заново")
        else:
            failures.append("НЕ СОШЛОСЬ: ожидание осталось в трекере или связь пережила "
                            f"снятие — {gone}/{again}/{calls}/{len(returned)}")

    # Явная галочка человека — достаточное подтверждение состояния. Сначала
    # сохраняется неизменяемое событие, затем оно становится доказательством
    # завершения; повтор уже упирается в resolved.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        rel = "work/programs/p/commitments/a.md"
        build(root, {"raw/log.md": "---\ntype: log\ntitle: журнал\n---\n\n# журнал\n",
                     "work/programs/p/program.md": holder,
                     rel: commitment("A", external="tt-done")})
        task = {"id": "tt-done", "title": "Дело A", "status": 2,
                "completedTime": "2026-08-12T10:00:00+0300",
                "modifiedTime": "2026-08-12T10:00:00+0300"}
        notes = store_mod.load(root, "work").notes
        back = ticktick.incoming(notes, {"tt-done": task}, TT_TODAY)
        batch = ticktick.capture_external_batch(root, {"tt-done": task}, back)
        first = ticktick.apply_incoming(
            root, back, {"tt-done": task}, batch.evidence, TT_TODAY)
        second = ticktick.apply_incoming(
            root, back, {"tt-done": task}, batch.evidence, TT_TODAY)
        note = store_mod.load(root, "work").by_rel[rel]
        proof = str(note.data.get("resolution") or "")
        if (first == [rel] and second == [rel] and note.status == "resolved"
                and proof.startswith("raw/inbox/") and (root / proof).is_file()):
            print("✓ галочка TickTick автономно закрывает дело с сохранённым доказательством")
        else:
            failures.append("НЕ СОШЛОСЬ: галочка потерялась или закрыла без источника — "
                            f"{first}/{second}/{note.data}")

    # Явное время создаёт один календарный блок. Затем календарь владеет
    # временем: перенос возвращается в TickTick; удаление снимает планирование,
    # но оставляет обязательство открытым.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        rel = "work/programs/p/commitments/a.md"
        build(root, {"work/programs/p/program.md": holder,
                     rel: commitment("A", due="2026-08-12", external="tt-time")})
        task = {"id": "tt-time", "projectId": "project-1", "title": "Дело A",
                "content": ticktick.MARK + rel, "status": 0, "isAllDay": False,
                "startDate": "2026-08-12T14:00:00+0300",
                "dueDate": "2026-08-12T14:25:00+0300",
                "modifiedTime": "2026-08-12T09:00:00Z"}
        tasks = {"tt-time": task}
        external_events: list[agenda.Event] = []
        created_calls: list[str] = []
        task_moves: list[tuple[dt.datetime, dt.datetime]] = []
        cleared: list[str] = []
        original_project = ticktick.project_id
        original_tasks = ticktick.fetch_tasks
        original_fetch_blocks = agenda.fetch_timeblocks
        original_create = agenda.create_timeblock
        original_update = agenda.update_timeblock
        original_delete = agenda.delete_timeblock
        original_set = ticktick.set_schedule
        original_clear = ticktick.clear_schedule
        ticktick.project_id = lambda _root: "project-1"
        ticktick.fetch_tasks = lambda _project: tasks
        agenda.fetch_timeblocks = lambda **_kwargs: external_events

        def fake_event(note, task_id, start, end, event_id="cal-1"):
            return agenda.Event(
                start=start, end=end, title=note.title, source_id=event_id,
                updated="2026-08-12T09:00:00Z", sync_path=note.rel,
                sync_task=task_id,
                sync_revision=agenda.timeblock_revision(
                    note.rel, task_id, note.title, start, end),
            )

        def create_block(note, task_id, start, end):
            created_calls.append(note.rel)
            event = fake_event(note, task_id, start, end)
            external_events[:] = [event]
            return event

        def update_block(event, note, task_id, start, end):
            changed = fake_event(note, task_id, start, end, event.source_id)
            changed.updated = "2026-08-12T12:00:00Z"
            external_events[:] = [changed]
            return changed

        def move_task(_project, current, start, end):
            task_moves.append((start, end))
            current.update({"startDate": ticktick.stamp(start),
                            "dueDate": ticktick.stamp(end), "isAllDay": False,
                            "modifiedTime": "2026-08-12T11:00:00Z"})
            return current

        def clear_task(_project, current, day):
            cleared.append(str(day or ""))
            current.update({"startDate": None, "dueDate": (
                f"{day:%Y-%m-%d}T09:00:00+0300" if day else None),
                "isAllDay": True})
            return current

        agenda.create_timeblock = create_block
        agenda.update_timeblock = update_block
        agenda.delete_timeblock = lambda event_id: external_events.clear()
        ticktick.set_schedule = move_task
        ticktick.clear_schedule = clear_task
        try:
            created_result = source_sync.schedule_once(root, TT_TODAY)
            repeat_result = source_sync.schedule_once(root, TT_TODAY)
            moved_event = external_events[0]
            moved_event.start = dt.datetime(2026, 8, 12, 16, 0)
            moved_event.end = dt.datetime(2026, 8, 12, 16, 25)
            moved_event.updated = "2026-08-12T12:00:00Z"
            moved_result = source_sync.schedule_once(root, TT_TODAY)
            external_events[0].status = "cancelled"
            external_events[0].updated = "2026-08-12T13:00:00Z"
            removed_result = source_sync.schedule_once(root, TT_TODAY)
        finally:
            ticktick.project_id = original_project
            ticktick.fetch_tasks = original_tasks
            agenda.fetch_timeblocks = original_fetch_blocks
            agenda.create_timeblock = original_create
            agenda.update_timeblock = original_update
            agenda.delete_timeblock = original_delete
            ticktick.set_schedule = original_set
            ticktick.clear_schedule = original_clear
        loaded_schedule = store_mod.load(root, "work")
        note = loaded_schedule.by_rel.get(rel)
        if (created_result.created == 1 and repeat_result.created == 0
                and repeat_result.updated == 0 and created_calls == [rel]
                and moved_result.updated >= 1 and task_moves
                and task_moves[-1][0] == dt.datetime(2026, 8, 12, 16, 0)
                and removed_result.removed == 1 and cleared
                and note is not None and note.status == "open"
                and not note.data.get("calendar_event")
                and not note.data.get("scheduled_start")):
            print("✓ время TickTick и календаря сходится, удаление не отменяет дело")
        else:
            failures.append("НЕ СОШЛОСЬ: календарный блок дублируется или меняет статус — "
                            f"{created_result}/{repeat_result}/{moved_result}/"
                            f"{removed_result}/{task_moves}/{cleared}/"
                            f"{note.data if note else loaded_schedule.unreadable}/"
                            f"{(root / rel).read_text(encoding='utf-8')}")

    # Служебная метка отличает собственную запись от действия человека.
    start = dt.datetime(2026, 8, 12, 14, 0)
    end = dt.datetime(2026, 8, 12, 14, 25)
    own = agenda.Event(
        start=start, end=end, title="Дело A", source_id="cal-echo",
        sync_path="work/a.md", sync_task="tt-a",
        sync_revision=agenda.timeblock_revision(
            "work/a.md", "tt-a", "Дело A", start, end),
    )
    moved = agenda.Event(**{**own.__dict__, "start": start + dt.timedelta(hours=1),
                            "end": end + dt.timedelta(hours=1)})
    if not agenda.worth_capturing(own) and agenda.worth_capturing(moved):
        print("✓ собственный календарный эхосигнал молчит, ручной перенос сохраняется")
    else:
        failures.append("НЕ СОШЛОСЬ: календарь не отличил своё эхо от переноса")
    generated_event_id = agenda.event_id("work/programs/p/commitments/a.md")
    if (5 <= len(generated_event_id) <= 1024
            and all(char in "0123456789abcdefghijklmnopqrstuv"
                    for char in generated_event_id)):
        print("✓ календарный id проходит ограниченный алфавит Google")
    else:
        failures.append(f"НЕ СОШЛОСЬ: Google отвергнет event id {generated_event_id}")

    # Пустой ответ удаления gws считает файлом. В постоянном клоне он не должен
    # оставлять download.html после каждого снятого блока.
    original_run = agenda.subprocess.run
    delete_command: list[str] = []

    class Deleted:
        returncode = 0
        stdout = "{}"
        stderr = ""

    def delete_run(command, **_kwargs):
        delete_command.extend(command)
        return Deleted()

    agenda.subprocess.run = delete_run
    try:
        agenda.delete_timeblock("cal-test")
    finally:
        agenda.subprocess.run = original_run
    if "--output" in delete_command and os.devnull in delete_command:
        print("✓ удаление календарного блока не оставляет download.html")
    else:
        failures.append(f"НЕ СОШЛОСЬ: gws засорит склад после удаления — {delete_command}")

    # Сервер обязан иметь сам клиент Google и refresh-доступ в закрытом файле.
    project_root = Path(__file__).resolve().parents[1]
    dockerfile = (project_root / "Dockerfile").read_text(encoding="utf-8")
    start_script = (project_root / "start.sh").read_text(encoding="utf-8")
    if ("@googleworkspace/cli" in dockerfile
            and "GOOGLE_WORKSPACE_CREDENTIALS_B64" in start_script
            and "GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE" in start_script):
        print("✓ сервер устанавливает Google-клиент и поднимает refresh-доступ")
    else:
        failures.append("НЕ СОШЛОСЬ: серверный образ не сможет держать календарь")

    # Локальный режим должен переживать закрытие терминала и новый вход в macOS.
    # Проверяем полное описание процесса, не регистрируя его в launchd из теста.
    local_root = Path("/Users/check/workhub")
    local_agent = local_sync.agent_definition(
        local_root, python="/opt/homebrew/bin/python3",
        gws="/opt/homebrew/bin/gws", log_dir=Path("/Users/check/Library/Logs"),
        interval=90)
    local_default = local_sync.agent_definition(
        local_root, python="/opt/homebrew/bin/python3",
        gws="/opt/homebrew/bin/gws", log_dir=Path("/Users/check/Library/Logs"))
    local_args = local_agent["ProgramArguments"]
    local_env = local_agent["EnvironmentVariables"]
    if (local_agent.get("RunAtLoad") is True
            and local_agent.get("KeepAlive") is True
            and "source_sync.py" in " ".join(local_args)
            and "--watch" in local_args and "90" in local_args
            and "3600" in local_default["ProgramArguments"]
            and "/opt/homebrew/bin" in local_env.get("PATH", "")):
        print("✓ локальная автосверка стартует после входа и сама перезапускается")
    else:
        failures.append(f"НЕ СОШЛОСЬ: локальный автозапуск неполон — {local_agent}")

    source_launcher = inspect.getsource(source_sync.main)
    if "check_env.load_secrets" in source_launcher:
        print("✓ локальная автосверка читает постоянный ключ из закрытого файла")
    else:
        failures.append("НЕ СОШЛОСЬ: локальный процесс не увидит постоянный ключ")

    # Дежурный запускает оба обязательных процесса. Остановка любого видна
    # управляющему процессу, а не оставляет половину системы молча работающей.
    class FakeProcess:
        def __init__(self, code=None):
            self.code = code
            self.terminated = False

        def poll(self):
            return self.code

        def terminate(self):
            self.terminated = True
            self.code = 0

        def wait(self, timeout=None):
            return self.code

        def kill(self):
            self.code = -9

    duty_specs = duty.services(Path("/warehouse"), 45)
    duty_running = [
        duty.Running(duty_specs[0], FakeProcess()),
        duty.Running(duty_specs[1], FakeProcess(7)),
    ]
    launcher = (Path(__file__).resolve().parents[1] / "start.sh").read_text(
        encoding="utf-8")
    if (len(duty_specs) == 2
            and any("telegram_bot.py" in " ".join(one.command) for one in duty_specs)
            and any("source_sync.py" in " ".join(one.command)
                    and "--watch" in one.command for one in duty_specs)
            and duty.failed(duty_running) == ("Сверка источников", 7)
            and "tools/duty.py" in launcher):
        print("✓ дежурный держит Telegram и автосверку и замечает остановку")
    else:
        failures.append("НЕ СОШЛОСЬ: дежурный не держит оба процесса или молчит "
                        f"об остановке — {duty_specs}/{duty.failed(duty_running)}")

    # Дубли 8 августа: восемь дел получили по две задачи, потому что защита
    # смотрела только на поле в карточке, а оно пишется после создания.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"work/programs/p/program.md": holder,
                     "work/programs/p/commitments/a.md": commitment("A")})
        notes = store_mod.load(root, "work").notes
        rel = "work/programs/p/commitments/a.md"
        existing = {"tt-9": {"id": "tt-9", "status": 0,
                             "content": f"{ticktick.MARK}{rel}\nпочему: …"}}
        blind = ticktick.outgoing(notes, TT_TODAY)
        seeing = ticktick.outgoing(notes, TT_TODAY, tasks=existing)
        if len(blind) == 1 and not seeing:
            print("✓ задача, уже заведённая в приложении, второй раз не создаётся")
        else:
            failures.append("НЕ СОШЛОСЬ: защита от дублей смотрит только в склад — "
                            f"без сверки {len(blind)}, со сверкой {len(seeing)}")

    # Закрытие в складе обязано доходить до приложения — и не обязано зависеть
    # от сети: обязательство уже закрыто, отсутствие связи этого не отменяет.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        rel = "work/programs/p/commitments/a.md"
        build(root, {"raw/log.md": "",
                     "work/programs/p/program.md": holder,
                     rel: commitment("A", status="in-progress", external="tt-7"),
                     "raw/sources/2026-08-08-result.md":
                         "---\ntype: source\ndate: 2026-08-08\ntitle: р\n"
                         "source: разговор\nsource_ref: r\n---\n\n# р"})
        original = ticktick.project_id

        def dead(*_args, **_kwargs):
            raise ticktick.TickTickUnavailable("сети нет")

        ticktick.project_id = dead
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output):
                workflow.transition(root, "finish", rel, on=dt.date(2026, 8, 8),
                                    resolution="raw/sources/2026-08-08-result.md")
        finally:
            ticktick.project_id = original
        card = (root / rel).read_text(encoding="utf-8")
        said = output.getvalue()
        if ("status: resolved" in card
                and "задача tt-7 осталась открытой" in said
                and "сети нет" in said):
            print("✓ отказ приложения назван строкой и не мешает закрыть дело в складе")
        else:
            failures.append("НЕ СОШЛОСЬ: закрытие сорвалось или отказ сети исчез\n"
                            f"    карточка: {card[:120]}\n    вывод: {said}")

    # Календарь — контекст, не второй статус дела. Даже если вызывающий код
    # передал похожее поле, наружу закрывается только связанная задача TickTick.
    status_targets = workflow.external_status_targets({
        "ticktick": "tt-1", "calendar": "cal-1",
    })
    if (status_targets == {"ticktick": "tt-1"}
            and workflow.EXTERNAL_STATUS_CHANNELS == ("ticktick",)):
        print("✓ закрытие дела не пишет в календарь — он остаётся контекстом")
    else:
        failures.append(f"НЕ СОШЛОСЬ: календарь стал внешним статусом {status_targets}")

    # Повтор команды о завершении останавливается на состоянии склада до
    # внешнего вызова. Одна и та же задача не закрывается второй раз.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        rel = "work/programs/p/commitments/a.md"
        build(root, {"raw/log.md": "",
                     "work/programs/p/program.md": holder,
                     rel: commitment("A", status="in-progress", external="tt-8"),
                     "raw/sources/2026-08-08-result.md":
                         "---\ntype: source\ndate: 2026-08-08\ntitle: р\n"
                         "source: разговор\nsource_ref: repeat\n---\n\n# р"})
        original_project = ticktick.project_id
        original_call = ticktick.call
        external_calls: list[str] = []

        def project_ok(_root):
            return "p-1"

        def call_ok(path, **_kwargs):
            external_calls.append(path)
            return {}

        ticktick.project_id = project_ok
        ticktick.call = call_ok
        repeated_stopped = False
        try:
            workflow.transition(root, "finish", rel, on=dt.date(2026, 8, 8),
                                resolution="raw/sources/2026-08-08-result.md")
            try:
                workflow.transition(root, "finish", rel, on=dt.date(2026, 8, 8),
                                    resolution="raw/sources/2026-08-08-result.md")
            except workflow.WorkflowError:
                repeated_stopped = True
        finally:
            ticktick.project_id = original_project
            ticktick.call = original_call
        if (repeated_stopped and len(external_calls) == 1
                and external_calls[0].endswith("/tt-8/complete")):
            print("✓ повторное завершение останавливается до второго внешнего вызова")
        else:
            failures.append("НЕ СОШЛОСЬ: повтор дошёл наружу: "
                            f"остановлен={repeated_stopped}, вызовы={external_calls}")

    incoming_cases = {
        "отметка «выполнено» возвращается как выполненное": (
            {"tt-1": {"id": "tt-1", "status": 2}}, "выполнено", "результат"),
        "исчезнувшая задача поднимает вопрос, а не отменяет дело": (
            {}, "исчезло", "передумал"),
        "перенос срока читается как отложенное с днём возврата": (
            {"tt-1": {"id": "tt-1", "status": 0, "dueDate": "2026-08-20T09:00:00+0300"}},
            "перенесено", "UNTIL=2026-08-20"),
        "живая задача без изменений молчит": (
            {"tt-1": {"id": "tt-1", "status": 0, "dueDate": "2026-08-10T09:00:00+0300"}},
            "", ""),
        # Приложение хранит день без времени как полночь в UTC: «10 августа»
        # приходит девятым числом. Пока сравнивали срезом строки, сверка каждые
        # пять минут переносила срок сама на себя и писала это в журнал.
        "срок из приложения в UTC — тот же день, а не перенос": (
            {"tt-1": {"id": "tt-1", "status": 0, "isAllDay": True,
                      "startDate": "2026-08-09T21:00:00.000+0000",
                      "dueDate": "2026-08-09T21:00:00.000+0000"}},
            "", ""),
    }
    for title, (tasks, event, hint) in incoming_cases.items():
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build(root, {"work/programs/p/program.md": holder,
                         "work/programs/p/commitments/a.md":
                             commitment("A", due="2026-08-10", external="tt-1")})
            got = ticktick.incoming(store_mod.load(root, "work").notes, tasks, TT_TODAY)
            ok = (not got if not event
                  else bool(got) and got[0].event == event
                  and hint in got[0].proposal)
            if ok:
                print(f"✓ {title}")
            else:
                failures.append(f"НЕ СОШЛОСЬ: {title}\n    получено "
                                f"{[(one.event, one.proposal) for one in got]}")

    # Постоянная сверка идёт каждые пять минут. Пока перенос сравнивался срезом
    # строки, второй проход видел то же расхождение и писал его снова: за сутки
    # 13 августа в журнал ушло 360 одинаковых строк про два обязательства.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        rel = "work/programs/p/commitments/a.md"
        build(root, {"raw/log.md": "# журнал\n",
                     "work/programs/p/program.md": holder,
                     rel: commitment("A", due="2026-08-10", external="tt-1")})
        moved_task = {"tt-1": {"id": "tt-1", "status": 0, "isAllDay": True,
                               "startDate": "2026-08-19T21:00:00.000+0000",
                               "dueDate": "2026-08-19T21:00:00.000+0000"}}
        first_back = ticktick.incoming(
            store_mod.load(root, "work").notes, moved_task, TT_TODAY)
        ticktick.apply_incoming(root, first_back, moved_task, {}, TT_TODAY)
        second_back = ticktick.incoming(
            store_mod.load(root, "work").notes, moved_task, TT_TODAY)
        ticktick.apply_incoming(root, second_back, moved_task, {}, TT_TODAY)
        written = activity.read(root, events={"реакция"}, future_ok=True)
        carried = store_mod.load(root, "work").by_rel[rel].date_field("due")
        if (len(first_back) == 1 and not second_back and len(written) == 1
                and carried == dt.date(2026, 8, 20)):
            print("✓ перенос срока записывается один раз, второй проход молчит")
        else:
            failures.append("НЕ СОШЛОСЬ: сверка повторила перенос — "
                            f"первый={first_back}, второй={second_back}, "
                            f"журнал={written}, срок={carried}")

    # Второй пояс: даже если писатель ошибётся, одно и то же состояние про один
    # объект в журнал дважды подряд не попадёт, а изменение — попадёт.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"raw/log.md": "# журнал\n"})
        same = ["реакция", "отложено", "work/a.md", "срок изменён на 20.08.2026"]
        wrote = activity.append_once(root, same)
        repeated = activity.append_once(root, same)
        changed = activity.append_once(
            root, ["реакция", "отложено", "work/a.md", "срок изменён на 25.08.2026"])
        another = activity.append_once(
            root, ["реакция", "отложено", "work/b.md", "срок изменён на 20.08.2026"])
        rows = activity.read(root, events={"реакция"}, future_ok=True)
        if wrote and not repeated and changed and another and len(rows) == 3:
            print("✓ повтор состояния отбрасывается, смена состояния записывается")
        else:
            failures.append("НЕ СОШЛОСЬ: отбор повторов в журнале — "
                            f"{wrote}/{repeated}/{changed}/{another}, строк {len(rows)}")

    # Список проекта может не возвращать завершённые задачи. Однозначно
    # связанный id дочитывается отдельно, иначе галочка выглядела бы удалением.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        rel = "work/programs/p/commitments/a.md"
        build(root, {"work/programs/p/program.md": holder,
                     rel: commitment("A", external="tt-completed")})
        notes = store_mod.load(root, "work").notes
        fetched: list[str] = []
        original_call = ticktick.call

        def fetch_one(path, **_kwargs):
            fetched.append(path)
            return {"id": "tt-completed", "title": "Дело A", "status": 2,
                    "completedTime": "2026-08-12T10:00:00+0300"}

        ticktick.call = fetch_one
        tasks: dict[str, dict] = {}
        try:
            ticktick.fetch_linked("project-1", notes, tasks)
        finally:
            ticktick.call = original_call
        back = ticktick.incoming(notes, tasks, TT_TODAY)
        if (fetched == ["/project/project-1/task/tt-completed"]
                and back and back[0].event == "выполнено"):
            print("✓ завершённая задача дочитывается по связи и не считается удалённой")
        else:
            failures.append("НЕ СОШЛОСЬ: завершённая задача потерялась из списка — "
                            f"{fetched}/{tasks}/{back}")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"work/programs/p/program.md": holder,
                     "work/programs/p/commitments/a.md":
                         commitment("A", external="tt-1")})
        got = ticktick.incoming(store_mod.load(root, "work").notes, {}, TT_TODAY)
        meanings = ("сделал", "передумал", "не нужно", "удалил случайно")
        if got and all(meaning in got[0].proposal for meaning in meanings):
            print("✓ исчезновение задачи оставляет человеку все четыре возможных смысла")
        else:
            failures.append("НЕ СОШЛОСЬ: вопрос об исчезновении сузил неоднозначность — "
                            f"{got[0].proposal if got else 'нет вопроса'}")

    # Во внешнюю задачу уходят все четыре поля контракта; вызов подставной и
    # ничего не отправляет в настоящее приложение.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        rel = "work/programs/p/commitments/a.md"
        owned = commitment("A", due="2026-08-10").replace(
            "origin:", "owner: ivan\norigin:")
        build(root, {"work/programs/p/program.md": holder, rel: owned})
        note = next(note for note in store_mod.load(root, "work").notes
                    if note.rel == rel)
        sent: list[dict] = []
        original_call = ticktick.call

        def remember(_path, **kwargs):
            sent.append(kwargs.get("body") or {})
            return {"id": "tt-created"}

        ticktick.call = remember
        try:
            ticktick.push(root, "project-1", [ticktick.Outgoing(note, "новая")])
        finally:
            ticktick.call = original_call
        body = sent[0] if sent else {}
        content = str(body.get("content") or "")
        if (body.get("title") == "Дело A" and "2026-08-10" in str(body.get("dueDate"))
                and "Владелец: ivan" in content and f"{ticktick.MARK}{rel}" in content
                and "Почему:" in content):
            print("✓ в TickTick уходят формулировка, срок, владелец и источник")
        else:
            failures.append(f"НЕ СОШЛОСЬ: наружная задача потеряла поле контракта — {body}")

    # Недоступность приложения не превращается в «дел нет».
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"work/programs/p/program.md": holder})
        original = ticktick.CONFIG
        original_api = os.environ.pop("TICKTICK_API_TOKEN", None)
        original_token = os.environ.pop("TICKTICK_ACCESS_TOKEN", None)
        original_expires = os.environ.pop("TICKTICK_EXPIRES_AT", None)
        ticktick.CONFIG = Path(tmp) / "нет-такого.json"
        try:
            ticktick.token()
            failures.append("НЕ СОШЛОСЬ: отсутствие доступа проглочено")
        except ticktick.TickTickUnavailable as exc:
            print(f"✓ отсутствие доступа названо вслух: {str(exc)[:40]}…")
        finally:
            ticktick.CONFIG = original
            if original_api is not None:
                os.environ["TICKTICK_API_TOKEN"] = original_api
            if original_token is not None:
                os.environ["TICKTICK_ACCESS_TOKEN"] = original_token
            if original_expires is not None:
                os.environ["TICKTICK_EXPIRES_AT"] = original_expires

    # На сервере файла ноутбука нет: ключ приходит переменной окружения и не
    # записывается в репозиторий.
    with tempfile.TemporaryDirectory() as tmp:
        original = ticktick.CONFIG
        original_api = os.environ.pop("TICKTICK_API_TOKEN", None)
        original_token = os.environ.get("TICKTICK_ACCESS_TOKEN")
        original_expires = os.environ.get("TICKTICK_EXPIRES_AT")
        ticktick.CONFIG = Path(tmp) / "нет-такого.json"
        os.environ["TICKTICK_ACCESS_TOKEN"] = "server-token"
        os.environ.pop("TICKTICK_EXPIRES_AT", None)
        try:
            server_token = ticktick.token()
        finally:
            ticktick.CONFIG = original
            if original_api is not None:
                os.environ["TICKTICK_API_TOKEN"] = original_api
            if original_token is None:
                os.environ.pop("TICKTICK_ACCESS_TOKEN", None)
            else:
                os.environ["TICKTICK_ACCESS_TOKEN"] = original_token
            if original_expires is None:
                os.environ.pop("TICKTICK_EXPIRES_AT", None)
            else:
                os.environ["TICKTICK_EXPIRES_AT"] = original_expires
        if server_token == "server-token":
            print("✓ сервер читает ключ TickTick из окружения без файла")
        else:
            failures.append("НЕ СОШЛОСЬ: серверный ключ TickTick не прочитан")

    # Для постоянного дежурного предпочтителен официальный постоянный API token.
    # Если вместо него выданы OAuth refresh-данные, истёкший access token
    # обновляется внутри процесса без ручного перезапуска.
    auth_names = (
        "TICKTICK_API_TOKEN", "TICKTICK_ACCESS_TOKEN", "TICKTICK_EXPIRES_AT",
        "TICKTICK_REFRESH_TOKEN", "TICKTICK_CLIENT_ID", "TICKTICK_CLIENT_SECRET",
    )
    auth_before = {name: os.environ.get(name) for name in auth_names}
    original_config = ticktick.CONFIG
    original_urlopen = ticktick.urllib.request.urlopen
    original_session = ticktick._SESSION_TOKEN
    original_session_expires = ticktick._SESSION_EXPIRES
    oauth_calls: list[str] = []

    class OAuthResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({"access_token": "renewed", "expires_in": 3600}).encode()

    def oauth_urlopen(request, timeout=None):
        oauth_calls.append(request.full_url)
        return OAuthResponse()

    try:
        for name in auth_names:
            os.environ.pop(name, None)
        ticktick.CONFIG = Path("/нет-файла")
        os.environ["TICKTICK_API_TOKEN"] = "permanent"
        permanent = ticktick.token()
        os.environ.pop("TICKTICK_API_TOKEN")
        os.environ.update({
            "TICKTICK_ACCESS_TOKEN": "expired",
            "TICKTICK_EXPIRES_AT": "1",
            "TICKTICK_REFRESH_TOKEN": "refresh",
            "TICKTICK_CLIENT_ID": "client",
            "TICKTICK_CLIENT_SECRET": "secret",
        })
        ticktick._SESSION_TOKEN = ""
        ticktick._SESSION_EXPIRES = 0
        ticktick.urllib.request.urlopen = oauth_urlopen
        renewed = ticktick.token()
    finally:
        ticktick.CONFIG = original_config
        ticktick.urllib.request.urlopen = original_urlopen
        ticktick._SESSION_TOKEN = original_session
        ticktick._SESSION_EXPIRES = original_session_expires
        for name, value in auth_before.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    if (permanent == "permanent" and renewed == "renewed"
            and oauth_calls == [ticktick.OAUTH_TOKEN]):
        print("✓ постоянный ключ не истекает, OAuth-доступ обновляется автоматически")
    else:
        failures.append("НЕ СОШЛОСЬ: дежурный останется без доступа после истечения — "
                        f"{permanent}/{renewed}/{oauth_calls}")

    # Общий приём внешних событий: та же версия узнаётся по любому устойчивому
    # ключу, а новая версия дописывается рядом и не меняет первую.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"work/people/check.md": OK_NOTE})
        first = intake_mod.Capture(
            source="google-calendar", external_id="google-1", revision="rev-1",
            aliases=("uid@example",), date="2026-08-08", title="Календарь · тест",
            body="первая версия")
        alternate = intake_mod.Capture(
            source="google-calendar", external_id="uid@example", revision="rev-1",
            aliases=("google-1",), date="2026-08-08", title="Календарь · тест",
            body="тот же ответ под другим ключом")
        changed = intake_mod.Capture(
            source="google-calendar", external_id="google-1", revision="rev-2",
            aliases=("uid@example",), date="2026-08-09", title="Календарь · тест",
            body="вторая версия")
        one = intake_mod.save(root, first)
        before = one.read_text(encoding="utf-8") if one else ""
        repeat = intake_mod.save(root, alternate)
        two = intake_mod.save(root, changed)
        raw = store_mod.load(root, "raw")
        if (one and two and repeat is None and len(raw.notes) == 2
                and one.read_text(encoding="utf-8") == before):
            print("✓ внешний повтор узнаётся по другому ключу, изменение дописывается")
        else:
            failures.append("НЕ СОШЛОСЬ: общий приём создал дубль или переписал raw — "
                            f"повтор={repeat}, записей={len(raw.notes)}")

    # Календарное событие остаётся источником расписания, а не притворяется
    # состоявшейся встречей. Повторный опрос не создаёт второй файл.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"work/people/check.md": OK_NOTE})
        calendar_event = agenda.Event(
            start=dt.datetime(2026, 8, 9, 13, 0),
            end=dt.datetime(2026, 8, 9, 14, 0), title="Разбор",
            attendees=["one@example.com"], source_id="event-1",
            ical_uid="uid-1@example", updated="2026-08-09T09:00:00Z",
            location="Meet", description="Повестка")
        saved, skipped = agenda.capture(root, [calendar_event])
        saved_again, skipped_again = agenda.capture(root, [calendar_event])
        notes = store_mod.load(root, "raw").notes
        note = notes[0] if notes else None
        if (len(saved) == 1 and skipped == 0 and not saved_again
                and skipped_again == 1 and note and note.type == "source"
                and note.data.get("source") == "google-calendar"
                and note.data.get("scheduled_start") == "2026-08-09T13:00"):
            print("✓ календарь принимается в raw один раз и остаётся расписанием")
        else:
            failures.append("НЕ СОШЛОСЬ: календарный приём не повторобезопасен или "
                            f"сменил сорт — saved={saved}, notes={notes}")

    # У повторений одна серия и один iCalUID, но это разные события. Исходное
    # время повторения входит в альтернативный ключ и не склеивает их.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"work/people/check.md": OK_NOTE})
        recurring = [agenda.Event(
            start=dt.datetime(2026, 8, 14, hour, 0),
            end=dt.datetime(2026, 8, 14, hour + 1, 0), title="Повторение",
            source_id=f"instance-{hour}", ical_uid="series@example",
            updated="2026-08-09T09:00:00Z", recurring_id="series-google-id",
            attendees=["kto-to@example.com"],   # рабочая серия: участники есть
            original_start=dt.datetime(2026, 8, 14, hour, 0))
            for hour in (10, 11)]
        saved, skipped = agenda.capture(root, recurring)
        if len(saved) == 2 and skipped == 0:
            print("✓ два повторения одной календарной серии не склеиваются")
        else:
            failures.append("НЕ СОШЛОСЬ: повторения календаря сочтены одной записью — "
                            f"saved={len(saved)}, skipped={skipped}")

    # Задача с телефона — вход, а не молча принятое обязательство. Связанная
    # задача тоже оставляет версионный след, включая изменение заголовка.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"work/programs/p/program.md": holder,
                     "work/programs/p/commitments/a.md":
                         commitment("A", external="tt-linked")})
        phone = {"id": "tt-phone", "title": "Идея с телефона", "status": 0,
                 "modifiedTime": "2026-08-09T08:00:00Z"}
        linked = {"id": "tt-linked", "title": "Дело A", "status": 0,
                  "modifiedTime": "2026-08-09T08:00:00Z"}
        saved, skipped = ticktick.capture_external(
            root, {"tt-phone": phone, "tt-linked": linked}, [])
        again, skipped_again = ticktick.capture_external(
            root, {"tt-phone": phone, "tt-linked": linked}, [])
        changed_linked = dict(linked, title="Текст изменён в телефоне",
                              modifiedTime="2026-08-09T09:00:00Z")
        changed_saved, _ = ticktick.capture_external(
            root, {"tt-phone": phone, "tt-linked": changed_linked}, [])
        raw = store_mod.load(root, "raw").notes
        work = store_mod.load(root, "work").notes
        # Связанная задача, с которой ничего не произошло, входом не является —
        # это эхо собственного действия склада. Поэтому в приём идёт только
        # задача с телефона, а переименование связанной остаётся новостью.
        if (len(saved) == 1 and skipped == 0 and not again
                and skipped_again == 1 and len(changed_saved) == 1
                and len([note for note in work if note.type == "commitment"]) == 1
                and any(note.data.get("external_id") == "tt-phone" for note in raw)):
            print("✓ задача с телефона идёт во вход, изменения связанной не теряются")
        else:
            failures.append("НЕ СОШЛОСЬ: TickTick потерял внешний вход или создал "
                            f"обязательство — saved={len(saved)}/{len(changed_saved)}, "
                            f"repeat={len(again)}, work={len(work)}")

    # Если задача была создана, а поле связи не успело записаться, метка в теле
    # восстанавливает связь. Повторный запуск уже ничего не меняет.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        rel = "work/programs/p/commitments/a.md"
        build(root, {"work/programs/p/program.md": holder,
                     rel: commitment("A")})
        tasks = {"tt-repair": {"id": "tt-repair", "status": 0,
                                "content": f"{ticktick.MARK}{rel}"}}
        repaired = ticktick.repair_links(root, tasks)
        repaired_again = ticktick.repair_links(root, tasks)
        note = next(note for note in store_mod.load(root, "work").notes
                    if note.rel == rel)
        if repaired == [rel] and not repaired_again and note.data.get("ticktick") == "tt-repair":
            print("✓ потерянная связь с TickTick восстанавливается без второй задачи")
        else:
            failures.append("НЕ СОШЛОСЬ: связь TickTick не восстановлена однозначно — "
                            f"{repaired}/{repaired_again}/{note.data}")

    # Один источник не может заслонить отказ другого: оба опрашиваются, а
    # успешный всё равно сохраняется во вход.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"work/programs/p/program.md": holder})
        original_calendar = agenda.fetch
        original_blocks = agenda.fetch_timeblocks
        original_project = ticktick.project_id
        original_tasks = ticktick.fetch_tasks

        def calendar_down(*_args, **_kwargs):
            raise agenda.CalendarUnavailable("401 нет доступа")

        agenda.fetch = calendar_down
        agenda.fetch_timeblocks = lambda **_kwargs: []
        ticktick.project_id = lambda _root: "project-1"
        ticktick.fetch_tasks = lambda _project: {
            "tt-phone": {"id": "tt-phone", "title": "С телефона", "status": 0,
                         "modifiedTime": "2026-08-09T10:00:00Z"}}
        try:
            results = source_sync.reconcile(root, dt.date(2026, 8, 9), 14)
        finally:
            agenda.fetch = original_calendar
            agenda.fetch_timeblocks = original_blocks
            ticktick.project_id = original_project
            ticktick.fetch_tasks = original_tasks
        if (not results[0].ok and results[1].ok and results[1].saved == 1
                and len(store_mod.load(root, "raw").notes) == 1):
            print("✓ отказ календаря не мешает принять TickTick в общей сверке")
        else:
            failures.append(f"НЕ СОШЛОСЬ: общая сверка оборвалась на первом источнике — {results}")

    # Календарь даёт ситуативный контекст и записи источника. Он обязан честно
    # называть свою недоступность: пустой
    # календарь и нечитаемый выглядят для человека одинаково, а значат разное.
    def event(title: str, hour: int = 14) -> agenda.Event:
        return agenda.Event(start=dt.datetime(2026, 8, 8, hour, 0),
                            end=dt.datetime(2026, 8, 8, hour + 1, 0), title=title)

    alfa_client = ("---\ntype: client\nprefix: ALF\nmode: active\n"
                  "title: Альфа\n---\n\n# Альфа")
    alfa_files = {
        "work/clients/alfa/client.md": alfa_client,
        "work/clients/alfa/commitments/a.md":
            ("---\ntype: commitment\nkey: ALF-C-1\ntitle: Описать сделку\n"
             "direction: outbound\nlevel: now\nstatus: open\ndue: 2026-08-10\n"
             "origin: work/clients/alfa/client.md\n---\n\n# c"),
        "work/clients/alfa/questions/q.md":
            ("---\ntype: question\nkey: ALF-Q-1\ntitle: Кто владелец процесса\n"
             "status: open\nopened: 2026-08-01\n"
             "origin: work/clients/alfa/client.md\n---\n\n# q"),
    }

    # Ритм жизни человека в приём не кладётся. 9 августа еженедельная Джума,
    # идущая с июля 2025, положила две записи за один обмен — и клала бы по
    # одной каждую неделю, вечно.
    def calendar_event(*, recurring: str = "", people: list[str] | None = None,
                       container: str = "") -> agenda.Event:
        return agenda.Event(start=dt.datetime(2026, 8, 14, 12, 0),
                            end=dt.datetime(2026, 8, 14, 13, 0), title="Джума",
                            attendees=people or [], recurring_id=recurring,
                            container=container)

    recurring_cases = {
        "разовое событие — вход, даже без участников": (calendar_event(), True),
        "повтор без участников и связи — ритм жизни, не сырьё": (
            calendar_event(recurring="серия-1"), False),
        "повтор с участниками — рабочая встреча, берём": (
            calendar_event(recurring="серия-1", people=["a@b.c"]), True),
        "повтор, связанный с контейнером, — берём": (
            calendar_event(recurring="серия-1",
                           container="work/clients/alfa"), True),
    }
    for title, (sample, expected) in recurring_cases.items():
        if agenda.worth_capturing(sample) == expected:
            print(f"✓ {title}")
        else:
            failures.append(f"НЕ СОШЛОСЬ: {title}")

    match_cases = {
        "встреча узнаётся по названию контейнера":
            ("Встреча с Альфа", "work/clients/alfa"),
        "слово из названия достаточно": ("Альфа · разбор процессов",
                                         "work/clients/alfa"),
        "чужая встреча контейнеру не приписывается": ("Стоматолог", ""),
    }
    for title, (summary, expected) in match_cases.items():
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build(root, alfa_files)
            matched = agenda.match([event(summary)], store_mod.load(root, "work").notes)
            if matched[0].container == expected:
                print(f"✓ {title}")
            else:
                failures.append(f"НЕ СОШЛОСЬ: {title}\n    ожидался «{expected}», "
                                f"получен «{matched[0].container}»")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, alfa_files)
        notes = store_mod.load(root, "work").notes
        one = agenda.match([event("Встреча с Альфа")], notes)[0]
        ctx = agenda.context(one, notes, [])
        text = "\n".join(agenda.render([ctx], today=dt.date(2026, 8, 8)))
        checks = [
            ("контекст называет моё обещание", "я обещал: Описать сделку" in text),
            ("контекст называет открытый вопрос",
             "открыт вопрос: Кто владелец процесса" in text),
            ("время встречи в строке", "14:00" in text),
        ]
        for name, ok in checks:
            if ok:
                print(f"✓ {name}")
            else:
                failures.append(f"НЕ СОШЛОСЬ: {name}\n    {text}")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, alfa_files)
        notes = store_mod.load(root, "work").notes
        unknown = agenda.match([event("Стоматолог")], notes)[0]
        text = "\n".join(agenda.render([agenda.context(unknown, notes, [])],
                                       today=dt.date(2026, 8, 8)))
        if "складу эта встреча незнакома" in text:
            print("✓ незнакомая встреча честно объявляется без контекста")
        else:
            failures.append(f"НЕ СОШЛОСЬ: незнакомая встреча выдумала контекст — {text}")

    # Недоступность календаря не превращается в «встреч нет» и не вырезает
    # остальную часть входа.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, dict(alfa_files, **{"raw/log.md": ""}))
        original = agenda.fetch

        def broken(*_args, **_kwargs):
            raise agenda.CalendarUnavailable("401 нет доступа")

        agenda.fetch = broken
        try:
            screen = today_mod.build(root, dt.date(2026, 8, 8), "full")
            said = today_mod.render(screen)
        finally:
            agenda.fetch = original
        if ("Календарь не прочитан" in said and "401" in said
                and "Взять?" in said and "Что берём?" in said):
            print("✓ отказ календаря назван, остальной вход остаётся полным")
        else:
            failures.append(f"НЕ СОШЛОСЬ: отказ календаря проглочен — {said}")

    # Подставная команда действительно висит. Ограничение проверяется настоящим
    # subprocess.run, а не заглушкой, которая сразу выбрасывает TimeoutExpired.
    original_run = agenda.subprocess.run

    def hanging_command(_command, **kwargs):
        return original_run(
            [sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)

    agenda.subprocess.run = hanging_command
    started = time.monotonic()
    timeout_reason = ""
    try:
        agenda.fetch(dt.date(2026, 8, 8), timeout=0.05)
    except agenda.CalendarUnavailable as exc:
        timeout_reason = str(exc)
    finally:
        agenda.subprocess.run = original_run
    elapsed = time.monotonic() - started
    if (agenda.TIMEOUT == 3 and elapsed < 1
            and "не ответил за 0.05 сек" in timeout_reason):
        print("✓ зависший календарь прекращает ждать по короткому таймауту")
    else:
        failures.append(
            "НЕ СОШЛОСЬ: зависший календарь задержал вход "
            f"на {elapsed:.2f} сек при штатном таймауте {agenda.TIMEOUT}; "
            f"причина: {timeout_reason}")

    # Движок вмешательств работает после attention: правила находят повод без
    # модели, календарь даёт реальное окно, а бюджет решает, можно ли прервать.
    INTERVENTION_CONF = {
        "interventions": {
            "advance_hours": 24,
            "response_hours": 24,
            "working_start": "09:00",
            "working_end": "18:00",
            "default_event_minutes": 60,
            "default_task_minutes": 25,
            "event_prep_minutes": 20,
            "optional_budget": {"full": 3, "half": 2, "low": 1,
                                "unknown": 0},
        },
        "situational_policy": {},
    }
    INTERVENTION_NOW = dt.datetime(2026, 8, 9, 9, 0)
    intervention_situation = policy_mod.Situation(
        today=INTERVENTION_NOW.date(), capacity="full")
    model_seen: list[str] = []

    def prepare_intervention(candidate: interventions.Candidate) -> str:
        model_seen.append(candidate.key)
        return f"готовый черновик для {candidate.target}"

    empty_result = interventions.plan(
        [], [], [], intervention_situation, INTERVENTION_CONF,
        INTERVENTION_NOW, prepare=prepare_intervention)
    if empty_result.model_calls == 0 and not model_seen:
        print("✓ без найденного кандидата модель вмешательств не вызывается")
    else:
        failures.append("НЕ СОШЛОСЬ: модель вызвана без кандидата")

    future_event = agenda.Event(
        start=dt.datetime(2026, 8, 9, 11, 0),
        end=dt.datetime(2026, 8, 9, 12, 0), title="Альфа · решение",
        container="work/clients/alpha")
    no_tail = interventions.detect_events(
        [agenda.Context(future_event)], INTERVENTION_NOW, INTERVENTION_CONF)
    if not no_tail:
        print("✓ встреча без открытых хвостов вмешательства не порождает")
    else:
        failures.append(f"НЕ СОШЛОСЬ: пустая встреча породила {no_tail}")

    blocking_question = store_mod.Note(
        Path("q.md"), "work/clients/alpha/questions/q.md",
        {"type": "question", "title": "Какая финальная цена", "status": "open",
         "blocks": ["work/clients/alpha/decisions/price.md"]})
    blocked_context = agenda.Context(future_event, questions=[blocking_question])
    event_candidates = interventions.detect_events(
        [blocked_context], INTERVENTION_NOW, INTERVENTION_CONF)
    model_seen.clear()
    event_result = interventions.plan(
        event_candidates, [future_event], [], intervention_situation,
        INTERVENTION_CONF, INTERVENTION_NOW, prepare=prepare_intervention)
    event_item = event_result.interventions[0] if event_result.interventions else None
    if (len(event_candidates) == 1 and event_item is not None
            and event_item.significance == 100
            and "блокирует" in event_item.text
            and event_result.model_calls == 1
            and model_seen == [event_candidates[0].key]
            and event_item.draft.startswith("готовый черновик")):
        print("✓ блокирующий вопрос порождает значимое вмешательство и только затем черновик")
    else:
        failures.append("НЕ СОШЛОСЬ: блокирующий вопрос не дошёл до готового черновика: "
                        f"{event_candidates}, {event_result}")

    busy = [
        agenda.Event(dt.datetime(2026, 8, 9, 9, 0),
                     dt.datetime(2026, 8, 9, 10, 30), "первая встреча"),
        agenda.Event(dt.datetime(2026, 8, 9, 11, 0),
                     dt.datetime(2026, 8, 9, 12, 0), "вторая встреча"),
    ]
    actual_window = interventions.choose_window(
        interventions.free_windows(busy, INTERVENTION_NOW,
                                   INTERVENTION_CONF,
                                   until=dt.datetime(2026, 8, 9, 14, 0)),
        30, dt.datetime(2026, 8, 9, 14, 0))
    if (actual_window is not None
            and actual_window.start == dt.datetime(2026, 8, 9, 10, 30)
            and actual_window.end == dt.datetime(2026, 8, 9, 11, 0)
            and actual_window.minutes == 30):
        print("✓ предложение привязано к реальному свободному окну нужной длины")
    else:
        failures.append(f"НЕ СОШЛОСЬ: свободное окно выбрано как {actual_window}")

    important = interventions.Candidate(
        "important", "обещание", "work/important.md", "важное обещание",
        100, 6, proposal_class="closure", optional=False,
        required_minutes=20, deadline=dt.datetime(2026, 8, 9, 14, 0),
        needs_model=True)
    meeting_now = agenda.Event(
        dt.datetime(2026, 8, 9, 10, 0),
        dt.datetime(2026, 8, 9, 11, 0), "идущая встреча")
    during = interventions.plan(
        [important], [meeting_now], [], intervention_situation,
        INTERVENTION_CONF, dt.datetime(2026, 8, 9, 10, 15),
        prepare=prepare_intervention).interventions[0]
    if (during.channel != "Telegram"
            and during.window.start >= meeting_now.end
            and "не прерывает встречу" in during.outcome):
        print("✓ важное во время встречи готовится, но встречу не прерывает")
    else:
        failures.append(f"НЕ СОШЛОСЬ: важное прервало встречу: {during}")

    ignored_log = activity.parse_text(
        "- 2026-08-06T09:00:00 · представлен · обещание · work/ignored.md\n"
        "- 2026-08-07T09:00:00 · представлен · обещание · work/ignored.md\n",
        now=INTERVENTION_NOW)
    ignored_candidate = interventions.Candidate(
        "ignored", "обещание", "work/ignored.md", "повторный сигнал",
        90, 6, proposal_class="closure", optional=False)
    ignored_item = interventions.plan(
        [ignored_candidate], [], ignored_log, intervention_situation,
        INTERVENTION_CONF, INTERVENTION_NOW).interventions[0]
    if (ignored_item.ignored == 2 and ignored_item.level == 4
            and ignored_item.level < ignored_item.base_level
            and "давление снижено" in ignored_item.outcome):
        print("✓ два проигнорированных показа снижают уровень, а не давление повышают")
    else:
        failures.append(f"НЕ СОШЛОСЬ: два игнора дали {ignored_item}")

    low_capacity = policy_mod.Situation(
        today=INTERVENTION_NOW.date(), capacity="low")
    optional_candidates = [
        interventions.Candidate(
            f"optional-{index}", "анализ", f"work/{index}.md", f"сигнал {index}",
            90 - index, 4, optional=True)
        for index in range(2)
    ]
    budgeted = interventions.plan(
        optional_candidates, [], [], low_capacity, INTERVENTION_CONF,
        INTERVENTION_NOW).interventions
    if ([item.channel for item in budgeted].count("Telegram") == 1
            and [item.channel for item in budgeted].count("бриф") == 1
            and any("бюджет" in item.outcome for item in budgeted)):
        print("✓ низкая ёмкость оставляет одно необязательное прерывание, остальное — в бриф")
    else:
        failures.append(f"НЕ СОШЛОСЬ: бюджет низкого дня дал {budgeted}")

    if (event_item is not None
            and event_item.level in interventions.LEVEL_NAMES
            and event_item.window is not None and event_item.channel
            and event_item.outcome):
        print("✓ вмешательство всегда называет уровень, окно, канал и исход")
    else:
        failures.append(f"НЕ СОШЛОСЬ: неполное вмешательство {event_item}")

    if event_item is not None:
        closed = [interventions.resolve(event_item, choice).outcome
                  for choice in interventions.RESOLUTIONS]
        if all(outcome.startswith("петля закрыта:") for outcome in closed):
            print("✓ сделать, изменить и не делать одинаково закрывают петлю")
        else:
            failures.append(f"НЕ СОШЛОСЬ: ответы не закрыли петлю: {closed}")

    # Профиль стареет от поправок, а не от календаря: пять раз ошиблись о
    # человеке — пора переписать, во что система про него верит.
    PROFILE_CONF = dict(OUT_CONF, profile={"path": "work/me/profil.md",
                                           "refusals_before_review": 3})
    profile_card = ("---\ntype: digest\ncontainer: work/me\n"
                    "reviewed: 2026-07-01\ntitle: Профиль\n---\n\n# профиль")

    def corrections(times: int, after: str = "07") -> str:
        return "".join(
            f"- 2026-{after}-{i + 2:02d}T09:00:00 · реакция · поправлено · work/x.md · раз\n"
            for i in range(times))

    profile_cases = {
        "пять поправок с пересмотра — профиль устарел": (corrections(4), True),
        "две поправки профиль ещё не старят": (corrections(2), False),
        "поправки до последнего пересмотра не считаются": (
            corrections(4, after="06"), False),
    }
    for title, (log, expected) in profile_cases.items():
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build(root, {"work/me/profil.md": profile_card})
            got = attention.build_lines(store_mod.load(root, "work").notes,
                                        PROFILE_CONF, dt.date(2026, 7, 20), [], None,
                                        log_entries=activity.parse_text(log), now=NOW)
            has = any(line.kind == "профиль" for line in got)
            if has == expected:
                print(f"✓ {title}")
            else:
                failures.append(f"НЕ СОШЛОСЬ: {title}\n    ожидалось {expected}, "
                                f"получено {[one.text for one in got]}")

    # Закрытие петли: ценность не в том, что сигнал сработал, а в том, что
    # поднятое получило явное разрешение. «Больше не показывай» закрывает петлю
    # не хуже «беру» — и учит сильнее.
    SHOWN_A = "- 2026-07-01T08:30:00 · представлен · чтение · work/a.md\n"
    closure_cases = {
        "отказ закрывает петлю так же, как дело": (
            SHOWN_A + "- 2026-07-01T09:00:00 · реакция · отклонено · work/a.md\n",
            1, 1, 0),
        "взятое в работу закрывает петлю и считается делом": (
            SHOWN_A + "- 2026-07-01T09:00:00 · реакция · взято · work/a.md\n",
            1, 1, 1),
        "отложенное — тоже закрытие: видно, что делать дальше": (
            SHOWN_A + "- 2026-07-01T09:00:00 · реакция · отложено · work/a.md\n",
            1, 1, 0),
        "молчание петлю не закрывает": (SHOWN_A, 1, 0, 0),
    }
    for title, (log, shown, closed, acted) in closure_cases.items():
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build(root, {"raw/log.md": log})
            presentations, reactions = reflect.read_log(root)
            signal = reflect.analyse(presentations, reactions, 3).get("чтение")
            ok = bool(signal and signal.shown == shown and signal.closed == closed
                      and signal.acted == acted)
            if ok:
                print(f"✓ {title}")
            else:
                failures.append(f"НЕ СОШЛОСЬ: {title}\n    ожидалось поднято {shown}, "
                                f"закрыто {closed}, делом {acted}; получено {signal}")

    # Отказы по одному поводу сжимаются в правило — но пишет его человек.
    def refusal_log(times: int) -> str:
        out = ""
        for i in range(1, times + 1):
            out += (f"- 2026-07-{i:02d}T08:30:00 · представлен · чтение · work/{i}.md\n"
                    f"- 2026-07-{i:02d}T09:00:00 · реакция · отклонено · work/{i}.md "
                    "· не до книг сейчас\n")
        return out

    rule_cases = {
        "три отказа по одному поводу дают заготовку правила": (refusal_log(3), 1),
        "два отказа правилом ещё не считаются": (refusal_log(2), 0),
    }
    for title, (log, expected) in rule_cases.items():
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build(root, {"raw/log.md": log})
            presentations, reactions = reflect.read_log(root)
            found = reflect.refusals(presentations, reactions)
            if len(found) == expected:
                print(f"✓ {title}")
            else:
                failures.append(f"НЕ СОШЛОСЬ: {title}\n    ожидалось {expected} "
                                f"поводов, получено {len(found)}: {found}")

    # Правило называет причину словами человека, а не пересказом.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"raw/log.md": refusal_log(3)})
        presentations, reactions = reflect.read_log(root)
        text = " ".join(reflect.rule_proposals(reflect.refusals(presentations, reactions)))
        if "не до книг сейчас" in text and "чтение" in text:
            print("✓ заготовка правила цитирует причину и называет повод")
        else:
            failures.append(f"НЕ СОШЛОСЬ: заготовка правила без причины — {text}")

    # Отказы по разным поводам в одно правило не сливаются.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        mixed = ("- 2026-07-01T08:30:00 · представлен · чтение · work/a.md\n"
                 "- 2026-07-01T09:00:00 · реакция · отклонено · work/a.md · раз\n"
                 "- 2026-07-02T08:30:00 · представлен · обещание · work/b.md\n"
                 "- 2026-07-02T09:00:00 · реакция · отклонено · work/b.md · два\n"
                 "- 2026-07-03T08:30:00 · представлен · перегрев · work/c.md\n"
                 "- 2026-07-03T09:00:00 · реакция · отклонено · work/c.md · три\n")
        build(root, {"raw/log.md": mixed})
        presentations, reactions = reflect.read_log(root)
        if not reflect.refusals(presentations, reactions):
            print("✓ три отказа по трём разным поводам правила не образуют")
        else:
            failures.append("ЛОЖНАЯ ТРЕВОГА: разные поводы слиты в одно правило")

    # Отложенное с днём возврата: до срока молчит, в срок возвращается.
    # «Вернись в понедельник» — половина ответа человека, и раньше её негде было
    # хранить: defer писал причину, но не день.
    REVIEW_TODAY = dt.date(2026, 7, 10)

    def deferred(review: str | None, status: str = "open", due: str | None = None):
        head = ("---\ntype: commitment\nkey: P-C-1\ntitle: Дело\n"
                "direction: outbound\nlevel: now\n"
                f"status: {status}\n")
        if review:
            head += f"review: {review}\n"
        if due:
            head += f"due: {due}\n"
        head += "origin: work/programs/p/program.md\n---\n\n# c"
        return head

    review_cases = {
        "до дня возврата строки нет вовсе": (
            deferred("2026-07-20", due="2026-07-01"), []),
        "в назначенный день строка возвращается": (
            deferred("2026-07-10", due="2026-07-01"), ["вернуться"]),
        "день возврата прошёл — строка тем более есть": (
            deferred("2026-07-05", due="2026-07-01"), ["вернуться"]),
        "взятое в работу не напоминает о себе возвратом": (
            deferred("2026-07-05", status="in-progress", due="2026-07-01"),
            ["обещание"]),
        "без дня возврата всё работает как раньше": (
            deferred(None, due="2026-07-01"), ["обещание"]),
    }
    for title, (body, expected) in review_cases.items():
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build(root, {"work/programs/p/program.md": holder,
                         "work/programs/p/commitments/a.md": body})
            got = [line for line in
                   attention.build_lines(store_mod.load(root, "work").notes,
                                         OUT_CONF, REVIEW_TODAY, [], None,
                                         log_entries=[], now=NOW)
                   if line.target == "work/programs/p/commitments/a.md"]
            kinds = sorted({line.kind for line in got})
            if kinds == sorted(expected):
                print(f"✓ {title}")
            else:
                failures.append(f"НЕ СОШЛОСЬ: {title}\n    ожидалось {expected}, "
                                f"получено {kinds}: {[one.text for one in got]}")

    # День возврата пишется командой, а не руками, и попадает в журнал.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"raw/log.md": "",
                     "work/programs/p/program.md": holder,
                     "work/programs/p/commitments/a.md": deferred(None)})
        workflow.feedback(root, "defer", "work/programs/p/commitments/a.md",
                          reason="они сами думают", until=dt.date(2026, 7, 13))
        card = (root / "work/programs/p/commitments/a.md").read_text(encoding="utf-8")
        logged = (root / "raw/log.md").read_text(encoding="utf-8")
        checks = [("день возврата записан в карточку", "review: 2026-07-13" in card),
                  ("статус позиции не тронут", "status: open" in card),
                  ("журнал знает и причину, и день",
                   "отложено" in logged and "они сами думают" in logged
                   and "вернуться 13.07.2026" in logged)]
        for name, ok in checks:
            if ok:
                print(f"✓ {name}")
            else:
                failures.append(f"НЕ СОШЛОСЬ: {name}\n    карточка: {card[:200]}\n"
                                f"    журнал: {logged[:200]}")

    # День возврата — свойство отложенного, а не любой реакции.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"raw/log.md": "",
                     "work/programs/p/program.md": holder,
                     "work/programs/p/commitments/a.md": deferred(None)})
        try:
            workflow.feedback(root, "dismiss", "work/programs/p/commitments/a.md",
                              reason="не надо", until=dt.date(2026, 7, 13))
            failures.append("НЕ СОШЛОСЬ: день возврата принят у dismiss")
        except workflow.WorkflowError:
            print("✓ день возврата принимает только defer")

    # История советов: роль обязана помнить, что предлагала и чем это кончилось.
    ADVISED = "- 2026-07-01T10:00:00 · совет · coach · work/a.md · закрой один контур\n"
    advice_cases = {
        "совет без ответа не теряется — он виден как «ответа нет»": (
            ADVISED, "coach", 1, ""),
        "реакция по той же позиции подшивается к совету": (
            ADVISED + "- 2026-07-01T11:00:00 · реакция · отклонено · work/a.md\n",
            "coach", 1, "отклонено"),
        "реакция до совета к нему не относится": (
            "- 2026-06-30T11:00:00 · реакция · отклонено · work/a.md\n" + ADVISED,
            "coach", 1, ""),
        "реакция позже окна ответом не считается": (
            ADVISED + "- 2026-07-20T11:00:00 · реакция · взято · work/a.md\n",
            "coach", 1, ""),
        "чужой совет в историю роли не попадает": (
            ADVISED + "- 2026-07-01T10:05:00 · совет · mentor · work/a.md · читай\n",
            "mentor", 1, ""),
        "реакция по другой позиции чужой совет не оплачивает": (
            ADVISED + "- 2026-07-01T11:00:00 · реакция · взято · work/b.md\n",
            "coach", 1, ""),
    }
    for title, (log, role, count, answer) in advice_cases.items():
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build(root, {"raw/log.md": log})
            history = advice_mod.read(root, role)
            got_answer = history[0].answer if history else "(пусто)"
            if len(history) == count and got_answer == answer:
                print(f"✓ {title}")
            else:
                failures.append(f"НЕ СОШЛОСЬ: {title}\n    ожидалось {count} шт., "
                                f"ответ «{answer}»; получено {len(history)} шт., "
                                f"ответ «{got_answer}»")

    # Новый совет объясним: тип объединяет способ помощи, а контекст и основание
    # позволяют отличить устаревший совет от совета, который не сработал.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"raw/log.md": "", "work/a.md":
                     "---\ntype: commitment\nstatus: open\n---\n\n# a"})
        advice_mod.give(
            root, "coach", "work/a.md", "выдели час на контент",
            advice_type="content-time", context="три клиентских контура",
            basis="контент откладывался три недели",
            now=dt.datetime(2026, 7, 1, 10, 0),
        )
        recorded = advice_mod.read(root, "coach")
        if (len(recorded) == 1 and recorded[0].kind == "content-time"
                and recorded[0].context == "три клиентских контура"
                and recorded[0].basis == "контент откладывался три недели"
                and recorded[0].text == "выдели час на контент"):
            print("✓ совет хранит тип, контекст, основание и текст раздельно")
        else:
            failures.append(f"НЕ СОШЛОСЬ: поля совета потерялись — {recorded}")

    # Согласие — ещё не результат. Завершение должно произойти после принятия
    # по той же позиции; отказ остаётся отдельным исходом.
    outcome_cases = {
        "отказ отличается от принятия": (
            ADVISED + "- 2026-07-01T11:00:00 · реакция · отклонено · work/a.md\n",
            "отвергнут"),
        "согласие без завершения не считается сделанным": (
            ADVISED + "- 2026-07-01T11:00:00 · реакция · взято · work/a.md\n",
            "принят, но не сделан"),
        "согласие с последующим завершением считается сделанным": (
            ADVISED
            + "- 2026-07-01T11:00:00 · реакция · взято · work/a.md\n"
            + "- 2026-07-08T12:00:00 · реакция · завершено · work/a.md\n",
            "принят и сделан"),
    }
    for title, (log, expected) in outcome_cases.items():
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build(root, {"raw/log.md": log})
            history = advice_mod.read(root, "coach")
            got = history[0].outcome if history else "история пуста"
            if got == expected:
                print(f"✓ {title}")
            else:
                failures.append(f"НЕ СОШЛОСЬ: {title}: {got}, ожидалось {expected}")

    def advice_sequence(*, completed: bool) -> str:
        rows = []
        for number, day in enumerate((1, 3, 5), 1):
            target = f"work/{number}.md"
            rows.append(
                f"- 2026-07-{day:02d}T10:00:00 · совет · coach · {target} · "
                "content-time · клиентская неделя · контент не двигался · "
                f"выдели час {number}\n"
            )
            rows.append(
                f"- 2026-07-{day:02d}T11:00:00 · реакция · взято · {target}\n"
            )
            if completed:
                rows.append(
                    f"- 2026-07-{day + 1:02d}T12:00:00 · реакция · завершено · "
                    f"{target}\n"
                )
        return "".join(rows)

    # Три согласия без дела означают, что не сработал способ помощи. История
    # говорит это вслух, а четвёртый совет того же типа не записывается.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"raw/log.md": advice_sequence(completed=False),
                     "work/next.md": "---\ntype: commitment\nstatus: open\n---\n\n# next"})
        history = advice_mod.read(root, "coach")
        blocked = advice_mod.ineffective_kinds(history)
        rendered = advice_mod.render(history, "coach")
        try:
            advice_mod.give(
                root, "coach", "work/next.md", "ещё один час",
                advice_type="content-time", context="контент всё ещё стоит",
                basis="три прежних согласия", now=dt.datetime(2026, 7, 8, 10, 0),
            )
            refused = False
        except ValueError as exc:
            refused = "больше не предлагать" in str(exc)
        if (blocked == {("coach", "content-time"): (3, 0)}
                and "Бесполезные типы" in rendered and refused):
            print("✓ три согласия без результата блокируют бесполезный тип совета")
        else:
            failures.append("НЕ СОШЛОСЬ: три несделанных совета не остановили повтор: "
                            f"{blocked}, отказ={refused}\n{rendered}")

    # Если принятые советы доведены до результата, тот же тип не объявляется
    # бесполезным и остаётся доступен.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"raw/log.md": advice_sequence(completed=True),
                     "work/next.md": "---\ntype: commitment\nstatus: open\n---\n\n# next"})
        history = advice_mod.read(root, "coach")
        blocked = advice_mod.ineffective_kinds(history)
        try:
            advice_mod.give(
                root, "coach", "work/next.md", "ещё один час",
                advice_type="content-time", context="прошлые советы сработали",
                basis="три завершения", now=dt.datetime(2026, 7, 9, 10, 0),
            )
            allowed = True
        except ValueError:
            allowed = False
        if not blocked and allowed:
            print("✓ три совета с результатом не блокируют работающий тип")
        else:
            failures.append(f"НЕ СОШЛОСЬ: работающий тип заблокирован: {blocked}")

    # Тип принадлежит роли: два совета коуча и один ментора не превращаются в
    # три повтора одного советчика.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        mixed_roles = advice_sequence(completed=False).replace(
            "совет · coach · work/3.md", "совет · mentor · work/3.md"
        )
        build(root, {"raw/log.md": mixed_roles})
        blocked = advice_mod.ineffective_kinds(advice_mod.read(root))
        if not blocked:
            print("✓ одинаковые типы разных ролей не складываются в ложный запрет")
        else:
            failures.append(f"НЕ СОШЛОСЬ: роли смешаны в истории советов: {blocked}")

    # Отвергнутое роль обязана увидеть в своей истории, иначе повторит его.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"raw/log.md":
                     ADVISED + "- 2026-07-01T11:00:00 · реакция · отклонено · work/a.md\n"})
        text = advice_mod.render(advice_mod.read(root, "coach"), "coach")
        if "не повторять" in text and "закрой один контур" in text:
            print("✓ отвергнутый совет назван в истории поимённо")
        else:
            failures.append(f"НЕ СОШЛОСЬ: отвергнутый совет не выделен\n    {text}")

    # Совет адресуется позиции склада: без адресата его нельзя связать с ответом.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"raw/log.md": "", "work/a.md": "---\ntype: commitment\n---\n\n# a"})
        try:
            advice_mod.give(root, "coach", "work/net-takogo.md", "совет в пустоту")
            failures.append("НЕ СОШЛОСЬ: совет записан несуществующей позиции")
        except ValueError:
            print("✓ совет несуществующей позиции не записывается")
        try:
            advice_mod.give(root, "coach", "work/a.md", "   ")
            failures.append("НЕ СОШЛОСЬ: пустой совет записан")
        except ValueError:
            print("✓ пустой совет не записывается")
        try:
            advice_mod.give(root, "coach", "work/a.md", "совет без объяснения")
            failures.append("НЕ СОШЛОСЬ: совет без типа, контекста и основания записан")
        except ValueError as exc:
            if all(field in str(exc) for field in ("тип", "контекст", "основание")):
                print("✓ совет без типа, контекста и основания не записывается")
            else:
                failures.append(f"НЕ СОШЛОСЬ: причина неполного совета потеряна: {exc}")

    # Роли берутся из agents/, а не из списка в коде: иначе новая роль потребует
    # правки инструмента и её история молча потеряется.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"agents/coach.md": "---\nname: coach\n---\n\n# coach",
                     "agents/index.md": "---\ntype: index\n---\n\n# agents"})
        if advice_mod.roles(root) == ["coach"]:
            print("✓ список ролей читается из agents/, указатель в него не попадает")
        else:
            failures.append(f"НЕ СОШЛОСЬ: роли — {advice_mod.roles(root)}")

    # Битая заметка не исчезает молча: раньше обязательство существовало,
    # а утренний экран говорил «сигналов нет»
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {
            "work/programs/p/program.md": holder,
            "work/programs/p/commitments/ok.md": commit("P-C-1", "now"),
            "work/programs/p/commitments/broken.md": "---\ntype: commitment\nkey: [не закрыт\n---\n# c",
        })
        loaded = store_mod.load(root, "work")
        complaint = loaded.complain()
        if (len(loaded.notes) == 2 and len(loaded.unreadable) == 1
                and complaint and "broken.md" in complaint):
            print("✓ битая заметка попадает в «не прочитано», а не в тишину")
        else:
            failures.append(f"НЕ ПОЙМАН: битая заметка исчезла молча — "
                            f"{len(loaded.notes)} заметок, {loaded.unreadable}")

    # Дыры, которые нашла повторная проверка Codex
    extra: list[tuple[str, bool, str]] = []

    # закрывающий разделитель шапки — отдельной строкой
    extra.append(("«---oops» не считается закрытием шапки",
                  store_mod.parse("---\ntype: person\nname: x\n---oops\n")[0] is None,
                  "принял неверный разделитель"))

    # порог дней нельзя обойти будущими датами
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        future = "\n".join(f"- 2099-01-{d:02d}T08:30:00 · представлен · кандидат · work/a.md"
                            for d in range(1, 13))
        build(root, {"raw/log.md": future + "\n"})
        presentations, _ = reflect.read_log(root)
        extra.append(("будущие даты не считаются днями эксплуатации",
                      len(presentations) == 0,
                      f"засчитано {len(presentations)} представлений"))

    # ёмкость: пять измерений за один день — не личная база
    same_day = [(dt.date(2026, 7, 26), 50.0)] * 6
    got = capacity.assess(same_day, same_day, dt.date(2026, 7, 26))
    extra.append(("пять измерений за один день не образуют базу",
                  got.level == "unknown", f"дало {got.level}"))

    # все выгрузки битые — не молчаливый откат
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp) / "exp"
        folder.mkdir()
        (folder / "HealthAutoExport-2026-07-26.json").write_text("{сломано", encoding="utf-8")
        (folder / "HealthAutoExport-2026-07-25.json").write_text('[1,2,3]', encoding="utf-8")
        merged = capacity._from_export(folder)
        extra.append(("битые и неверной формы выгрузки попадают в список",
                      len(merged.get("__broken__", [])) == 2,
                      f"насчитано {len(merged.get('__broken__', []))}"))

    # трекер: отказ git не выдаётся за «ничего не нашлось»
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"work/programs/p/program.md": holder})
        try:
            tracker.recent_changes(root, 7)
            ok = False
        except tracker.GitUnavailable:
            ok = True
        extra.append(("отказ git в подборе сделанного — исключение, не пустота", ok,
                      "вернул пустой словарь"))

    # застрявшее в idle-контейнере не считается застрявшим
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {
            "work/programs/p/program.md": holder.replace("mode: active", "mode: idle"),
            "work/programs/p/specs/s.md":
                "---\ntype: spec\nkey: P-S-1\ntitle: С\nstatus: draft\ncreated: 2020-01-01\n"
                "source: work/programs/p/program.md\n---\n\n# s",
        })
        rep = lint.run(root)
        stale = [m for _, m in rep.notices if "в состоянии" in m]
        extra.append(("idle-контейнер не даёт «застряло»", not stale, f"дало {stale}"))

    # --- третий круг: аудит Codex + свой ---
    third: list[tuple[str, bool, str]] = []

    FLOW_CONF = (
        "limit: 7\ndue_soon_days: 3\n"
        "tracker:\n  wip_limit: 2\n  candidates: 3\n"
        "capacity:\n  full: 3\n  half: 2\n  low: 1\n"
        "stale_days: {}\n"
    )
    FLOW_OPEN = (
        "---\ntype: commitment\nkey: P-C-1\ntitle: Подготовить результат\n"
        "direction: outbound\nstatus: open\nowner: ivan\nlevel: now\n"
        "when_then:\n  cue: открыть проект\n  action: сделать первый шаг\n"
        "origin: work/programs/p/commitments/c.md\n---\n\n# c"
    )
    RESULT = "---\ntype: source\ndate: 2026-07-31\ntitle: Результат\n---\n\n# результат"

    # Явный переход пишет даты и реакцию; чтение само по себе ничего не меняет.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {
            "config/attention.yml": FLOW_CONF,
            "raw/log.md": "# журнал\n",
            "raw/sources/2026-07-31-result.md": RESULT,
            "work/programs/p/program.md": holder,
            "work/programs/p/commitments/c.md": FLOW_OPEN,
        })
        try:
            target = "work/programs/p/commitments/c.md"
            workflow.transition(root, "take", target, on=TODAY,
                                now=dt.datetime(2026, 7, 26, 9, 0))
            taken = store_mod.load(root, "work").by_rel[target]
            log_after_take = (root / "raw/log.md").read_text(encoding="utf-8")
            third.append(("взятие записывает in-progress, started и явную реакцию",
                          taken.status == "in-progress"
                          and taken.date_field("started") == TODAY
                          and "реакция · взято" in log_after_take,
                          f"шапка {taken.data}, журнал {log_after_take!r}"))

            workflow.transition(
                root, "finish", target, on=TODAY,
                resolution="raw/sources/2026-07-31-result.md",
                now=dt.datetime(2026, 7, 26, 10, 0),
            )
            finished = store_mod.load(root, "work").by_rel[target]
            third.append(("завершение записывает дату конца и ссылку на результат",
                          finished.status == "resolved"
                          and finished.date_field("resolved") == TODAY
                          and finished.data.get("resolution")
                          == "raw/sources/2026-07-31-result.md",
                          f"шапка {finished.data}"))
        except Exception as exc:
            third.extend([
                ("взятие записывает in-progress, started и явную реакцию",
                 False, f"{type(exc).__name__}: {exc}"),
                ("завершение записывает дату конца и ссылку на результат",
                 False, f"{type(exc).__name__}: {exc}"),
            ])

    # Заблокированное после старта остаётся WIP и не освобождает слот.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        conf_one = FLOW_CONF.replace("wip_limit: 2", "wip_limit: 1")
        first = FLOW_OPEN.replace("key: P-C-1", "key: P-C-0").replace(
            "title: Подготовить результат", "title: Уже начато").replace(
            "status: open", "status: in-progress\nstarted: 2026-07-20")
        second = FLOW_OPEN.replace("P-C-1", "P-C-2").replace(
            "commitments/c.md", "commitments/d.md")
        build(root, {
            "config/attention.yml": conf_one,
            "raw/log.md": "# журнал\n",
            "work/programs/p/program.md": holder,
            "work/programs/p/commitments/c.md": first,
            "work/programs/p/commitments/d.md": second,
        })
        try:
            workflow.transition(
                root, "wait", "work/programs/p/commitments/c.md", on=TODAY,
                reason="ждём ответ", now=dt.datetime(2026, 7, 26, 9, 0),
            )
            blocked = False
            try:
                workflow.transition(
                    root, "take", "work/programs/p/commitments/d.md", on=TODAY,
                    now=dt.datetime(2026, 7, 26, 10, 0),
                )
            except workflow.WorkflowError as exc:
                blocked = "лимит" in str(exc)
            untouched = store_mod.load(root, "work").by_rel[
                "work/programs/p/commitments/d.md"].status == "open"
            third.append(("ожидание после старта остаётся в лимите работы",
                          blocked and untouched,
                          f"blocked={blocked}, untouched={untouched}"))
        except Exception as exc:
            third.append(("ожидание после старта остаётся в лимите работы",
                          False, f"{type(exc).__name__}: {exc}"))

    # Отклонение совета — наблюдение, а не скрытая отмена обязательства.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {
            "raw/log.md": "# журнал\n",
            "work/programs/p/commitments/c.md": FLOW_OPEN,
        })
        try:
            workflow.feedback(
                root, "dismiss", "work/programs/p/commitments/c.md",
                reason="не относится к сегодняшнему фокусу",
                now=dt.datetime(2026, 7, 26, 9, 0),
            )
            note = store_mod.load(root, "work").by_rel[
                "work/programs/p/commitments/c.md"]
            logged = (root / "raw/log.md").read_text(encoding="utf-8")
            third.append(("отклонение записывает реакцию, но не меняет дело",
                          note.status == "open" and "реакция · отклонено" in logged,
                          f"status={note.status}, log={logged!r}"))
        except Exception as exc:
            third.append(("отклонение записывает реакцию, но не меняет дело",
                          False, f"{type(exc).__name__}: {exc}"))

    # Живой вход не читает wiki, держит порядок и только представляет варианты.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        started = FLOW_OPEN.replace("key: P-C-1", "key: P-C-2").replace(
            "title: Подготовить результат", "title: Продолжить начатое").replace(
            "status: open", "status: in-progress\nstarted: 2026-07-20").replace(
            "commitments/c.md", "commitments/started.md")
        waiting_note = FLOW_OPEN.replace("key: P-C-1", "key: P-C-3").replace(
            "title: Подготовить результат", "title: Получить ответ").replace(
            "direction: outbound", "direction: inbound").replace(
            "status: open", "status: waiting").replace(
            "commitments/c.md", "commitments/waiting.md")
        candidate = FLOW_OPEN.replace("commitments/c.md", "commitments/candidate.md")
        build(root, {
            "config/attention.yml": FLOW_CONF,
            "raw/log.md": "# журнал\n",
            "raw/inbox/2026-07-01-vhod.md": SOURCE,
            "work/programs/p/program.md": holder,
            "work/programs/p/commitments/started.md": started,
            "work/programs/p/commitments/waiting.md": waiting_note,
            "work/programs/p/commitments/candidate.md": candidate,
        })
        try:
            screen = today_mod.build(root, TODAY, "full")
            kinds = [line.kind for line in screen.lines]
            compact = len(screen.lines) + 1 <= 7
            ordered = (kinds and kinds[0] == "ждёт разбора"
                       and kinds.index("в работе") < kinds.index("ждёт")
                       < kinds.index("кандидат"))
            candidate_rel = "work/programs/p/commitments/candidate.md"
            still_open = store_mod.load(root, "work").by_rel[candidate_rel].status == "open"
            third.append(("today строится без wiki, держит порядок и семь строк",
                          compact and ordered and not (root / "wiki").exists(),
                          f"строки={kinds}, compact={compact}"))
            third.append(("показ кандидата не начинает его автоматически",
                          still_open, f"status={store_mod.load(root, 'work').by_rel[candidate_rel].status}"))
            today_mod.record(root, screen, now=dt.datetime(2026, 7, 26, 9, 0))
            logged = (root / "raw/log.md").read_text(encoding="utf-8")
            third.append(("today пишет «представлен», а не техническое «показано»",
                          " · представлен · " in logged
                          and " · показано · " not in logged,
                          logged))
        except Exception as exc:
            third.extend([
                ("today строится без wiki, держит порядок и семь строк",
                 False, f"{type(exc).__name__}: {exc}"),
                ("показ кандидата не начинает его автоматически",
                 False, f"{type(exc).__name__}: {exc}"),
                ("today пишет «представлен», а не техническое «показано»",
                 False, f"{type(exc).__name__}: {exc}"),
            ])

    # Пересборка диагностического вида фиксирует вычисление, но не показ.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {
            "config/attention.yml": FLOW_CONF,
            "raw/log.md": "# журнал\n",
            "work/programs/p/program.md": holder,
            "work/programs/p/commitments/c.md": FLOW_OPEN,
        })
        (root / "wiki").mkdir()
        code = attention.main(["--root", str(root), "--today", TODAY.isoformat()])
        logged = (root / "raw/log.md").read_text(encoding="utf-8")
        third.append(("attention фиксирует вычисление, но не ложный показ",
                      code == 0 and "экран собран" in logged
                      and " · показано · " not in logged
                      and " · представлен · " not in logged,
                      logged))

    # Поток считается по датам позиции, а не по истории правок.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        active = FLOW_OPEN.replace("status: open", "status: in-progress\nstarted: 2026-07-20")
        closed = FLOW_OPEN.replace("key: P-C-1", "key: P-C-4").replace(
            "status: open", "status: resolved\nstarted: 2026-07-01\nresolved: 2026-07-05").replace(
            "commitments/c.md", "commitments/closed.md")
        build(root, {
            "work/programs/p/commitments/c.md": active,
            "work/programs/p/commitments/closed.md": closed,
        })
        flow = reflect.flow_metrics(store_mod.load(root, "work").notes, TODAY, 30)
        third.append(("поток даёт WIP, завершения, возраст и длительность цикла",
                      flow.wip == 1 and flow.throughput == 1
                      and flow.ages == [6] and flow.cycles == [4],
                      f"{flow}"))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"work/programs/p/program.md": holder,
                     "work/programs/p/commitments/c.md":
                        "---\ntype: commitment\nkey: P-C-1\ntitle: Ц\ndirection: outbound\n"
                        "status: open\nowner: ivan\ndue: 2026-02-31\n"
                        "origin: work/programs/p/program.md\n---\n\n# c"})
        try:
            loaded = store_mod.load(root, "work")
            why = dict(loaded.unreadable).get("work/programs/p/commitments/c.md", "")
            third.append(("невозможная дата — причина, а не падение",
                          "не разбирается" in why, f"получено {why!r}"))
        except Exception as exc:
            third.append(("невозможная дата — причина, а не падение", False,
                          f"склад упал: {type(exc).__name__}"))
        try:
            lint.run(root)
            attention.build_lines(store_mod.load(root, "work").notes, CONFIG, TODAY)
            third.append(("инструменты переживают битую дату", True, ""))
        except Exception as exc:
            third.append(("инструменты переживают битую дату", False,
                          f"{type(exc).__name__}: {exc}"))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        files = {"work/programs/p/program.md": holder}
        for i in range(4):
            files[f"work/programs/p/specs/s{i}.md"] = (
                f"---\ntype: spec\nkey: P-S-{i}\ntitle: Спека {i}\nstatus: draft\n"
                f"created: 2024-01-01\nsource: work/programs/p/program.md\n---\n\n# s")
        build(root, files)
        notes = store_mod.load(root, "work").notes
        conf = dict(CONFIG, stuck_top=2)
        lines = attention.build_lines(notes, conf, TODAY)
        shown = [line for line in lines if not line.demoted]
        hidden = [line for line in lines if line.demoted]
        third.append(("срезанное лимитом не исчезает, а уходит в «ещё N»",
                      len(shown) == 2 and len(hidden) == 2,
                      f"показано {len(shown)}, скрыто {len(hidden)}"))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        files = {"work/programs/p/program.md": holder, "work/me/me.md": SELF_HOLDER}
        for i in range(3):
            files[f"work/programs/p/specs/s{i}.md"] = (
                f"---\ntype: spec\nkey: P-S-{i}\ntitle: Спека {i}\nstatus: draft\n"
                f"created: 2024-01-01\nsource: work/programs/p/program.md\n---\n\n# s")
        files["work/me/goals/g.md"] = ("---\ntype: goal\nkind: result\nhorizon: 2026-12-31\n"
                                       "status: active\ntitle: Цель\ncreated: 2026-01-01\n---\n\n# g")
        build(root, files)
        notes = store_mod.load(root, "work").notes
        lines = attention.build_lines(notes, dict(CONFIG, stuck_top=2, goal_top=3), TODAY)
        goal = [line for line in lines if line.kind == "цель без шагов" and not line.demoted]
        third.append(("цель не вытесняется чужими спеками: лимит на класс, а не общий",
                      len(goal) == 1, f"целей показано {len(goal)}"))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"work/people/a.md": OK_NOTE})
        report = lint.run(root)
        third.append(("линт называет покрытие, а не только «чисто»",
                      "обязательства" in report.coverage and report.coverage["обязательства"] == 0,
                      f"покрытие {report.coverage}"))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"work/people/a.md": OK_NOTE})
        report = lint.run(root)
        third.append(("отказ git — замечание, а не тишина",
                      any("git недоступен" in m for _, m in report.notices),
                      f"замечания {[m for _, m in report.notices]}"))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"work/people/a.md": OK_NOTE, "tools/README.md": "# инструменты"})
        report = lint.run(root)
        third.append(("README рядом с кодом не считается заметкой без сорта",
                      not any("вне трёх корней" in m for _, m in report.problems),
                      f"нарушения {[m for _, m in report.problems]}"))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"work/goals/g.md": "---\ntype: goal\nkind: result\nhorizon: 2026-Q3\n"
                                        "status: active\ntitle: Сирота\ncreated: 2026-01-01\n---\n\n# g"})
        report = lint.run(root)
        third.append(("цель вне контейнера «Я» — нарушение",
                      any("вне контейнера" in m for _, m in report.problems),
                      f"нарушения {[m for _, m in report.problems]}"))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        log = "".join(
            f"- 2026-07-0{d}T08:3{d}:00 · представлен · обещание · work/a{d}.md\n"
            for d in range(1, 7)
        )
        build(root, {"raw/log.md": log})
        presentations, reactions = reflect.read_log(root)
        signals = reflect.analyse(presentations, reactions, 3)
        useful = reflect.working_days(signals)
        props = reflect.proposals(signals, {"stale_days": {}}, useful)
        noisy = any("полезная реакция" in proposal for proposal in props)
        third.append(("самоанализ не предлагает пороги по данным, которые сам счёл малыми",
                      not noisy, f"дней {useful}, предложения {props}"))

    # --- приём: мягкая мерка по месту, а не по знакомству ---
    ZONE = "intake:\n  zone: raw/inbox\n"
    CAPTURE = ("---\ntype: meeting\ndate: 2026-07-01\nsource: meeting-copilot\n"
               'source_ref: "1-standup"\n---\n\n# m')

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"config/attention.yml": ZONE,
                     "raw/inbox/2026-07-01-c.md": CAPTURE})
        report = lint.run(root)
        third.append(("сырьё в зоне приёма не валит гейт из-за полей, которые ставит разбор",
                      not report.problems, f"нарушения {[m for _, m in report.problems]}"))
        third.append(("очередь на разбор названа числом, а не молчанием",
                      any("ждут приёма: 1" in m for _, m in report.notices),
                      f"замечания {[m for _, m in report.notices]}"))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        rel = "raw/inbox/2026-07-01-c.md"
        build(root, {"config/attention.yml": ZONE,
                     rel: CAPTURE,
                     "work/programs/p/program.md": holder + f"\n\nисточник: {rel}"})
        report = lint.run(root)
        raw_notes = store_mod.load(root, "raw").notes
        intake_conf = {"intake": {"zone": "raw/inbox", "pending_days": 3}}
        lines = attention.build_lines([], dict(CONFIG, **intake_conf),
                                      dt.date(2026, 7, 20), raw_notes,
                                      pending_rels=set())
        third.append(("разобранное сырьё не остаётся долгом линтера и экрана",
                      not any("ждут приёма" in m for _, m in report.notices)
                      and not any(line.kind == "ждёт разбора" for line in lines),
                      f"замечания={report.notices}, строки={[line.kind for line in lines]}"))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"config/attention.yml": ZONE,
                     "raw/meetings/2026-07-01-c.md": CAPTURE})
        report = lint.run(root)
        third.append(("та же запись вне зоны мягкой мерки не получает — знакомство не помогает",
                      any("нет обязательного поля container" in m
                          for _, m in report.problems),
                      f"нарушения {[m for _, m in report.problems]}"))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"config/attention.yml": ZONE,
                     "raw/inbox/2026-07-01-c.md":
                        "---\ntype: interview\ndate: 2026-07-01\n---\n\n# i"})
        report = lint.run(root)
        third.append(("зона приёма смягчает мерку любому писателю, не только знакомому",
                      not report.problems, f"нарушения {[m for _, m in report.problems]}"))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"raw/inbox/index.md": "---\ntype: index\ntitle: inbox\n"
                                           "generated: false\n---\n\n# inbox"})
        report = lint.run(root)
        third.append(("index.md в зоне приёма не считается сырьём",
                      report.coverage.get("сырьё в приёме", 0) == 0,
                      f"покрытие {report.coverage}"))

    pending = [store_mod.Note(path=Path("x"), rel="raw/inbox/2026-07-01-c.md",
                              data={"type": "meeting", "date": "2026-07-01"})]
    INTAKE = {"intake": {"zone": "raw/inbox", "pending_days": 3}}
    for day, expect in ((dt.date(2026, 7, 2), False), (dt.date(2026, 7, 20), True)):
        got = [line for line in attention.build_lines([], dict(CONFIG, **INTAKE), day, pending)
               if line.kind == "ждёт разбора"]
        third.append((f"очередь просится в экран только после порога ({day:%d.%m})",
                      bool(got) == expect, f"строк {len(got)}"))

    third.append(("двадцать три записи дают одну строку, а не двадцать три",
                  len([line for line in attention.build_lines(
                      [], dict(CONFIG, **INTAKE), dt.date(2026, 7, 20), pending * 23)
                      if line.kind == "ждёт разбора"]) == 1, "строк больше одной"))

    # --- повтор не событие ---
    REPEAT = ('---\ntype: meeting\ndate: 2026-07-01\ncontainer: work/x\n'
              'source: meeting-copilot\nsource_ref: "abc-123"\n---\n\n# m')
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"raw/meetings/2026-07-01-m.md": REPEAT,
                     "raw/meetings/2026-07-01-m-2.md": REPEAT})
        got = [m for _, m in lint.run(root).problems if "повтор события" in m]
        third.append(("тот же разговор во втором файле — повтор, а не новое событие",
                      len(got) == 1, f"нарушения {got}"))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"raw/meetings/2026-07-01-m.md": REPEAT,
                     "raw/meetings/2026-07-02-m.md":
                        REPEAT.replace('"abc-123"', '"abc-456"').replace("07-01", "07-02")})
        got = [m for _, m in lint.run(root).problems if "повтор события" in m]
        third.append(("разные разговоры одного писателя повтором не считаются",
                      not got, f"нарушения {got}"))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"raw/meetings/2026-07-01-a.md":
                        '---\ntype: meeting\ndate: 2026-07-01\ncontainer: work/x\n'
                        'source_ref: "s"\n---\n\n# a',
                     "raw/meetings/2026-07-02-b.md":
                        '---\ntype: meeting\ndate: 2026-07-02\ncontainer: work/x\n'
                        'source_ref: "s"\n---\n\n# b'})
        got = [m for _, m in lint.run(root).problems if "повтор события" in m]
        third.append(("без указания писателя совпадение source_ref повтором не считается",
                      not got, f"нарушения {got}"))

    # --- сборщик навигации ---
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"work/clients/x/client.md":
                        "---\ntype: client\nmode: active\nprefix: X\ntitle: Икс\n---\n\n# x",
                     "work/clients/x/commitments/c.md":
                        "---\ntype: commitment\nkey: X-C-1\ntitle: Обещание\n"
                        "direction: outbound\nstatus: open\nowner: ivan\n"
                        "origin: work/clients/x/client.md\n---\n\n# c"})
        (root / "wiki" / "insights").mkdir(parents=True)
        written, _ = index_mod.build(root, dt.datetime(2026, 7, 27, 1, 0))
        body = (root / "work/clients/x/commitments/index.md").read_text(encoding="utf-8")
        third.append(("индекс перечисляет заметки папки с их состоянием",
                      "Обещание" in body and "| commitment | open |" in body,
                      "в индексе нет заметки или состояния"))
        third.append(("индекс объявляет себя видом с датой сборки",
                      "generated: true" in body and "generated_at: 2026-07-27" in body,
                      "нет пометки о генерации"))
        up = (root / "work/clients/x/index.md").read_text(encoding="utf-8")
        third.append(("из индекса можно спуститься в подпапку",
                      "commitments/index.md" in up, "нет ссылки вглубь"))
        third.append(("папка без своих заметок называется развилкой, а не пустой",
                      "Развилка" in (root / "work/clients/index.md").read_text(encoding="utf-8"),
                      "«пусто» о папке с подпапками"))
        third.append(("пустая папка на диске тоже получает индекс",
                      (root / "wiki/insights/index.md").exists(),
                      "старый индекс продолжал бы обещать сборщика"))
        third.append(("индексы не заводятся в архиве",
                      not any("archive" in w for w in written), f"написано {written}"))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"raw/meetings/2026-07-01-m.md":
                        '---\ntype: meeting\ndate: 2026-07-01\ncontainer: work/x\n'
                        'source_ref: "s"\n---\n\n# m'})
        index_mod.build(root, dt.datetime(2026, 7, 27, 1, 0))
        first = (root / "raw/meetings/index.md").read_text(encoding="utf-8")
        index_mod.build(root, dt.datetime(2026, 7, 27, 1, 0))
        third.append(("пересборка на тех же данных даёт тот же файл",
                      first == (root / "raw/meetings/index.md").read_text(encoding="utf-8"),
                      "вид не воспроизводится — значит хранит состояние"))

    # --- похожие позиции ---
    def commit_pair(one: str, two: str, same_origin: bool = True) -> list:
        origin2 = "raw/sources/2026-01-01-s.md" if same_origin else "work/programs/p/program.md"
        files = {"raw/sources/2026-01-01-s.md": SOURCE,
                 "work/programs/p/program.md": holder,
                 "config/attention.yml": "duplicates:\n  threshold: 0.35\n"
                                         "  same_origin_threshold: 0.25\n"}
        for i, (title, origin) in enumerate([(one, "raw/sources/2026-01-01-s.md"),
                                             (two, origin2)]):
            files[f"work/programs/p/commitments/c{i}.md"] = (
                f"---\ntype: commitment\nkey: P-C-{i}\ntitle: {title}\n"
                f"direction: outbound\nstatus: open\nowner: ivan\n"
                f"origin: {origin}\n---\n\n# c")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build(root, files)
            return [m for _, m in lint.run(root).notices if "похоже на дубль" in m]

    hits = commit_pair("Отправить клиенту коммерческое предложение", "Отправить КП клиенту")
    third.append(("пересказ одного обещания опознаётся как возможный дубль",
                  len(hits) == 1, f"замечаний {len(hits)}"))
    third.append(("замечание называет обе формулировки и меру сходства",
                  bool(hits) and "против" in hits[0] and "%" in hits[0],
                  f"текст {hits[:1]}"))
    third.append(("судить предлагается человеку, а не линтеру",
                  bool(hits) and "Судить тебе" in hits[0], "нет отказа от вывода"))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"raw/sources/2026-01-01-s.md": SOURCE,
                     "work/programs/p/program.md": holder,
                     "work/programs/p/commitments/c0.md":
                        "---\ntype: commitment\nkey: P-C-0\ntitle: Отправить КП клиенту\n"
                        "direction: outbound\nstatus: open\nowner: ivan\n"
                        "origin: raw/sources/2026-01-01-s.md\n---\n\n# c",
                     "work/programs/p/commitments/c1.md":
                        "---\ntype: commitment\nkey: P-C-1\ntitle: Отправить КП клиенту\n"
                        "direction: outbound\nstatus: open\nowner: ivan\n"
                        "origin: raw/sources/2026-01-01-s.md\n---\n\n# c"})
        report = lint.run(root)
        third.append(("похожесть — замечание, а не нарушение: гейт не краснеет",
                      not report.problems, f"нарушения {[m for _, m in report.problems]}"))

    quiet = commit_pair("Провести первую рабочую сессию AS-IS",
                        "Назначить представителя заказчика")
    third.append(("разные дела из одного события ложной тревоги не дают",
                  not quiet, f"замечания {quiet}"))

    far = commit_pair("Отправить клиенту коммерческое предложение", "Отправить КП клиенту",
                      same_origin=False)
    third.append(("для разных источников порог выше, но пересказ всё равно виден",
                  len(far) == 1, f"замечаний {len(far)}"))

    for title, files, expect in (
        ("идея без гипотезы дольше порога — замечание",
         {"work/ideas/i.md": "---\ntype: idea\nkey: ID-1\ntitle: Замысел\n"
                             "created: 2025-01-01\n---\n\n# i"}, True),
        ("идея с гипотезой претензий не вызывает",
         {"work/ideas/i.md": "---\ntype: idea\nkey: ID-2\ntitle: Замысел\n"
                             "created: 2025-01-01\n---\n\n# i",
          "work/ideas/h.md": "---\ntype: hypothesis\nstatus: open\naction: Проверю\n"
                             "expected: Сработает\nrationale: Есть довод\n"
                             "idea: work/ideas/i.md\n---\n\n# h"}, False),
        ("свежая идея права на тишину не теряет",
         {"work/ideas/i.md": "---\ntype: idea\nkey: ID-3\ntitle: Замысел\n"
                             "created: 2026-07-25\n---\n\n# i"}, False),
    ):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build(root, dict(files, **{"config/attention.yml":
                                       "stale_days:\n  idea_without_hypothesis: 90\n"}))
            got = any("без единой гипотезы" in m for _, m in lint.run(root).notices)
            third.append((title, got == expect, f"замечание {'есть' if got else 'нет'}"))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"work/people/a.md": OK_NOTE})
        cover = lint.run(root).coverage
        missing = [k for k in ("сырьё в приёме", "файлы вне трёх корней", "идеи")
                   if k not in cover]
        third.append(("счётчик покрытия не исчезает при нуле объектов",
                      not missing, f"пропали ключи {missing}, есть {sorted(cover)}"))

    # --- реальная работа, а не фикстуры: удалённое событие и битая дата ---
    def git(root: Path, *args: str) -> None:
        subprocess.run(["git", "-C", str(root), *args],
                       capture_output=True, check=False,
                       env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
                            "HOME": str(root)})

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"raw/meetings/2026-07-01-vstrecha.md":
                        '---\ntype: meeting\ndate: 2026-07-01\ncontainer: work/programs/p\n'
                        'source_ref: "s"\n---\n\n# встреча p',
                     "raw/meetings/2026-02-31-bitaya.md":
                        '---\ntype: meeting\ndate: 2026-01-01\ncontainer: work/programs/p\n'
                        'source_ref: "b"\n---\n\n# битая дата в имени',
                     "work/programs/p/program.md": holder,
                     "work/programs/p/commitments/c.md":
                        "---\ntype: commitment\nkey: P-C-1\ntitle: Обещание\n"
                        "direction: outbound\nstatus: open\nowner: ivan\nlevel: now\n"
                        "origin: raw/meetings/2026-07-01-vstrecha.md\n---\n\n# c"})
        git(root, "init", "-q")
        git(root, "add", "-A")
        git(root, "commit", "-qm", "первый")
        git(root, "rm", "-q", "raw/meetings/2026-07-01-vstrecha.md")
        git(root, "commit", "-qm", "уборка")
        notes = store_mod.load(root, "work").notes
        try:
            out = tracker.pickup(root, notes, TCONF, 30)
            third.append(("подбор сделанного переживает удалённое из raw событие",
                          True, ""))
            third.append(("исчезнувшее с диска событие названо, а не проглочено",
                          any("больше нет на диске" in line for line in out),
                          f"вывод {out}"))
        except Exception as exc:
            third.append(("подбор сделанного переживает удалённое из raw событие",
                          False, f"{type(exc).__name__}: {exc}"))
            third.append(("исчезнувшее с диска событие названо, а не проглочено", False, "упал"))
        third.append(("битая дата в имени события не роняет разбор",
                      tracker.event_date(root, "raw/meetings/2026-02-31-bitaya.md") is None,
                      "fromisoformat не защищён"))

        report = lint.run(root)
        kinds = [m for _, m in report.notices if "после создания" in m or "коммит" in m]
        third.append(("удаление события названо удалением, а не правкой",
                      any("удалено" in m for m in kinds), f"замечания {kinds}"))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        files = {f"raw/meetings/2026-07-0{i}-m{i}.md":
                    f'---\ntype: meeting\ndate: 2026-07-0{i}\ncontainer: work/x\n'
                    f'source_ref: "s{i}"\n---\n\n# m{i}' for i in range(1, 6)}
        build(root, files)
        git(root, "init", "-q")
        git(root, "add", "-A")
        git(root, "commit", "-qm", "первый")
        for name in files:
            git(root, "rm", "-q", name)
        git(root, "commit", "-qm", "уборка")
        report = lint.run(root)
        bulk = [m for _, m in report.notices if "удалено событий" in m]
        third.append(("разовая уборка даёт одну сводку, а не строку на файл",
                      len(bulk) == 1 and "5" in bulk[0], f"замечания {bulk}"))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"config/attention.yml": "intake:\n  zone: raw/inbox\n",
                     "work/people/a.md": OK_NOTE,
                     "raw/inbox/2026-07-01-c.md":
                        "---\ntype: meeting\ndate: 2026-07-01\nsource: meeting-copilot\n"
                        'source_ref: "1"\n---\n\n# m'})
        git(root, "init", "-q")
        report = lint.run(root)
        third.append(("свежее сырьё вне git — замечание, а не красный гейт",
                      not any("но не в git" in m and "inbox" in w
                              for w, m in report.problems),
                      f"нарушения {[(w, m) for w, m in report.problems]}"))
        third.append(("но оно названо, а не пропущено молча",
                      any("ещё не в git" in m for _, m in report.notices),
                      f"замечания {[m for _, m in report.notices]}"))
        third.append(("своя заметка вне git остаётся нарушением",
                      any("но не в git" in m and "people" in w
                          for w, m in report.problems),
                      f"нарушения {[(w, m) for w, m in report.problems]}"))

    # --- разбор не может быть доказательством самому себе ---
    for title, evidence, expect in (
        ("доказательством может быть встреча", "raw/meetings/2026-01-01-m.md", False),
        ("доказательством может быть интервью", "raw/interviews/2026-01-01-m.md", False),
        ("доказательством может быть документ", "raw/sources/2026-01-01-m.md", False),
        ("наш разбор доказательством быть не может, даже лёжа в raw/",
         "raw/digests/2026-01-01-m.md", True),
        ("позиция доказательством быть не может", "work/x/d.md", True),
    ):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build(root, {evidence: '---\ntype: source\ndate: 2026-01-01\ntitle: "И"\n---\n\n# и',
                         "work/clients/x/decisions/d.md":
                            "---\ntype: decision\nkey: X-D-9\nstatus: accepted\n"
                            f"date: 2026-01-01\nevidence: [{evidence}]\n---\n\n# d"})
            got = any("доказательством может быть только событие" in m
                      for _, m in lint.run(root).problems)
            third.append((title, got == expect, f"нарушение {'есть' if got else 'нет'}"))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"raw/meetings/2026-01-01-m.md":
                        '---\ntype: meeting\ndate: 2026-01-01\ncontainer: work/clients/x\n'
                        'source_ref: "s"\n---\n\n# m',
                     "work/clients/x/digests/r.md":
                        "---\ntype: digest\nsource: raw/meetings/2026-01-01-m.md\n"
                        "date: 2026-01-01\n---\n\n# разбор"})
        got = [m for _, m in lint.run(root).problems if "container" in m]
        third.append(("разбор обязан назвать свой контейнер", bool(got), f"нарушения {got}"))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"work/programs/p/program.md": holder,
                     "work/programs/p/commitments/c.md":
                        "---\ntype: commitment\nkey: P-C-8\ntitle: Ответ\n"
                        "direction: inbound\nstatus: waiting\nowner: ivan\n"
                        "origin: work/programs/p/program.md\nopened: 2026-07-25\n---\n\n# c"})
        notes = store_mod.load(root, "work").notes
        lines = [line for line in attention.build_lines(notes, CONFIG, TODAY)
                 if line.target.endswith("commitments/c.md")]
        third.append(("ожидающее ответа занимает одну строку экрана, а не две",
                      len(lines) == 1 and lines[0].kind == "ждёт",
                      f"строк {len(lines)}: {[line.kind for line in lines]}"))

    # --- процесс: связь хранится у зависимого, список вычисляется ---
    PROC = ("---\ntype: process\nkey: X-P-1\ntitle: Оплата\nstatus: mapping\n"
            "container: work/clients/x\n---\n\n# процесс")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"work/clients/x/processes/p.md": PROC,
                     "work/clients/x/questions/q.md":
                        "---\ntype: question\nkey: X-Q-7\ntitle: Вопрос\nstatus: open\n"
                        "origin: work/clients/x/processes/p.md\n"
                        "process: work/clients/x/processes/p.md\n---\n\n# q",
                     "work/clients/x/risks/r.md":
                        "---\ntype: risk\nkey: X-R-7\ntitle: Риск\nstatus: open\n"
                        "origin: work/clients/x/processes/p.md\n"
                        "process: work/clients/x/processes/p.md\n---\n\n# r"})
        report = lint.run(root)
        third.append(("процесс проходит проверку склада",
                      not report.problems, f"нарушения {[m for _, m in report.problems]}"))
        index_mod.build(root, dt.datetime(2026, 7, 28, 12, 0))
        body = (root / "work/clients/x/processes/index.md").read_text(encoding="utf-8")
        third.append(("связанные записи собираются в оглавлении сами",
                      "Вопрос" in body and "Риск" in body,
                      "в оглавлении нет привязанных записей"))
        third.append(("список связей не хранится в самом процессе",
                      "Вопрос" not in (root / "work/clients/x/processes/p.md").read_text(encoding="utf-8"),
                      "процесс ведёт список руками — он разойдётся"))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"work/clients/x/processes/p.md": PROC,
                     "work/clients/x/questions/q.md":
                        "---\ntype: question\nkey: X-Q-8\ntitle: Вопрос\nstatus: open\n"
                        "origin: work/clients/x/processes/p.md\n"
                        "process: work/clients/x/processes/nope.md\n---\n\n# q"})
        got = [m for _, m in lint.run(root).problems if "process" in m]
        third.append(("ссылка на несуществующий процесс — нарушение",
                      bool(got), f"нарушения {got}"))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"work/clients/x/processes/p.md":
                        PROC.replace("status: mapping", "status: летающий")})
        got = [m for _, m in lint.run(root).problems if "вне словаря" in m]
        third.append(("состояние процесса вне словаря — нарушение",
                      bool(got), f"нарушения {got}"))

    # --- карта развития: уровень поднимает работа, а не чтение ---
    SKILL_CARD = ("---\ntype: skill\nkey: ASR-K-{sid}\nskill_id: {sid}\n"
                  "title: {sid}\ndomains: [d]\nnode: atomic\nroles: [core]\n"
                  "target: 4\n{req}status: named\n---\n\n# {sid}")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {
            "work/me/me.md": SELF_HOLDER,
            "raw/sources/2026-01-01-s.md": SOURCE,
            "work/programs/asr/skills/a.md": SKILL_CARD.format(sid="S01", req=""),
            "work/programs/asr/skills/b.md": SKILL_CARD.format(
                sid="S02", req="requires: {S01: 3}\n"),
            "work/programs/asr/evidence/e.md":
                "---\ntype: evidence\nskill: work/programs/asr/skills/a.md\nlevel: 3\n"
                'result: "сделал"\ndate: 2026-08-05\norigin: raw/sources/2026-01-01-s.md\n'
                "---\n\n# e",
        })
        nodes = skill_map.build(store_mod.load(root, "work").notes, 3)
        third.append(("доказанный навык открывает следующий",
                      nodes["S02"]["state"] == "открыт", f"состояние {nodes['S02']['state']}"))
        third.append(("сам навык с доказательством помечен доказанным",
                      nodes["S01"]["state"] == "доказан" and nodes["S01"]["proved"] == 3,
                      f"{nodes['S01']['state']}, уровень {nodes['S01']['proved']}"))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {
            "work/me/me.md": SELF_HOLDER,
            "raw/sources/2026-01-01-s.md": SOURCE,
            "work/programs/asr/skills/a.md": SKILL_CARD.format(sid="S01", req=""),
            "work/programs/asr/skills/b.md": SKILL_CARD.format(
                sid="S02", req="requires: {S01: 4}\n"),
            "work/programs/asr/evidence/e.md":
                "---\ntype: evidence\nskill: work/programs/asr/skills/a.md\nlevel: 3\n"
                'result: "сделал"\ndate: 2026-08-05\norigin: raw/sources/2026-01-01-s.md\n'
                "---\n\n# e",
        })
        nodes = skill_map.build(store_mod.load(root, "work").notes, 3)
        third.append(("предпосылка на уровень выше доказанного не открывает",
                      nodes["S02"]["state"] == "закрыт", f"состояние {nodes['S02']['state']}"))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {
            "work/me/me.md": SELF_HOLDER,
            "raw/sources/2026-01-01-s.md": SOURCE,
            "work/programs/asr/skills/a.md": SKILL_CARD.format(sid="S01", req=""),
            "work/me/reading/k.md": reading(skills="{S01: 4}", status="read",
                                            started="2026-01-02", finished="2026-01-20",
                                            body="забрал"),
        })
        nodes = skill_map.build(store_mod.load(root, "work").notes, 3)
        third.append(("прочитанная книга поднимает изученное, но не владение",
                      nodes["S01"]["known"] == 4 and nodes["S01"]["proved"] == 0,
                      f"изучено {nodes['S01']['known']}, доказано {nodes['S01']['proved']}"))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"work/me/me.md": SELF_HOLDER})
        code = skill_map.main(["--root", str(root), "--dry-run"])
        third.append(("карта развития без навыков — провал, а не пустая картинка",
                      code == 1, f"код возврата {code}"))

    # --- карта чтения: разрыв обязан быть виден ---
    # Карта, распавшаяся на куски, — это книги, до которых нет ни одного пути.
    # Она молча выглядит как обычная карта, поэтому связность проверяется, а не
    # предполагается: первая же сборка распалась на четыре куска.
    MAP_BOOK = ("---\ntype: reading\nkey: ME-L-{n}\ntitle: Книга {n}\nkind: book\n"
                "topics: [{topic}]\ntier: {tier}\nstatus: queued\n"
                "source: work/me/me.md\n---\n\n# книга {n}")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        files = {"work/me/me.md": SELF_HOLDER}
        for i in range(1, 5):
            files[f"work/me/reading/a{i}.md"] = MAP_BOOK.format(
                n=i, topic="системное-мышление", tier="база" if i < 3 else "ядро")
        for i in range(5, 9):
            files[f"work/me/reading/b{i}.md"] = MAP_BOOK.format(
                n=i, topic="финансы-предприятия", tier="база" if i < 7 else "ядро")
        build(root, files)
        graph = reading_map.build(store_mod.load(root, "work").notes)
        parts = reading_map.components(graph["nodes"], graph["edges"])
        third.append(("карта из двух дисциплин остаётся связной",
                      len(parts) == 1, f"кусков {len(parts)}"))
        third.append(("на карту попали все книги",
                      len(graph["nodes"]) == 8, f"узлов {len(graph['nodes'])}"))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"work/me/me.md": SELF_HOLDER})
        code = reading_map.main(["--root", str(root), "--dry-run"])
        third.append(("карта без единой книги — провал, а не пустая картинка",
                      code == 1, f"код возврата {code}"))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        files = {"work/me/me.md": SELF_HOLDER}
        for i in range(1, 4):
            files[f"work/me/reading/c{i}.md"] = MAP_BOOK.format(
                n=i, topic="системное-мышление", tier="база")
        build(root, files)
        graph = reading_map.build(store_mod.load(root, "work").notes)
        graph["edges"] = []          # рёбра отняты: карта обязана назваться рваной
        parts = reading_map.components(graph["nodes"], graph["edges"])
        third.append(("карта без связей названа рваной, а не целой",
                      len(parts) == 3, f"кусков {len(parts)}"))

    # --- чтение и владение не подменяют друг друга ---
    # Главный принцип слоя развития: книга поднимает изученную глубину, работа —
    # доказанное владение. Стоит их смешать — и прогресс снова начнёт считаться
    # прочитанными книгами, ровно от чего этот слой уходит.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {
            "work/me/me.md": SELF_HOLDER,
            "raw/sources/2026-01-01-s.md": SOURCE,
            "work/programs/asr/skills/a.md": SKILL,
            "work/me/reading/k.md": reading(skills="{S01: 4}"),
        })
        cov = {s["id"]: s for s in reading_map.skill_coverage(
            store_mod.load(root, "work").notes)}
        third.append(("непрочитанная книга изученной глубины не даёт",
                      cov["S01"]["known"] == 0 and cov["S01"]["reachable"] == 4,
                      f"изучено {cov['S01']['known']}, доступно {cov['S01']['reachable']}"))
        third.append(("владение без доказательств остаётся нулевым",
                      cov["S01"]["proven"] == 0, f"доказано {cov['S01']['proven']}"))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {
            "work/me/me.md": SELF_HOLDER,
            "raw/sources/2026-01-01-s.md": SOURCE,
            "work/programs/asr/skills/a.md": SKILL,
            "work/me/reading/k.md": reading(skills="{S01: 4}", status="read",
                                            started="2026-01-02", finished="2026-01-20",
                                            body="Забрал: границы системы."),
            "work/programs/asr/evidence/e.md":
                "---\ntype: evidence\nskill: work/programs/asr/skills/a.md\nlevel: 3\n"
                "date: 2026-02-01\norigin: raw/sources/2026-01-01-s.md\n"
                "context: real-world\n---\n\n# e",
        })
        cov = {s["id"]: s for s in reading_map.skill_coverage(
            store_mod.load(root, "work").notes)}
        third.append(("прочитанная книга поднимает изученную глубину",
                      cov["S01"]["known"] == 4, f"изучено {cov['S01']['known']}"))
        third.append(("владение поднимает работа, а не чтение",
                      cov["S01"]["proven"] == 3 and cov["S01"]["proofs"] == 1,
                      f"доказано {cov['S01']['proven']} по {cov['S01']['proofs']} записям"))

    # --- дисциплина: ключ у навыка обязан указывать на настоящую карточку ---
    DOMAIN_CARD = ("---\ntype: domain\nkey: ASR-D-{did}\ndomain_id: {did}\n"
                   "title: \"{did}\"\norder: 1\nstatus: specified\n---\n\n# {did}\n\n"
                   "## Что это и зачем она тебе\n\nтекст\n")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {
            "work/me/me.md": SELF_HOLDER,
            "work/programs/asr/skills/a.md": SKILL_CARD.format(sid="S01", req="")
                .replace("domains: [d]", "domains: [strategy]"),
        })
        got = [m for _, m in lint.run(root).problems if "не описана" in m]
        third.append(("навык ссылается на дисциплину, которой нет ни одной карточки",
                      not got, f"нарушения {got}"))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {
            "work/me/me.md": SELF_HOLDER,
            "work/programs/asr/domains/d.md": DOMAIN_CARD.format(did="strategy"),
            "work/programs/asr/skills/a.md": SKILL_CARD.format(sid="S01", req="")
                .replace("domains: [d]", "domains: [strategy]"),
        })
        got = [m for _, m in lint.run(root).problems if "не описана" in m]
        third.append(("навык ссылается на дисциплину, у которой есть карточка — тихо",
                      not got, f"нарушения {got}"))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {
            "work/me/me.md": SELF_HOLDER,
            "work/programs/asr/domains/d1.md": DOMAIN_CARD.format(did="strategy"),
            "work/programs/asr/domains/d2.md": DOMAIN_CARD.format(did="strategy"),
        })
        got = [m for _, m in lint.run(root).problems if "уже описана" in m]
        third.append(("одна дисциплина не может быть описана дважды",
                      bool(got), f"нарушения {got}"))

    # --- атлас: страница обязана показывать склад, а не своё представление о нём ---
    # Атлас — единственный вид, который человек читает целиком, и потому единственный,
    # где тихая ошибка дорога: пустая страница выглядит как «нечего показывать»,
    # оторванная от карт — как обновлённая. Проверяются оба провала.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {
            "work/me/me.md": SELF_HOLDER,
            "raw/sources/2026-01-01-s.md": SOURCE,
            "work/programs/asr/skills/a.md": SKILL_CARD.format(sid="S01", req=""),
            "work/programs/asr/skills/b.md": SKILL_CARD.format(
                sid="S02", req="requires: {S01: 3}\n"),
            "work/me/reading/k.md": reading(skills="{S01: 4}", status="read",
                                            started="2026-01-02", finished="2026-01-20",
                                            body="забрал"),
            "work/programs/asr/evidence/e.md":
                "---\ntype: evidence\nskill: work/programs/asr/skills/a.md\nlevel: 3\n"
                "date: 2026-02-01\norigin: raw/sources/2026-01-01-s.md\n"
                "context: real-world\nresult: провёл границу\n---\n\n# e",
        })
        data = atlas.collect(root, 3)
        page = atlas.render(data, dt.datetime(2026, 8, 6, 12, 0))
        third.append(("атлас берёт мерки у карты, а не считает их заново",
                      data["skills"]["S01"]["known"] == 4
                      and data["skills"]["S01"]["proved"] == 3,
                      (f"изучено {data['skills']['S01']['known']}, "
                       f"доказано {data['skills']['S01']['proved']}")))
        # Обратная связь предпосылок нигде не записана: её видно только отсюда.
        third.append(("атлас показывает, что навык открывает",
                      data["skills"]["S01"]["opens"] == [["S02", "S02", 3]],
                      f"открывает {data['skills']['S01']['opens']}"))
        third.append(("книга на странице навыка названа своим файлом",
                      data["skills"]["S01"]["books"] == [["k", 4]],
                      f"книги {data['skills']['S01']['books']}"))
        (root / "tools" / "coverkit").mkdir(parents=True, exist_ok=True)
        (root / "tools" / "coverkit" / "covers.json").write_text(
            json.dumps({"k": {"cover_i": 1, "data": "ZmFrZQ==",
                              "year": 1986, "pages": 315}}), "utf-8")
        with_cover = atlas.collect(root, 3)
        page_with_cover = atlas.render(with_cover, dt.datetime(2026, 8, 6, 12, 0))
        third.append(("обложка из кэша доходит до карточки книги",
                      with_cover["books"]["k"]["cover"] == "ZmFrZQ==",
                      f"обложка {with_cover['books']['k']['cover']!r}"))
        # Год и объём — единственное на странице книги, чего нет в складе:
        # они приходят из кэша стороннего сервиса и теряются молча, если
        # выгрузка перестанет их переносить.
        third.append(("выходные данные издания доходят до карточки книги",
                      (with_cover["books"]["k"]["year"],
                       with_cover["books"]["k"]["pages"]) == (1986, 315),
                      (f"год {with_cover['books']['k']['year']}, "
                       f"страниц {with_cover['books']['k']['pages']}")))
        # Описание живёт в теле карточки, а не в поле шапки: его легко потерять
        # правкой разбора — и страница книги снова перестанет отвечать, о чём она.
        third.append(("описание книги из тела карточки доходит до страницы",
                      "забрал" in with_cover["books"]["k"]["about"],
                      f"описание {with_cover['books']['k']['about']!r}"))
        third.append(("встроенная обложка попадает на страницу как данные",
                      "ZmFrZQ==" in page_with_cover,
                      "base64 обложки не нашлось на странице"))
        third.append(("доказательство доносит до страницы результат, уровень и дату",
                      data["skills"]["S01"]["evidence"]
                      == [["провёл границу", 3, "2026-02-01"]],
                      f"доказательства {data['skills']['S01']['evidence']}"))
        # Пути к файлам человек попросил убрать со страницы. Они лезут обратно
        # тремя дорогами: полем выгрузки, ссылкой разметки внутри текста и новым
        # блоком. Проверяется результат — путей в готовой странице нет вовсе.
        third.append(("путей склада на странице не остаётся",
                      "raw/" not in page and "work/" not in page,
                      "путь просочился в готовую страницу"))
        third.append(("ссылка разметки в тексте разворачивается в слова",
                      "](" not in page.split("window.WH = ")[1],
                      "ссылка разметки осталась в данных"))
        # Обложка встроена base64, не адресом: письмо без сети должно показать
        # картинку, а не дыру. Проверяется конкретный регресс — обращение к
        # чужому хосту, — а не буквальное «src=» в тексте: оно у страницы есть
        # всегда, потому что верстает её сама себя JS-строками.
        third.append(("обложка встроена, а не подгружается с чужого хоста",
                      "covers.openlibrary" not in page_with_cover,
                      "страница ссылается на внешний хост за картинкой"))
        third.append(("данные страницы не рвут её же разметку",
                      "</script>" not in page.split("window.WH = ")[1].split(";</script>")[0],
                      "закрывающий тег просочился в данные"))

    # --- профиль: избыток в одном навыке не закрывает пробел в другом ---
    # Главный риск целевого профиля — средний процент: он позволяет «достичь»
    # роли перекосом в сильную сторону. Потолок min(владение/цель, 1) существует
    # ровно против этого, и без проверки его легко потерять правкой формулы.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {
            "work/me/me.md": SELF_HOLDER,
            "raw/sources/2026-01-01-s.md": SOURCE,
            "work/programs/asr/skills/a.md": SKILL_CARD.format(sid="S01", req=""),
            "work/programs/asr/skills/b.md": SKILL_CARD.format(sid="S02", req=""),
            "work/programs/asr/builds/b.md":
                "---\ntype: build\nkey: ASR-B-x\nbuild_id: x\ntitle: Роль\n"
                "status: active\nrequires:\n  S01: {level: 2, weight: 1}\n"
                "  S02: {level: 2, weight: 1}\n---\n\n# Роль\n\nвступление\n",
            "work/programs/asr/evidence/e.md":
                "---\ntype: evidence\nskill: work/programs/asr/skills/a.md\nlevel: 5\n"
                "date: 2026-02-01\norigin: raw/sources/2026-01-01-s.md\n"
                "context: real-world\nresult: сделал\n---\n\n# e",
        })
        data = atlas.collect(root, 3)
        got = data["builds"][0]
        third.append(("пятый уровень при цели два даёт сто процентов, а не двести",
                      got["mastery"] == 50,
                      f"готовность {got['mastery']}% при одном закрытом из двух"))
        third.append(("профиль не достигнут, пока хоть одно требование ниже цели",
                      not got["complete"], f"достигнут: {got['complete']}"))
        third.append(("путь к профилю считает и требования, и их предпосылки",
                      got["base"] == 2 and got["baseProved"] == 1,
                      f"основание {got['base']}, доказано {got['baseProved']}"))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"work/me/me.md": SELF_HOLDER})
        code = atlas.main(["--root", str(root), "--dry-run"])
        third.append(("атлас без навыков и книг — провал, а не пустая страница",
                      code == 1, f"код возврата {code}"))

    # --- приветствие при входе: молчание опаснее ошибки ---
    # Приветствие читают в первую секунду сессии и по нему решают, чему верить.
    # Поэтому проверяется не «печатает ли оно текст», а обратное: не выдаёт ли
    # оно отсутствие данных за спокойное состояние.
    welcomes: list[tuple[str, bool, str]] = []
    ENTRY = dt.datetime(2026, 7, 29, 9, 0)
    SCREEN = ("---\ntype: attention\ngenerated: true\ngenerated_at: {when}\nsignals: {n}\n"
              "---\n\n# Требует внимания\n\n{body}\n")

    def screen(when: str, titles: list[str]) -> str:
        body = "\n".join(f"{i}. {t}" for i, t in enumerate(titles, 1))
        return SCREEN.format(when=when, n=len(titles), body=body)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"work/programs/p/program.md": holder})
        text = "\n".join(welcome.render(root, ENTRY, gates=False))
        welcomes.append(("экрана внимания нет — сказано вслух, а не «сигналов ноль»",
                         "Экрана внимания нет" in text, text))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"wiki/attention.md": "---\ntype: attention\nsignals: 3\n\n# a"})
        text = "\n".join(welcome.render(root, ENTRY, gates=False))
        welcomes.append(("нечитаемый экран — причина, а не тишина",
                         "не разобран" in text, text))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"work/programs/p/program.md": holder,
                     "wiki/attention.md": screen("2020-01-01T08:00", ["сигнал"])})
        text = "\n".join(welcome.render(root, ENTRY, gates=False))
        welcomes.append(("экран старше склада — предложено пересобрать",
                         "менялся после сборки" in text, text))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"work/programs/p/program.md": holder,
                     "wiki/attention.md": screen("2099-01-01T08:00", ["сигнал"])})
        text = "\n".join(welcome.render(root, ENTRY, gates=False))
        welcomes.append(("свежий экран не подгоняет пересобирать",
                         "менялся после сборки" not in text, text))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"wiki/attention.md":
                        screen("2099-01-01T08:00", [f"сигнал {i}" for i in range(1, 6)])})
        text = "\n".join(welcome.render(root, ENTRY, gates=False))
        welcomes.append(("длинный экран урезан, но остаток назван числом",
                         "сигнал 3" in text and "сигнал 4" not in text and "ещё 2" in text,
                         text))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"wiki/attention.md": screen("2099-01-01T08:00", ["сигнал"])})
        text = "\n".join(welcome.render(root, ENTRY, gates=False))
        welcomes.append(("склад вне git — сказано, что сверять нечем",
                         "вне git" in text, text))

    # --- неразобранное сырьё: сессия открывается незакрытым ---
    # Признак разобранного — ссылка из work/, а не полнота полей: 30 июля три
    # записи type: source с безупречными шапками не были видны ни одному сигналу.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"raw/inbox/2026-01-02-ideya.md": SOURCE,
                     "work/programs/p/program.md": holder})
        text = "\n".join(welcome.render(root, ENTRY, gates=False))
        welcomes.append(("неразобранное сырьё — прежде всего остального, с предложением",
                         "Неразобрано: 1" in text and "разобрать?" in text
                         and text.find("Неразобрано") < text.find("вне git"), text))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"raw/inbox/2026-01-02-ideya.md": SOURCE,
                     "work/programs/p/program.md":
                        holder + "\n\nразобрано: raw/inbox/2026-01-02-ideya.md"})
        text = "\n".join(welcome.render(root, ENTRY, gates=False))
        welcomes.append(("сырьё со ссылкой из work — разобрано, строки нет",
                         "Неразобрано" not in text, text))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {f"raw/inbox/2026-01-0{i}-zapis.md": SOURCE for i in range(1, 6)})
        text = "\n".join(welcome.render(root, ENTRY, gates=False))
        welcomes.append(("длинный список неразобранного урезан с числом остатка",
                         "Неразобрано: 5" in text and "ещё 2" in text
                         and "2026-01-04" not in text, text))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"raw/inbox/index.md": "---\ntype: index\n---\n\n# оглавление"})
        text = "\n".join(welcome.render(root, ENTRY, gates=False))
        welcomes.append(("оглавление зоны приёма — не запись, строки нет",
                         "Неразобрано" not in text, text))

    connectors: list[tuple[str, bool, str]] = []

    # --- общий приём внешних записей: tools/capture.py ---
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, {"raw/log.md": "---\ntype: log\ntitle: Журнал\n---\n\n# Журнал\n"})
        payload = root / "vhod.json"
        запись = {"source": "plaud", "external_id": "rec_1", "date": "2026-08-14",
                  "title": "Планёрка по маршрутам", "body": "Расшифровка."}
        payload.write_text(json.dumps([запись], ensure_ascii=False), encoding="utf-8")

        code = capture.main(["--file", str(payload), "--root", str(root)])
        принято = list((root / "raw" / "inbox").glob("*.md"))
        connectors.append(("внешняя запись принята в зону приёма одним файлом",
                           code == 0 and len(принято) == 1, f"код {code}, файлов {len(принято)}"))

        capture.main(["--file", str(payload), "--root", str(root)])
        connectors.append(("повторный запуск приёма не создаёт второй копии",
                           len(list((root / "raw" / "inbox").glob("*.md"))) == 1,
                           f"файлов {len(list((root / 'raw' / 'inbox').glob('*.md')))}"))

        payload.write_text(json.dumps([{**запись, "revision": "2"}], ensure_ascii=False),
                           encoding="utf-8")
        capture.main(["--file", str(payload), "--root", str(root)])
        connectors.append(("изменённая версия внешнего объекта — новое событие, а не правка",
                           len(list((root / "raw" / "inbox").glob("*.md"))) == 2,
                           f"файлов {len(list((root / 'raw' / 'inbox').glob('*.md')))}"))

        payload.write_text(json.dumps([{"source": "plaud"}], ensure_ascii=False), encoding="utf-8")
        code = capture.main(["--file", str(payload), "--root", str(root)])
        connectors.append(("неполная запись останавливает приём, а не пропускается молча",
                           code == 2, f"код {code}"))

        payload.write_text("не json вовсе", encoding="utf-8")
        code = capture.main(["--file", str(payload), "--root", str(root)])
        connectors.append(("неразобранный вход называет себя, а не считается пустым",
                           code == 2, f"код {code}"))

    for title, ok, why in third + welcomes + connectors:
        if ok:
            print(f"✓ {title}")
        else:
            failures.append(f"НЕ СОШЛОСЬ: {title} — {why}")

    for title, ok, why in extra:
        if ok:
            print(f"✓ {title}")
        else:
            failures.append(f"НЕ СОШЛОСЬ: {title} — {why}")

    total = counted_stdout.successes + len(failures)
    sys.stdout = original_stdout

    print()
    if failures:
        for text in failures:
            print(f"✗ {text}")
        print(f"\nVERIFY: не сошлось {len(failures)} случаев из {total}")
        return 1
    print(f"VERIFY: сошлись все {total} случаев")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
