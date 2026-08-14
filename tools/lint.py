#!/usr/bin/env python3
"""Проверка склада на соответствие схеме.

Не украшение, а замена дисциплины, которой нет. Правила — docs/storage-schema.md,
происхождение каждой проверки — docs/known-failure-modes.md.

Два уровня строгости:
  **нарушение** — склад противоречит правилам, сборка красная;
  **замечание** — обещание без исполнителя (вид объявлен, но сборщика нет).
    Показывается, но не валит: постоянно красный гейт перестают читать.

Главное свойство: проверка, обработавшая ноль файлов, — это провал, а не успех.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import itertools
import re
import subprocess
from pathlib import Path

import activity
import store
import welcome
import yaml

SORTS = ("raw", "work", "wiki")
RESERVED = {"index.md", "log.md"}   # OKF: навигация и журнал, не содержимое
# Чужой код рядом со складом: библиотеки узлов и виртуальное окружение python.
# Их markdown — лицензии и readme зависимостей, а не заметки без сорта.
VENDOR_DIRS = {"node_modules", ".venv", "venv"}

# Обязательные поля сверх `type` — по типам из docs/storage-schema.md
REQUIRED = {
    "meeting": ["date", "container", "source_ref"],
    "interview": ["date", "person", "mode"],
    "observation": ["source", "kind"],
    "digest": ["source", "container", "date"],
    "source": ["date", "title"],
    "commitment": ["key", "direction", "status", "origin"],
    "question": ["key", "title", "status", "origin"],
    "decision": ["key", "status", "date"],
    "risk": ["key", "status", "origin"],
    "idea": ["key"],
    "hypothesis": ["status", "action", "expected", "rationale"],
    "constraint": ["key", "status", "container", "leverage"],
    "sprint": ["key", "status", "starts", "ends"],
    "goal": ["kind", "horizon", "status", "created"],
    "reading": ["key", "title", "kind", "topics", "tier", "status", "source"],
    "build": ["key", "build_id", "title", "status"],
    "domain": ["key", "domain_id", "title", "order", "status"],
    "skill": ["key", "skill_id", "title", "domains", "node", "target", "status"],
    "evidence": ["skill", "level", "result", "date", "origin"],
    "process": ["key", "title", "status", "container"],
    "spec": ["key", "status", "created", "source"],
    "metrics": ["status"],
    "person": ["name"],
    "client": ["mode", "prefix"],
    "program": ["mode", "prefix"],
    "self": ["mode", "prefix"],
    "engagement": ["client", "stage"],
    "index": [],
    "log": [],
    "attention": [],
    "self-review": [],
    "reading-map": [],
    "skill-map": [],
}

ENUMS = {
    ("commitment", "status"): {"open", "in-progress", "waiting", "resolved", "cancelled"},
    ("commitment", "direction"): {"outbound", "inbound"},
    ("question", "status"): {"open", "resolved", "cancelled"},
    ("decision", "status"): {"proposed", "accepted", "superseded"},
    ("decision", "weight"): {"small", "medium", "large"},
    ("risk", "status"): {"open", "mitigated", "accepted", "materialized"},
    ("hypothesis", "status"): {"open", "supported", "refuted"},
    ("hypothesis", "stage"): {"H", "A", "D", "I"},
    ("constraint", "status"): {"open", "removed"},
    ("constraint", "leverage"): {"high", "medium", "low"},
    ("sprint", "status"): {"planned", "active", "closed"},
    ("process", "status"): {"mapping", "described", "agreed", "dropped"},
    ("spec", "status"): {"draft", "ready", "in-progress", "shipped", "dropped"},
    ("metrics", "status"): {"candidates", "partial", "system"},
    ("goal", "kind"): {"skill", "result"},
    ("goal", "status"): {"active", "reached", "dropped"},
    ("reading", "status"): {"queued", "reading", "read", "dropped"},
    ("reading", "kind"): {"book", "article", "paper", "course", "talk"},
    ("reading", "level"): {"now", "next", "later"},
    ("reading", "tier"): {"база", "ядро", "практика", "продвинутый", "экспертный",
                          "справочник", "дополнительно"},
    ("build", "status"): {"active", "achieved", "retired"},
    ("domain", "status"): {"named", "specified", "retired"},
    ("skill", "node"): {"atomic", "compound", "meta"},
    ("skill", "status"): {"named", "specified", "retired"},
    ("skill", "target"): {0, 1, 2, 3, 4, 5},
    ("evidence", "context"): {"micro", "resource", "cluster", "build", "real-world"},
    ("self", "mode"): {"active", "idle", "maintained", "archived"},
    ("interview", "mode"): {"generative", "verification"},
    ("observation", "kind"): {"behaviour", "opinion"},
    ("observation", "force"): {"push", "pull", "anxiety", "habit"},
    ("hypothesis", "risk_type"): {"value", "usability", "feasibility", "viability"},
    ("client", "mode"): {"active", "idle", "maintained", "archived"},
    ("program", "mode"): {"active", "idle", "maintained", "archived"},
    # Этап работы с клиентом. До 30 июля словаря не было, и в складе жило одно
    # значение — active. Второй этап Альфа («разобрать бизнес целиком») пришёл в
    # состоянии «предложен», и слово для него выбиралось на глаз: без словаря
    # следующий выбрал бы своё, а разные слова для одного состояния — это
    # молчаливый развал отбора по стадии.
    ("engagement", "stage"): {"proposed", "active", "closed", "dropped"},
}

# Папки, где живут записи о произошедшем. Только они годятся в доказательства:
# проверка «путь начинается с raw/» пропускала наш собственный разбор, если он
# лежал в raw/. Провенанс — это откуда взялось, а не «лежит в правильной папке».
EVENT_FOLDERS = ("raw/meetings/", "raw/interviews/", "raw/sources/", "raw/inbox/")

PATH_FIELDS = ("origin", "source", "container", "resolution", "client", "person",
               "mitigation", "owner", "counterpart", "became", "constraint", "goal",
               "idea", "process", "skill", "spec")
LIST_FIELDS = ("evidence", "participants", "blocks", "supported_by", "refuted_by",
               "hypotheses", "constraints", "related", "email", "phone")

# Что имеет право лежать не в трёх корнях: контракт, документация, своды правил
# ролей. Всё остальное — заметка, которая не объявила свой сорт.
# Код и его настройки — не заметки: README рядом с инструментом не обязан
# объявлять сорт. Список закрытый: всё, чего в нём нет, — заметка без сорта.
OUTSIDE_DIRS = ("docs", "agents", "tools", "config", ".github", ".claude", ".backlog")
OUTSIDE_FILES = {"AGENTS.md", "CLAUDE.md", "README.md"}
COMMITMENT_GRACE_DAYS = 7   # только что записанное обещание имеет право быть неполным

# Поля, которые может проставить только приём: какой контейнер, кто участник,
# каков режим разговора. Коннектор их не знает и знать не может — это суждение,
# а не факт из записи. Требовать их на входе значит требовать результат разбора
# до разбора; ровно на этом схема разошлась с первым же внешним писателем.
BY_INTAKE = {
    "meeting": ["container"],
    "interview": ["person", "mode"],
}

EVENT_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}-[a-z0-9-]+\.md$")
SLUG_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*\.md$")
LINK = re.compile(r"\[[^\]]*\]\(([^)#\s]+\.md)[^)]*\)")
FENCE = re.compile(r"```.*?```", re.DOTALL)
CODE = re.compile(r"`[^`\n]*`")


def strip_code(text: str) -> str:
    """Ссылка внутри кода — пример синтаксиса, а не ссылка."""
    return CODE.sub("", FENCE.sub("", text))


class Report:
    def __init__(self) -> None:
        self.problems: list[tuple[str, str]] = []
        self.notices: list[tuple[str, str]] = []
        self.checked = 0
        self.links_checked = 0
        # Сколько объектов обработала каждая проверка. Общий страж ловит только
        # полное отсутствие файлов: при нуле обязательств check_commitments делал
        # ноль итераций, а гейт печатал «чисто». Требовать наличия каждого типа
        # нельзя — это ложное ограничение; но покрытие обязано быть названо.
        self.coverage: dict[str, int] = {}

    def fail(self, where: str, message: str) -> None:
        self.problems.append((where, message))

    def notice(self, where: str, message: str) -> None:
        self.notices.append((where, message))

    def covered(self, check: str, count: int = 1) -> None:
        self.coverage[check] = self.coverage.get(check, 0) + count


def frontmatter(text: str) -> dict | None:
    """Оставлено для совместимости; разбор один на всех — в store.parse."""
    return store.parse(text)[0]


def resolve_link(root: Path, path: Path, target: str) -> Path:
    """Ссылка от корня склада или относительно файла."""
    if target.startswith(SORTS) or target.startswith("docs/"):
        return root / target
    return path.parent / target


def check_links(root: Path, path: Path, text: str, report: Report) -> None:
    for target in LINK.findall(strip_code(text)):
        if target.startswith(("http://", "https://")):
            continue
        report.links_checked += 1
        if not resolve_link(root, path, target).exists():
            report.fail(str(path.relative_to(root)), f"ссылка в никуда: {target}")


def is_capture(rel: str, data: dict, intake: dict) -> bool:
    """Сырьё, ещё не прошедшее приём.

    Признак один — место. Путь есть идентификатор, значит папка и объявляет сорт:
    лежащее в зоне приёма по определению не разобрано.

    Был и второй, временный: `source` называет известную программу. Он держал под
    мягкой меркой записи стенографиста, пока тот писал мимо зоны. Убран, когда
    стенографист перенаправлен: список знакомых программ — это дыра, которая
    расширяется с каждой новой программой, а место работает для любой.
    """
    if Path(rel).name in {"index.md", "log.md"}:
        return False   # зарезервированы OKF: навигация и журнал, а не запись
    zone = str(intake.get("zone") or "raw/inbox").rstrip("/") + "/"
    return rel.startswith(zone)


def check_store_file(root: Path, path: Path, report: Report, keys: dict[str, str],
                     intake: dict | None = None) -> None:
    relative = path.relative_to(root)
    sort = relative.parts[0]
    text = path.read_text(encoding="utf-8")
    where = str(relative)

    data, why = store.parse(text)
    if data is None:
        report.fail(where, why or "шапка отсутствует или не разбирается")
        return

    report.checked += 1
    note_type = data.get("type")
    if not isinstance(note_type, str) or not note_type:
        report.fail(where, "нет поля type")
        return
    if note_type not in REQUIRED:
        report.fail(where, f"неизвестный тип: {note_type}")
        return

    # Сырьё держится минимальной меркой: читается, знает свою дату и источник.
    # Остальное — работа приёма, и её отсутствие не нарушение, а очередь.
    required = REQUIRED[note_type]
    if is_capture(where, data, intake or {}):
        required = [f for f in required if f not in BY_INTAKE.get(note_type, [])]

    for field in required:
        if field not in data:
            report.fail(where, f"{note_type}: нет обязательного поля {field}")
        elif data[field] in (None, "", []):
            report.fail(where, f"{note_type}: поле {field} пустое")
    for field in LIST_FIELDS:
        if field in data and data[field] is not None and not isinstance(data[field], list):
            # строка вместо списка перебиралась бы посимвольно и молча давала мусор
            report.fail(where, f"{field} должно быть списком, а не {type(data[field]).__name__}")

    for (kind, field), allowed in ENUMS.items():
        if note_type == kind and field in data and data[field] not in allowed:
            report.fail(where, f"{field}={data[field]!r} вне словаря {sorted(allowed)}")

    # Ключи уникальны по всему складу — коллизия CSS-Q-001 уже случалась
    key = data.get("key")
    if key:
        if key in keys:
            report.fail(where, f"ключ {key} уже занят: {keys[key]}")
        else:
            keys[key] = where

    # Имена: события с датой, остальное слагом. Путь — идентификатор и аргумент команды
    name = path.name
    if sort == "raw" and name not in {"index.md", "log.md"} and not EVENT_NAME.match(name):
        report.fail(where, "событие должно называться ГГГГ-ММ-ДД-слаг.md")
    if not EVENT_NAME.match(name) and not SLUG_NAME.match(name):
        report.fail(where, "имя не машиночитаемое: строчные, цифры, дефисы")

    # Виды пересчитываются — ручная правка бессмысленна, и это должно быть видно
    if sort == "wiki" and data.get("generated") is not True:
        report.fail(where, "вид без generated: true")
    # Обещание без исполнителя: вид объявлен, но ни разу не собирался
    if data.get("generated") is True and not data.get("generated_at"):
        report.notice(where, "вид объявлен, но никогда не собирался — сборщика нет")

    check_links(root, path, text, report)

    for field in PATH_FIELDS:
        value = data.get(field)
        if isinstance(value, str) and value.endswith(".md") and not (root / value).exists():
            report.fail(where, f"{field} указывает в никуда: {value}")
        if isinstance(value, str) and value in ("", "null"):
            report.fail(where, f"{field} пустой")
    for field in LIST_FIELDS:
        for value in data.get(field) or []:
            if isinstance(value, str) and value.endswith(".md") and not (root / value).exists():
                report.fail(where, f"{field}: ссылка в никуда: {value}")

    # Цель — позиция контейнера «Я». Вне его она печаталась с пустым контейнером
    # и не подчинялась ничьему режиму: тип есть, принадлежности нет.
    if note_type == "goal" and not where.startswith("work/me/"):
        report.fail(where, "цель вне контейнера «Я»: место цели — work/me/goals/")

    # Заявленный контейнер должен совпадать с фактическим расположением
    container = data.get("container")
    if (isinstance(container, str) and container.startswith("work/") and sort == "work"
            and not where.startswith(container)):
        report.fail(where, f"container={container} не совпадает с расположением")


def check_raw_immutability(root: Path, report: Report) -> None:
    """События не переписываются. Проверяется историей, а не обещанием в документе."""
    result = subprocess.run(
        ["git", "-C", str(root), "log", "--diff-filter=MDR", "--format=%x00%h",
         "--name-status", "--", "raw"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode:
        report.notice("raw", "неизменность событий не проверена: git недоступен")
        return
    commit = ""
    # Правка, удаление и переименование — три разных поступка, и одно слово
    # «изменялось» их путало. Плюс сводка по коммиту вместо строки на файл:
    # двадцать три отдельных замечания о разовой уборке — это шум, который
    # заставляет перестать читать замечания вообще.
    KIND = {"M": "правлено", "D": "удалено", "R": "переименовано"}
    by_commit: dict[tuple[str, str], list[str]] = {}
    seen: set[str] = set()
    for line in result.stdout.splitlines():
        # Хеш помечен явным разделителем, а не угадывается по длине строки
        if line.startswith("\x00"):
            commit = line[1:]
            continue
        parts = line.split("\t")
        if len(parts) < 2 or not parts[-1].endswith(".md"):
            continue
        path, kind = parts[-1].strip(), KIND.get(parts[0][:1], "тронуто")
        if Path(path).name in RESERVED or path in seen:
            # index.md и log.md зарезервированы OKF: это навигация и журнал о
            # папке, а не события в ней. Правило неизменности — про события.
            continue
        seen.add(path)
        by_commit.setdefault((commit, kind), []).append(path)
    for (where, kind), paths in sorted(by_commit.items()):
        if len(paths) == 1:
            report.notice(paths[0], f"событие {kind} после создания (коммит {where})")
        else:
            report.notice("raw", f"коммит {where}: {kind} событий — {len(paths)}, "
                                 f"первое {min(paths)}")


def check_stale(root: Path, report: Report, notes: list) -> None:
    """Позиции, застрявшие в начальном состоянии дольше порога — обещание схемы."""
    today = dt.date.today()
    limits = {("spec", "draft"): 90, ("metrics", "candidates"): 60, ("idea", None): 180}
    report.covered("застревание", sum(1 for n in notes
                                      if n.type in {"spec", "metrics", "idea"}))
    for note in notes:
        for (kind, status), days in limits.items():
            if note.type != kind or (status and note.status != status):
                continue
            if note.mode and note.mode != "active":
                continue  # idle и архив не стареют — как и в экране внимания
            when = note.date_field("created", "opened", "date")
            if when and (today - when).days > days:
                report.notice(note.rel,
                              f"{kind} в состоянии «{note.status or 'начальное'}» "
                              f"{(today - when).days} дн. при пороге {days}")


def check_ideas(report: Report, notes: list, conf: dict) -> None:
    """Идея, к которой не привязана ни одна гипотеза, — это мнение, а не замысел.

    Тот же ход, что у цели без шагов: замысел знает, чем его проверяют, только
    через обратную ссылку — гипотеза называет свою идею, а не наоборот. Иначе
    один факт лежал бы в двух местах.
    """
    days = int((conf.get("stale_days") or {}).get("idea_without_hypothesis", 90))
    today = dt.date.today()
    ideas = [n for n in notes if n.type == "idea"]
    report.covered("идеи", len(ideas))
    tested = {str(n.data.get("idea") or "") for n in notes if n.type == "hypothesis"}
    for note in ideas:
        if note.rel in tested or note.data.get("became"):
            continue
        when = note.date_field("created", "opened", "date")
        if when and (today - when).days > days:
            report.notice(note.rel, f"идея без единой гипотезы {(today - when).days} дн. "
                                    f"при пороге {days} — пока это мнение, а не замысел")


def check_answered_questions(report: Report, notes: list) -> None:
    """Связь есть, а статус ей противоречит.

    Первая версия ругалась на любой открытый вопрос в контейнере, где есть хоть
    одно принятое решение — шесть срабатываний из шести оказались ложными. Шумный
    сигнал не удаляют, а настраивают: теперь нужна ЯВНАЯ связь.
    """
    by_rel = {n.rel: n for n in notes}
    report.covered("вопросы против решений",
                   sum(1 for n in notes if n.type in {"question", "decision"}))
    for note in notes:
        # вопрос указывает, чем разрешён, но сам всё ещё открыт
        resolution = str(note.data.get("resolution") or "")
        if note.type == "question" and note.status == "open" and resolution in by_rel:
            report.fail(note.rel, f"вопрос открыт, хотя указано разрешение {resolution}")
        # решение ссылается на вопрос, который до сих пор открыт
        if note.type == "decision" and note.status == "accepted":
            for target in LINK.findall(strip_code(note.path.read_text(encoding="utf-8"))):
                resolved = (note.path.parent / target).resolve()
                for other in notes:
                    if other.path.resolve() == resolved and other.type == "question" \
                            and other.status == "open":
                        report.notice(other.rel,
                                      f"решение {note.key or note.title} ссылается на этот "
                                      "вопрос, а он открыт")


def check_archive(root: Path, report: Report) -> None:
    """Архиву — своя минимальная мерка: не схема, а читаемость и наличие в git.

    Полное исключение 235 файлов из проверки давало вторую систему со своими
    правилами качества. Схему к чужим заметкам не применяем — но молчать о том,
    что часть из них нечитаема, тоже нельзя.
    """
    base = root / "work" / "archive"
    if not base.exists():
        return
    total = unreadable = 0
    for path in base.rglob("*.md"):
        total += 1
        try:
            path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            unreadable += 1
            report.fail(str(path.relative_to(root)), f"архив: файл не читается ({type(exc).__name__})")
    if total:
        report.notice("work/archive", f"архив: {total} файлов, нечитаемых {unreadable}; "
                                      "схема к нему не применяется — только читаемость")


def check_outside_roots(root: Path, path: Path, report: Report) -> None:
    """Файл вне трёх корней — нарушение инварианта сорта.

    Заметка обязана объявить, что она такое: событие, позиция или вид. Файл,
    лежащий сбоку, не принадлежит ни одному сорту — значит, про него неизвестно,
    можно ли его править и кто им владеет. Ровно так в предыдущей системе
    появлялись заметки, которых не видел ни один инструмент.
    """
    relative = path.relative_to(root)
    if relative.parts[0] in SORTS or relative.parts[0] in OUTSIDE_DIRS:
        return
    if len(relative.parts) == 1 and relative.name in OUTSIDE_FILES:
        return
    report.fail(str(relative), "файл вне трёх корней: не событие, не позиция, не вид — "
                               "сорт не объявлен, права на него неизвестны")


def _origin_date(note) -> dt.date | None:
    """Дата события-источника: она в его имени, потому что события так называются."""
    match = store.DATE.search(str(note.data.get("origin") or ""))
    if not match:
        return None
    try:
        return dt.date.fromisoformat(match.group(0))
    except ValueError:
        return None


def check_commitments(report: Report, notes: list) -> None:
    """Обязательство, которое нечем исполнить.

    Норма — docs/storage-schema.md: владелец обязателен всегда, а у взятого на
    «сейчас» должен быть либо срок, либо зацепка `when_then`. Без того и другого
    это не позиция, а запись о намерении: непонятно, кто делает и с чего
    начинается. Порог в неделю от даты источника — чтобы не придираться к тому,
    что записано только что и ещё не разобрано.
    """
    today = dt.date.today()
    all_commitments = [n for n in notes if n.type == "commitment"]
    live = [n for n in all_commitments if n.status in {"open", "in-progress"}]
    report.covered("обязательства", len(live))

    # Даты рабочего цикла появились 31 июля. Историческим закрытым позициям без
    # дат нельзя приписывать выдуманное прошлое, поэтому они остаются допустимым
    # baseline. Но как только цикл начат (`started`), потерять его конец уже
    # нельзя; новые переходы всегда проставляют обе даты.
    for note in all_commitments:
        started_raw = note.data.get("started")
        resolved_raw = note.data.get("resolved")
        started = note.date_field("started")
        resolved = note.date_field("resolved")
        if note.status == "in-progress" and not started_raw:
            report.fail(note.rel, "в работе без started: неизвестно, когда начался цикл")
        elif started_raw and started is None:
            report.fail(note.rel, f"started не является датой: {started_raw!r}")
        if note.status in {"resolved", "cancelled"} and started_raw and not resolved_raw:
            report.fail(note.rel, "цикл закрыт без resolved: длительность потеряна")
        elif resolved_raw and resolved is None:
            report.fail(note.rel, f"resolved не является датой: {resolved_raw!r}")
        if started and resolved and resolved < started:
            report.fail(note.rel, "resolved раньше started: конец цикла предшествует началу")

    for note in live:
        if not str(note.data.get("owner") or "").strip():
            report.fail(note.rel, "обязательство без владельца: непонятно, кто исполняет")
        if note.data.get("level") != "now":
            continue
        when_then = note.data.get("when_then")
        cue = str((when_then or {}).get("cue") or "").strip() if isinstance(when_then, dict) else ""
        if note.data.get("due") or cue:
            continue
        started = note.date_field("opened", "created", "date") or _origin_date(note)
        age = (today - started).days if started else None
        if age is None or age > COMMITMENT_GRACE_DAYS:
            tail = f", лежит {age} дн." if age is not None else ""
            report.notice(note.rel, "взято на «сейчас», но ни срока, ни зацепки — "
                                    f"начать нечем{tail}")


def check_evidence(report: Report, notes: list) -> None:
    """Решение обязано опираться на событие, а не на нашу же интерпретацию.

    В схеме это записано как «evidence в статусе ниже `verified`», но статуса
    `verified` в складе нет и не появилось: источник не помечается проверенным —
    он либо лежит в `raw/`, либо его нет. Поэтому проверяем то, что действительно
    проверяемо: происхождение доказательства. Ссылка на `work/` означает, что
    решение опирается на другую нашу позицию, а та — неизвестно на что.
    """
    decisions = [n for n in notes if n.type == "decision"]
    report.covered("решения", len(decisions))
    for note in decisions:
        evidence = note.data.get("evidence") or []
        if note.status == "accepted" and not evidence:
            report.fail(note.rel, "принятое решение без evidence: пересмотреть его можно "
                                  "только собрав контекст заново")
        for item in evidence if isinstance(evidence, list) else []:
            if isinstance(item, str) and item.endswith(".md") \
                    and not item.startswith(EVENT_FOLDERS):
                report.fail(note.rel, f"evidence ведёт не в запись о произошедшем: {item} — "
                                      "доказательством может быть только событие, "
                                      f"то есть файл из {', '.join(EVENT_FOLDERS)}")


WORD = re.compile(r"[а-яёa-z0-9]+")
DUPLICATE_TYPES = {"commitment", "question", "risk", "decision", "spec", "idea",
                   "goal", "process"}
# Книги в общей мере не участвуют, и это не поблажка, а признание её границ.
# Названия книг похожи по природе жанра: «Networks» против «Network Science» —
# разные книги, а лексически близнецы. На каталоге в шестьсот записей общая мера
# дала 323 замечания, из которых верными были единицы: столько шума не читают,
# и вместе с ним перестают читать настоящие находки. Своя мера — ниже, в
# check_reading: она смотрит на автора, а не только на слова в заголовке.


def stems(title: str) -> set[str]:
    """Слова, усечённые до основы.

    Посимвольное сравнение на русском шумит: «Назначить представителя» и
    «Согласовать назначение» делят половину букв, ничего общего не имея по
    смыслу. Усечение до пяти букв снимает падежи и склонения, а разделение на
    слова снимает порядок — так «Отправить КП клиенту» и «Отправить клиенту
    коммерческое предложение» оказываются рядом, чем они и являются.
    """
    return {w[:5] for w in WORD.findall(title.lower()) if len(w) > 2}


def similarity(a: str, b: str) -> float:
    first, second = stems(a), stems(b)
    return len(first & second) / len(first | second) if first | second else 0.0


def check_duplicates(report: Report, notes: list, conf: dict) -> None:
    """Две позиции, похожие на одну и ту же. **Всегда замечание, никогда нарушение.**

    Это наблюдение, а не вывод. Совпадение слов — не доказательство, что речь об
    одном деле, и контракт прямо запрещает превращать одно в другое молча: агент
    показывает, что увидел, решает человек. Поэтому здесь нет ни автоматического
    слияния, ни красного гейта — только пара путей и мера сходства.

    Мера лексическая, и её границы надо назвать честно: она ловит **пересказ**
    («Отправить КП» ≈ «Отправить коммерческое предложение») и не ловит
    **синонимы** («согласовать цену» ≈ «утвердить стоимость»). Второе требует
    разбора смысла, которого у линтера нет и без внешней модели не будет.

    Общий источник — независимое свидетельство: две позиции из одного события
    чаще всего одно предложение, разобранное дважды. Поэтому для них порог ниже.
    """
    limits = conf.get("duplicates") or {}
    general = float(limits.get("threshold", 0.35))
    same_origin = float(limits.get("same_origin_threshold", 0.25))
    pool = [n for n in notes if n.type in DUPLICATE_TYPES and n.title]
    report.covered("похожие позиции", len(pool))
    for first, second in itertools.combinations(pool, 2):
        if first.type != second.type:
            continue
        shared = (first.data.get("origin")
                  and first.data.get("origin") == second.data.get("origin"))
        score = similarity(first.title, second.title)
        if score < (same_origin if shared else general):
            continue
        why = "из одного события" if shared else "в разных источниках"
        report.notice(first.rel,
                      f"похоже на дубль {second.rel} — сходство {score:.0%}, {why}. "
                      f"«{first.title}» ({first.status or '—'}) против "
                      f"«{second.title}» ({second.status or '—'}). Судить тебе: "
                      "совпадение слов не доказывает, что дело одно")


def body_text(path: Path) -> str:
    """Тело записи без шапки, но с заголовками."""
    text = path.read_text(encoding="utf-8")
    parts = text.split("---", 2)
    return (parts[2] if len(parts) == 3 else text).strip()


def body_of(path: Path) -> str:
    """Тело записи без шапки и без заголовков — только содержательные строки.

    Заголовки выбрасываются намеренно: «прочитано, а внутри пусто» не должно
    закрываться одной строчкой с названием книги. Но искать по этому тексту
    заголовки бесполезно — их здесь уже нет; для этого есть `body_text`.
    """
    lines = [one.strip() for one in body_text(path).splitlines()]
    return "\n".join(one for one in lines if one and not one.startswith("#")).strip()


def check_reading(report: Report, notes: list, conf: dict) -> None:
    """Литература: рабочий цикл, причина отказа, чем начинается и что осталось.

    Тот же цикл, что у обязательства, и по той же причине: начатое чтение — это
    незавершённая работа, а незавершённую работу нельзя измерить, если неизвестно,
    когда она началась. Очередь при этом не проверяется на возраст вообще: полка
    имеет право лежать годами, и требовать от неё движения значит превратить
    библиотеку в источник вины.

    Замечания — про две вещи, которые ломают чтение тише всего: книгу, взятую на
    «сейчас» без единой зацепки (начать нечем), и прочитанное, от которого не
    осталось ни строки (отметка о посещении вместо знания).
    """
    items = [n for n in notes if n.type == "reading"]
    report.covered("литература", len(items))
    if not items:
        return

    for note in items:
        started_raw, finished_raw = note.data.get("started"), note.data.get("finished")
        started, finished = note.date_field("started"), note.date_field("finished")
        if note.status == "reading" and not started_raw:
            report.fail(note.rel, "в чтении без started: неизвестно, когда начата")
        elif started_raw and started is None:
            report.fail(note.rel, f"started не является датой: {started_raw!r}")
        if note.status in {"read", "dropped"} and started_raw and not finished_raw:
            report.fail(note.rel, "чтение закрыто без finished: длительность потеряна")
        elif finished_raw and finished is None:
            report.fail(note.rel, f"finished не является датой: {finished_raw!r}")
        if started and finished and finished < started:
            report.fail(note.rel, "finished раньше started: конец предшествует началу")
        if note.status == "dropped" and not str(note.data.get("dropped_reason") or "").strip():
            report.fail(note.rel, "брошено без причины: через месяц отказ читается как "
                                  "«почему-то не пошло» и книга возвращается в очередь")

        when_then = note.data.get("when_then")
        cue = str((when_then or {}).get("cue") or "").strip() if isinstance(when_then, dict) else ""
        if note.data.get("level") == "now" and note.status == "queued" and not cue:
            report.notice(note.rel, "взято на «сейчас», но ни зацепки, ни начала — "
                                    "маршрут расписан, а с чего он начинается, не сказано")
        if note.status == "read" and not body_of(note.path):
            report.notice(note.rel, "прочитано, а в записи пусто: это отметка о посещении, "
                                    "а не знание. Хотя бы три строки — что забрал")

    # Одна книга дважды. Мера здесь своя, потому что общая на книгах шумит:
    # у книг похожие названия — норма жанра. Довод даёт автор: две записи одного
    # автора с близкими названиями почти всегда одна книга, попавшая в каталог из
    # двух списков под разными формами имени. Замечание, не нарушение.
    by_author: dict[str, list] = {}
    for note in items:
        author = " ".join(sorted(stems(str(note.data.get("author") or ""))))
        if author:
            by_author.setdefault(author, []).append(note)
    for same in by_author.values():
        for first, second in itertools.combinations(same, 2):
            score = similarity(first.title, second.title)
            if score >= 0.5:
                report.notice(first.rel,
                              f"похоже на ту же книгу, что {second.rel} — тот же автор, "
                              f"сходство названий {score:.0%}. «{first.title}» против "
                              f"«{second.title}»")

    # Два направления об одном. Семь библиотек пришли из разных генераций, и
    # «процессы» против «бизнес-процессов» — вопрос времени, а не гипотеза. Всегда
    # замечание: совпадение слов не доказывает, что дисциплина одна, судит человек.
    threshold = float((conf.get("reading") or {}).get("topic_similarity", 0.45))
    first_seen: dict[str, str] = {}
    for note in items:
        topics = note.data.get("topics")
        topics = topics if isinstance(topics, list) else [topics]
        named = [str(one).strip() for one in topics if str(one or "").strip()]
        if not named:
            report.fail(note.rel, "ни одной дисциплины: книга не принадлежит ничему, "
                                  "значит не найдётся ни по одному направлению")
        for one in named:
            first_seen.setdefault(one, note.rel)
    for one, two in itertools.combinations(sorted(first_seen), 2):
        score = similarity(one.replace("-", " "), two.replace("-", " "))
        if score >= threshold:
            report.notice(first_seen[one],
                          f"направление «{one}» похоже на «{two}» — сходство {score:.0%}. "
                          "Две дисциплины об одном разъедутся на первой же новой книге")


LEVEL_HEAD = re.compile(r"^###\s*(\d)\s*[—-]", re.MULTILINE)


def check_skills(report: Report, notes: list) -> None:
    """Навыки: идентификатор один на всю систему, предпосылки без петель,
    описанные уровни — до целевого включительно.

    Первое правило взято из разбора исходного пакета: `Systems Diagnosis` жил
    тремя записями — `S08`, `B03` и `M02`. Пока это три файла, изученная глубина
    и доказанное владение расходятся между копиями, а сводка считает один навык
    трижды. Поэтому `skill_id` уникален, а прежние имена живут полем `aliases`.

    Второе — про уровни: навык, объявленный описанным, но без дескрипторов,
    невозможно ни оценить, ни проверить. «Уровень 3 — применяет» одинаковым
    текстом для всех навыков не описывает ничего, поэтому требуется отдельный
    раздел на каждый уровень до целевого.
    """
    skills = [n for n in notes if n.type == "skill"]
    report.covered("навыки", len(skills))
    # Имя навыка занято либо как собственный идентификатор, либо как прежнее имя
    # другого узла. Различать обязательно: это две разные ошибки, и чинятся они
    # по-разному — вторая означает, что канонизация мостов разошлась с графом.
    by_id: dict[str, tuple[str, str]] = {}

    def claim(name: str, note, kind: str) -> None:
        held = by_id.get(name)
        if held and held[0] != note.rel:
            where, how = held
            report.fail(note.rel, f"имя {name} уже занято: {where} держит его "
                                  f"{how}. Один навык не может жить двумя записями — "
                                  "глубина и доказательства разойдутся между ними")
        by_id[name] = (note.rel, kind)

    for note in skills:
        ident = str(note.data.get("skill_id") or "").strip()
        if ident:
            claim(ident, note, "как свой идентификатор")
        for alias in note.data.get("aliases") or []:
            claim(str(alias), note, "как прежнее имя")

        target = note.data.get("target")
        if not isinstance(target, int) or not 0 <= target <= 5:
            report.fail(note.rel, f"целевой уровень вне шкалы 0–5: {target!r}")
            target = None
        if note.status == "specified" and isinstance(target, int):
            described = {int(m) for m in LEVEL_HEAD.findall(body_text(note.path))}
            # У мета-узла нижних уровней нет по устройству: «узнать» или
            # «объяснить» способность решать целый класс задач отдельно от её
            # применения нельзя — её можно только показать в работе. Поэтому с
            # него спрашиваются уровни начиная с применения.
            first = 3 if str(note.data.get("node")) == "meta" else 1
            missing = [lvl for lvl in range(first, target + 1) if lvl not in described]
            if missing:
                report.fail(note.rel, "навык объявлен описанным, но уровни "
                                      f"{', '.join(map(str, missing))} не расписаны — "
                                      "такой уровень нельзя ни присвоить, ни оспорить")

    # Предпосылки: имя навыка и уровень, с которого он открывает следующий.
    # «После причинности» и «когда причинность применяется сама» — разные условия.
    known_ids = set(by_id)
    graph: dict[str, list[str]] = {}
    for note in skills:
        deps = note.data.get("requires")
        if deps is None:
            deps = {}
        if not isinstance(deps, dict):
            report.fail(note.rel, "предпосылки должны быть парами «навык: уровень», "
                                  "иначе теряется, на каком уровне навык открывает "
                                  "следующий")
            deps = {}
        graph[str(note.data.get("skill_id") or "")] = [str(k) for k in deps]
        for dep, level in deps.items():
            if str(dep) not in known_ids:
                report.fail(note.rel, f"предпосылка {dep} не найдена среди навыков")
            if not isinstance(level, int) or not 1 <= level <= 5:
                report.fail(note.rel, f"уровень предпосылки {dep} вне шкалы 1–5: {level!r}")
            # Первый уровень — «узнаёт», то есть слышал о понятии. Он ничего не
            # блокирует и не создаёт различимой разницы в результате: такое ребро
            # рисует строгий порядок обучения там, где его нет, и зашумляет расчёт
            # «что уже открыто». Пятнадцать таких рёбер жили в графе, пока их не
            # пересмотрели поимённо — часть оказалась связями наоборот.
            elif level == 1:
                report.fail(note.rel, f"предпосылка {dep} на уровне «узнаёт» — это не "
                                      "предпосылка: слышать о навыке не значит "
                                      "зависеть от него. Поднять до 2 или снять")

    # Петля: навык, который в конце концов требует сам себя, никогда не откроется.
    # Проверяется обходом, а не глазами: цепочка в три звена глазами не видна.
    for start in graph:
        seen, queue = set(), list(graph[start])
        while queue:
            step = queue.pop()
            if step == start:
                where = by_id.get(start, (start, ""))[0]
                report.fail(where, "предпосылки образуют петлю: навык требует сам себя")
                break
            if step in seen or step not in graph:
                continue
            seen.add(step)
            queue.extend(graph[step])

    # Дисциплина названа в карточке навыка ключом. Пока ключ ни на что не
    # указывает, его нельзя ни раскрыть человеку, ни проверить: опечатка в
    # `finanсe` тихо заводит тринадцатую дисциплину из одного навыка.
    fields = [n for n in notes if n.type == "domain"]
    report.covered("дисциплины", len(fields))
    by_key: dict[str, str] = {}
    for note in fields:
        ident = str(note.data.get("domain_id") or "").strip()
        if ident in by_key:
            report.fail(note.rel, f"дисциплина {ident} уже описана в {by_key[ident]}")
        by_key[ident] = note.rel
        order = note.data.get("order")
        if not isinstance(order, int) or order < 1:
            report.fail(note.rel, f"порядок дисциплины не число от единицы: {order!r}")

    if fields:
        named = set(by_key)
        for note in skills:
            for key in note.data.get("domains") or []:
                if str(key) not in named:
                    report.fail(note.rel, f"дисциплина {key} не описана — ключ есть, "
                                          "а карточки нет, и раскрыть его нечем")
        held = {str(k) for n in skills for k in (n.data.get("domains") or [])}
        for ident, rel in by_key.items():
            if ident not in held:
                report.notice(rel, "дисциплина не названа ни одним навыком: "
                                   "раздел без содержания")

    proofs = [n for n in notes if n.type == "evidence"]
    report.covered("доказательства", len(proofs))
    for note in proofs:
        level = note.data.get("level")
        if not isinstance(level, int) or not 1 <= level <= 5:
            report.fail(note.rel, f"уровень доказательства вне шкалы 1–5: {level!r}")
        origin = str(note.data.get("origin") or "")
        if origin and not origin.startswith(EVENT_FOLDERS):
            report.fail(note.rel, f"доказательство опирается не на событие: {origin} — "
                                  "владение подтверждает произошедшее, а не наша же оценка")
        # «Что сделано» одной фразой. Машина не отличит работу от намерения, но
        # человек, читающий «выбрал вариант Б» напротив навыка «разработка
        # обоснования», видит натяжку сразу. Три таких доказательства из девяти
        # прожили в складе двое суток, пока их не прочитали вслух.
        if not str(note.data.get("result") or "").strip():
            report.fail(note.rel, "не сказано, что именно сделано: без этого "
                                  "доказательство нельзя отличить от намерения")

    # Книга называет навыки, которых нет: связь литературы с развитием обязана
    # быть проверяемой, иначе разметка тихо указывает в пустоту.
    known = set(by_id)
    card_of = {str(n.data.get("skill_id")): n.path for n in skills}
    for note in notes:
        if note.type != "reading":
            continue
        marks = note.data.get("skills")
        if not isinstance(marks, dict):
            continue
        for ident, depth in marks.items():
            if str(ident) not in known:
                report.fail(note.rel, f"книга раскрывает навык {ident}, которого нет в графе")
            if not isinstance(depth, int) or not 1 <= depth <= 5:
                report.fail(note.rel, f"глубина по навыку {ident} вне шкалы 1–5: {depth!r}")
                continue
            # Глубина говорит, до какой ступени навыка книга доводит. Ступени
            # выше описанной у навыка нет — значит и довести до неё нельзя, и
            # такая глубина ничего не означает. Одиннадцать пар простояли с
            # глубиной 4 у навыков, расписанных до 3: покрытие считалось по
            # ступени, которой не существует.
            owner = card_of.get(str(ident))
            if owner is None:
                continue
            described = {int(m) for m in LEVEL_HEAD.findall(body_text(owner))}
            if described and depth > max(described):
                report.fail(note.rel, f"глубина {depth} по навыку {ident}, а его ступени "
                                      f"расписаны до {max(described)}: до ступени, которой "
                                      "нет, книга довести не может")


def check_repeats(root: Path, report: Report) -> None:
    """Один разговор — одна запись. Повтор не событие, а вторая копия события.

    Правила «только дописывание» мало, когда пишет программа: она может
    запуститься дважды. Первый внешний писатель прочитал «никогда не переписывай»
    буквально и на каждый повторный запуск заводил новый файл — двадцать четыре
    копии одного интервью. По букве он был прав; недостаёт было правила.

    Пара «кто прислал» и «какой это разговор» — `source` и `source_ref` — и есть
    признак разговора. Совпала у двух файлов, значит один из них лишний.
    """
    seen: dict[tuple[str, str], str] = {}
    events = store.load(root, "raw").notes
    report.covered("повторы событий", len(events))
    for note in events:
        who = str(note.data.get("source") or "").strip()
        which = str(note.data.get("source_ref") or "").strip()
        if not who or not which:
            continue
        key = (who, which)
        if key in seen:
            report.fail(note.rel, f"повтор события: тот же разговор уже записан в "
                                  f"{seen[key]} (source={who}, source_ref={which})")
        else:
            seen[key] = note.rel


def check_untracked(root: Path, report: Report, intake: dict | None = None) -> None:
    """На диске и в git должно быть одно и то же — иначе клон получит другое дерево.

    Исключение — сырьё, которое только что пришло. Коннектор пишет непрерывно, и
    файл, появившийся минуту назад, ещё не дефект склада, а файл в пути: гейт,
    краснеющий от чужого расписания, перестают читать. Исключение не бесплатное:
    очередь на приём считается отдельно и стареет, так что «в пути» не может
    длиться вечно.
    """
    result = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode:
        # Отказ git — не «расхождений нет». Молчание здесь означало бы зелёный
        # гейт ровно там, где проверка не выполнялась.
        report.notice("git", "расхождение диска и git не проверено: git недоступен")
        return
    report.covered("диск против git", 1)
    arriving: list[str] = []
    for line in result.stdout.splitlines():
        if not (line.startswith("??") and line.strip().endswith(".md")):
            continue
        rel = line[3:].strip()
        data = None
        with contextlib.suppress(OSError, UnicodeDecodeError):
            data, _ = store.parse((root / rel).read_text(encoding="utf-8"))
        if data and is_capture(rel, data, intake or {}):
            arriving.append(rel)
        else:
            report.fail(rel, "файл есть на диске, но не в git")
    if arriving:
        report.notice("raw", f"сырьё ещё не в git: {len(arriving)} записей, только что "
                             f"пришли. Дефектом станут, если останутся так")


def check_intake(root: Path, report: Report, intake: dict) -> None:
    """Очередь на разбор — вслух и с возрастом.

    Мягкая мерка без счётчика превратилась бы в постоянное освобождение: сырьё
    копилось бы годами и выглядело как порядок. Поэтому очередь называется
    числом, а старейшая запись — датой.
    """
    # Зону приёма разбирает сам is_capture: место — единственный признак сырья,
    # и знать его должен один код, а не каждый вызывающий по своей копии.
    captures = 0
    for note in store.load(root, "raw").notes:
        rel, data = note.rel, note.data
        if not is_capture(rel, data, intake):
            continue
        captures += 1
    report.covered("сырьё в приёме", captures)

    # Разобранность определяется следом в work/, как и в живом входе. Поля
    # сырья после приёма не дописываются: raw неизменяем, поэтому отсутствие
    # container/person не может навсегда оставлять уже разобранную запись в долге.
    waiting, _ = welcome.unprocessed(root)
    if waiting:
        rels = [str(path.relative_to(root)) for path in waiting]
        oldest = min(rels)
        report.notice("raw", f"ждут приёма: {len(rels)} записей, старейшая {oldest}. "
                             "Пока не разобраны, в экран внимания и в досье они не попадают")


def check_mirror(root: Path, report: Report) -> None:
    """Поле mirror — указатель на живую копию во внешней системе.

    Заведено, когда рабочим местом по процессам Альфа стал Notion. Без поля пришлось
    бы либо держать в складе вторую копию описания, либо прятать адрес в тексте,
    где его не проверить.

    Две проверки, и обе про одно: указатель обязан работать.
    Первая — адрес внешний и целый, а не путь и не пустая строка.
    Вторая — адрес назван в теле записи. Поле, которого читатель не видит,
    указывает в никуда так же надёжно, как битая ссылка: человек прочтёт заметку,
    не узнает про живую версию и станет править устаревшее.
    """
    seen = 0
    for sort in ("work", "wiki", "raw"):
        for note in store.load(root, sort).notes:
            value = note.data.get("mirror")
            if value is None:
                continue
            seen += 1
            where = note.rel
            if not isinstance(value, str) or not value.startswith("https://"):
                report.fail(where, f"mirror не внешний адрес: {value!r}")
                continue
            # Именно тело, не весь файл: в шапке адрес есть всегда, и проверка
            # по целому файлу срабатывала бы сама на себе — вакуумный гейт.
            raw = (root / where).read_text(encoding="utf-8")
            body = raw.split("---", 2)[2] if raw.startswith("---") else raw
            if value not in body:
                report.fail(where, "mirror не назван в теле — читатель не узнает "
                                   "про живую версию и будет править устаревшее")
    report.covered("указатели на живые копии", seen)


MESSENGERS = {"telegram", "instagram", "whatsapp", "signal", "vk", "max"}
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_DIGITS = (10, 15)         # от номера без кода страны до самого длинного международного


def _digits(value: str) -> str:
    """Только цифры: «+7 (988) 444-44-07» и «89884444407» — один и тот же телефон."""
    return re.sub(r"\D", "", value)


def check_people(report: Report, notes: list) -> None:
    """Контакты человека: адрес обязан быть адресом и принадлежать одному человеку.

    Заведены 9 августа 2026. До этого у карточки человека полей для связи не было
    вовсе, и телефон, названный в письме, жил в складе единственным способом — внутри
    текста самого письма. Следствие дороже неудобства: разбор почты не может узнать
    отправителя, если у людей нет адресов, а «письмо от того, кого ты ждёшь» — весь
    смысл разбора.

    Проверяется то, что проверяемо машиной. Верен ли номер, она знать не может — но может
    сказать, что в нём семь цифр вместо одиннадцати, что почта без собаки почтой не
    является и что один и тот же адрес стоит у двух разных людей. Последнее почти
    всегда значит одно из двух: человек заведён дважды или адрес приписан не тому.
    """
    people = [note for note in notes if note.type == "person"]
    report.covered("люди", len(people))
    owners: dict[tuple[str, str], str] = {}
    for note in people:
        for value in note.data.get("email") or []:
            if not isinstance(value, str) or not EMAIL.match(value.strip()):
                report.fail(note.rel, f"не похоже на адрес почты: {value!r}")
                continue
            key = ("почта", value.strip().lower())
            if key in owners and owners[key] != note.rel:
                report.fail(note.rel, f"тот же адрес {value} стоит у {owners[key]} — "
                                      "либо это один человек, заведённый дважды, "
                                      "либо адрес приписан не тому")
            owners.setdefault(key, note.rel)

        for value in note.data.get("phone") or []:
            digits = _digits(str(value))
            if not PHONE_DIGITS[0] <= len(digits) <= PHONE_DIGITS[1]:
                report.fail(note.rel, f"не похоже на номер телефона: {value!r} — "
                                      f"{len(digits)} цифр")
                continue
            # Сравниваем по последним десяти: 8-988 и +7-988 — один номер
            key = ("телефон", digits[-10:])
            if key in owners and owners[key] != note.rel:
                report.fail(note.rel, f"тот же номер {value} стоит у {owners[key]} — "
                                      "либо это один человек, заведённый дважды, "
                                      "либо номер приписан не тому")
            owners.setdefault(key, note.rel)

        messengers = note.data.get("messengers")
        if messengers is not None:
            if not isinstance(messengers, dict):
                report.fail(note.rel, "messengers обязано быть словарём «сеть: имя»: "
                                      "иначе непонятно, где именно писать")
            else:
                for network, handle in messengers.items():
                    if not isinstance(handle, str) or not handle.strip():
                        report.fail(note.rel, f"мессенджер {network} без имени — "
                                              "поле, по которому нельзя написать")
                    elif network not in MESSENGERS:
                        # Замечание, не нарушение: список сетей открытый по природе
                        report.notice(note.rel, f"мессенджер вне словаря: {network}. "
                                                f"Известные: {', '.join(sorted(MESSENGERS))}")

        contact = note.data.get("google_contact")
        if contact is not None and (not isinstance(contact, str)
                                    or not contact.startswith("people/")):
            report.fail(note.rel, f"google_contact не ссылается на запись адресной книги: "
                                  f"{contact!r} — ожидается вид people/c123")


def check_log_events(root: Path, report: Report) -> None:
    """Опечатка в имени события не имеет права исчезнуть у части читателей."""
    for event in activity.unknown_events(root):
        report.notice("raw/log.md", f"событие журнала вне словаря: «{event}»")


def load_config(root: Path) -> dict:
    path = root / "config" / "attention.yml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def run(root: Path) -> Report:
    report = Report()
    report.covered("файлы вне трёх корней", 0)   # ключ заводится до обхода: ноль
    report.covered("сырьё в приёме", 0)          # осмотренных — это факт, а не тишина
    config = load_config(root)
    intake = config.get("intake") or {}
    keys: dict[str, str] = {}
    for path in sorted(root.rglob("*.md")):
        relative = path.relative_to(root)
        if ".git" in relative.parts:
            continue
        # Чужой код, скачанный инструментами: тысячи README, к складу отношения
        # не имеющие. Без этого одна папка с библиотеками валит весь гейт.
        # Виртуальное окружение python — тот же случай: ставится рядом со
        # складом при первой установке и приносит чужие лицензии в markdown.
        if VENDOR_DIRS & set(relative.parts):
            continue
        # Архив сохраняется как было: чужая схема — не наш контракт
        if relative.parts[:2] == ("work", "archive"):
            continue
        if relative.parts[0] in SORTS:
            check_store_file(root, path, report, keys, intake)
        else:
            # Контракт и документы не следуют схеме склада, но их ссылки обязаны вести куда-то
            report.covered("файлы вне трёх корней", 1)
            check_outside_roots(root, path, report)
            check_links(root, path, path.read_text(encoding="utf-8"), report)
    check_untracked(root, report, intake)
    loaded = store.load(root, "work")
    for rel, why in loaded.unreadable:
        report.fail(rel, why)
    notes = loaded.notes
    check_stale(root, report, notes)
    check_commitments(report, notes)
    check_evidence(report, notes)
    check_answered_questions(report, notes)
    check_raw_immutability(root, report)
    check_intake(root, report, intake)
    check_mirror(root, report)
    check_people(report, notes)
    check_repeats(root, report)
    check_duplicates(report, notes, config)
    check_reading(report, notes, config)
    check_skills(report, notes)
    check_ideas(report, notes, config)
    check_archive(root, report)
    check_log_events(root, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Проверка склада")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    report = run(args.root.resolve())

    for where, what in report.problems:
        print(f"✗ {where}\n    {what}")
    if not args.quiet:
        for where, what in report.notices:
            print(f"· {where}\n    {what}")

    if report.checked == 0:
        print("\nЛИНТ: ВАКУУМ — проверено 0 файлов. Это провал, а не успех.")
        return 1
    if report.problems:
        print(f"\nЛИНТ: {len(report.problems)} нарушений в {report.checked} файлах")
        return 1
    tail = f", замечаний {len(report.notices)}" if report.notices else ""
    print(
        f"ЛИНТ: чисто — {report.checked} файлов, {report.links_checked} ссылок{tail}"
    )
    # Без этой строки «чисто» не отличить от «проверка не нашла чего проверять».
    # Общий страж ловит только полное отсутствие файлов; ноль объектов у
    # отдельной проверки он пропускал молча.
    if report.coverage:
        parts = [f"{name} {count}" + (" — НЕПРИМЕНИМО" if count == 0 else "")
                 for name, count in sorted(report.coverage.items())]
        print("  покрытие: " + ", ".join(parts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
