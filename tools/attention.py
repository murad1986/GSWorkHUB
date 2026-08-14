#!/usr/bin/env python3
"""Сборщик экрана внимания.

Вид, а не документ: читает `work/`, применяет правила из `config/attention.yml`
и перезаписывает `wiki/attention.md` целиком. Своего состояния не хранит —
поэтому не может разойтись с реальностью.

Правила — гипотеза. Они снаружи, в конфиге. Сборка пишет только факт вычисления
вида; показ человеку фиксирует живой вход `today`, иначе технический запуск
притворялся бы использованием.

Четыре класса сигналов, в этом же порядке приоритета:
  1. обещано человеку — срок прошёл или близко, либо чужое обещание без срока;
  2. застряло — не двигалось дольше своего порога;
  3. ждёт человека — позиция в статусе waiting;
  4. система противоречит себе — закрыто без результата, заблокировано, пусто.
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
from dataclasses import dataclass, field
from pathlib import Path

import activity
import welcome
import yaml
from store import DATE, Note, load

SORT = "work"
UNRESOLVED = {"open", "in-progress", "waiting"}


@dataclass
class Line:
    rank: int          # класс сигнала: чем меньше, тем выше
    weight: int        # внутри класса: чем больше, тем выше
    text: str
    kind: str
    target: str = ""        # на что указывает сигнал — нужно для самоанализа
    extras: list[str] = field(default_factory=list)
    demoted: bool = False   # срезано лимитом класса: в экран не попадёт, но в «ещё N» — да


def as_date(value: str) -> dt.date | None:
    r"""Дата или None. Форма `\d{4}-\d{2}-\d{2}` ещё не значит существующий день:
    `2026-02-31` проходит регулярное выражение и роняет fromisoformat."""
    match = DATE.search(value)
    if not match:
        return None
    try:
        return dt.date.fromisoformat(match.group(0))
    except ValueError:
        return None


def age_days(note: Note, today: dt.date, notes_by_rel: dict[str, Note]) -> int | None:
    """Возраст от события, а не от файла: перенос склада не должен обнулять его."""
    for field_name in ("opened", "created", "date"):
        value = note.data.get(field_name)
        if value:
            when = as_date(str(value))
            if when:
                return (today - when).days
    origin = note.data.get("origin")
    if isinstance(origin, str) and origin in notes_by_rel:
        return age_days(notes_by_rel[origin], today, {})
    return None


def parse_due(note: Note) -> dt.date | None:
    return as_date(str(note.data.get("due") or ""))


QUARTER = re.compile(r"^(\d{4})-Q([1-4])$")
QUARTER_ENDS = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}


def horizon_end(value: str) -> dt.date | None:
    """Последний день горизонта: `2026-Q3` → 30 сентября. Иначе прямая дата."""
    match = QUARTER.match(value.strip())
    if match:
        month, day = QUARTER_ENDS[int(match.group(2))]
        return dt.date(int(match.group(1)), month, day)
    return as_date(value)


def night_log_entries(entries: list[activity.Entry], conf: dict,
                      now: dt.datetime) -> list[dt.datetime]:
    """Записи журнала, сделанные ночью в недавнем окне.

    Ночь — интервал через полночь: [start, 24:00) ∪ [00:00, end). Строки без
    времени пропускаются: у них нечего проверять. Технические строки из
    ignore_markers не считаются — сборка по расписанию не ночная работа."""
    try:
        start = dt.time.fromisoformat(str(conf.get("start", "23:00")))
        end = dt.time.fromisoformat(str(conf.get("end", "06:00")))
    except ValueError:
        return []
    lookback = dt.timedelta(hours=float(conf.get("lookback_hours", 24)))
    ignore = conf.get("ignore_markers")
    if ignore is None:
        ignore = ["экран собран"]
    out: list[dt.datetime] = []
    for entry in entries:
        when = entry.stamp
        if not (now - lookback <= when <= now):
            continue
        moment = when.time()
        at_night = (moment >= start or moment < end) if start > end \
            else (start <= moment < end)
        if at_night and entry.event not in ignore:
            out.append(when)
    return out


def outbound_recent(raw: list[Note], entries: list[activity.Entry],
                    conf: dict, today: dt.date) -> bool:
    """Было ли за окно хоть одно событие наружу: встреча/интервью в raw/ или
    строка журнала с внешним глаголом. Дата строки берётся из её начала, а не
    из всего текста — в теле строки живут пути с датами."""
    since = today - dt.timedelta(days=int(conf.get("window_days", 7)))
    for note in raw:
        if note.type in {"meeting", "interview"}:
            when = as_date(str(note.data.get("date") or ""))
            if when and when >= since:
                return True
    markers = [str(m) for m in (conf.get("log_markers") or [])]
    for entry in entries:
        if entry.stamp.date() >= since and entry.event in markers:
            return True
    return False


def pending_captures(raw: list[Note], config: dict,
                     pending_rels: set[str] | None = None) -> list[Note]:
    """Сырьё, не прошедшее приём: у него нет полей, которые проставляет разбор."""
    intake = config.get("intake") or {}
    zone = str(intake.get("zone") or "raw/inbox").rstrip("/") + "/"
    connectors = set(intake.get("connectors") or [])
    by_intake = {"meeting": ["container"], "interview": ["person", "mode"]}
    out: list[Note] = []
    for note in raw:
        if Path(note.rel).name in {"index.md", "log.md"}:
            continue
        if not (note.rel.startswith(zone) or str(note.data.get("source") or "") in connectors):
            continue
        if pending_rels is not None and note.rel not in pending_rels:
            continue
        if any(f not in note.data for f in by_intake.get(note.type, [])):
            out.append(note)
    return out


def build_lines(notes: list[Note], config: dict, today: dt.date,
                raw: list[Note] | None = None,
                pending_rels: set[str] | None = None,
                log_entries: list[activity.Entry] | None = None,
                now: dt.datetime | None = None) -> list[Line]:
    by_rel = {n.rel: n for n in notes}
    aging = set(config.get("aging_modes") or ["active"])
    stale = config.get("stale_days") or {}
    lines: list[Line] = []

    # 0. Отложенное до названного дня. Человек сказал «вернись в понедельник» —
    # значит до понедельника строки быть не должно вовсе, иначе «отложил» не
    # отличается от «промолчал» и экран продолжает упрекать тем же самым.
    # В назначенный день позиция возвращается одной строкой — но только если её
    # к тому времени не взяли в работу: начатое видно и без напоминания.
    snoozed: set[str] = set()
    for note in notes:
        review = note.date_field("review")
        if review is None or note.status not in UNRESOLVED:
            continue
        if review > today:
            snoozed.add(note.rel)
        elif note.status != "in-progress":
            overdue = (today - review).days
            when = "сегодня" if overdue == 0 else f"{overdue} дн. назад"
            lines.append(Line(1, 90 + overdue,
                              f"{note.container} · {note.title} — "
                              f"откладывал до {review:%d.%m}, срок вернуться {when}",
                              "вернуться", note.rel))
            # Строка возврата уже говорит и про дело, и про срок: вторая строка
            # о просрочке была бы тем же делом дважды.
            snoozed.add(note.rel)

    # 1. Обещано человеку
    for note in notes:
        if note.type != "commitment" or note.status not in UNRESOLVED:
            continue
        if note.rel in snoozed:
            continue
        due = parse_due(note)
        who = "мне обещали" if note.data.get("direction") == "inbound" else "я обещал"
        if due and due <= today:
            overdue = (today - due).days
            when = "срок сегодня" if overdue == 0 else f"просрочено на {overdue} дн."
            lines.append(Line(1, 100 + overdue, f"{note.container} · {note.title} — {when} ({who})", "обещание", note.rel))
        elif due and (due - today).days <= int(config.get("due_soon_days", 3)):
            lines.append(Line(1, 50, f"{note.container} · {note.title} — срок {due:%d.%m} ({who})", "обещание", note.rel))
        elif not due and note.data.get("direction") == "inbound" \
                and note.status != "waiting":
            # Ожидающее уже показывается строкой «ждёт ответа»: одно дело —
            # одна строка, иначе семь мест экрана тратятся на дубли
            age = age_days(note, today, by_rel)
            tail = f", висит {age} дн." if age else ""
            lines.append(Line(1, 40, f"{note.container} · {note.title} — чужое обещание без срока{tail}", "обещание", note.rel))

    # 2. Застряло
    stuck: list[Line] = []
    for note in notes:
        if note.type not in stale or note.status not in UNRESOLVED | {"draft"}:
            continue
        if note.rel in snoozed:
            continue
        if note.mode and note.mode not in aging:
            continue
        age = age_days(note, today, by_rel)
        if age is None or age < int(stale[note.type]):
            continue
        stuck.append(Line(2, age, f"{note.container} · {note.title} — без движения {age} дн.", "застряло", note.rel))

    # 2б. Цель, к которой не привязано ни одного живого обязательства.
    # Цель без шагов — это намерение: она не двигается и не может, потому что
    # двигать её нечем. Отдельный класс от «застряло» не нужен — лечится тем же:
    # либо появляется шаг, либо цель снимается.
    stepped = {str(n.data.get("goal") or "") for n in notes
               if n.type == "commitment" and n.status in UNRESOLVED}
    for note in notes:
        if note.type != "goal" or note.status != "active" or note.rel in stepped:
            continue
        if note.mode and note.mode not in aging:
            continue
        age = age_days(note, today, by_rel)
        if age is None or age < int(stale.get("goal", 30)):
            continue
        stuck.append(Line(2, age, f"{note.container} · {note.title} — цель без единого шага "
                                  f"{age} дн.", "цель без шагов", note.rel))
    # 2г. Начатая книга, которая не двигается. Очередь при этом молчит совсем:
    # полка имеет право лежать годами, и старение полки — это ежедневный упрёк
    # за то, что человек не всесилен. А вот начатое и брошенное чтение — ровно та
    # же незавершёнка, что начатая и брошенная работа: место занято, знания нет.
    reading_conf = config.get("reading") or {}
    for note in notes:
        if note.type != "reading" or note.status != "reading":
            continue
        started = note.date_field("started")
        if started is None:
            continue
        age = (today - started).days
        if age < int(stale.get("reading", 30)):
            continue
        stuck.append(Line(2, age, f"Чтение · {note.title} — начата {age} дн. назад "
                                  "и не двигается", "чтение", note.rel))

    # Свой лимит на класс, а не общий. Общий означал, что три старые чужие спеки
    # выдавливают личные цели полностью — при том, что «Я» объявлен контейнером
    # высшего приоритета. И отсечённое раньше исчезало совсем: срез делался до
    # подсчёта «ещё N», поэтому вытесненного не было ни в экране, ни в счётчике.
    # У чтения потолок свой и низкий: книги важны, но никому не обещаны, и
    # выдавить ими обещание человеку значит поменять местами долг и намерение.
    for kind, cap in (("застряло", int(config.get("stuck_top", 5))),
                      ("цель без шагов", int(config.get("goal_top", 3))),
                      ("чтение", int(reading_conf.get("top", 2)))):
        same = sorted([one for one in stuck if one.kind == kind], key=lambda line: -line.weight)
        for i, line in enumerate(same):
            line.demoted = i >= cap
        lines.extend(same)

    # 2в. Сырьё ждёт разбора. Одной строкой, а не двадцатью тремя: пока цена
    # разбора не заплачена, ни одна из этих записей не видна ни экрану, ни досье —
    # но лечится это одним действием, значит и строка одна.
    waiting = pending_captures(raw or [], config, pending_rels)
    if waiting:
        ages = [a for a in (age_days(n, today, {}) for n in waiting) if a is not None]
        oldest = max(ages, default=0)
        if oldest >= int((config.get("intake") or {}).get("pending_days", 3)):
            tail = f", старейшей {oldest} дн." if oldest else ""
            lines.append(Line(2, 1000, f"Приём · {len(waiting)} записей ждут разбора{tail} — "
                                       "до разбора их не видит ни экран, ни досье",
                              "ждёт разбора", "raw"))

    # 3. Ждёт человека
    # Строка называет, чьего ответа ждут: «ждёт ответа» без имени читается как
    # «ждёт ответа от тебя» — так и было прочитано 30 июля, и доклад разошёлся
    # со складом, где направление и собеседник записаны верно.
    for note in notes:
        if note.status == "waiting" and note.rel not in snoozed:
            # Имя ставится через двоеточие, а не «от <имени>»: склонять русские
            # имена в коде — значит хранить падежи в карточках людей, то есть
            # расширять склад ради одной строки экрана.
            whom = ""
            if note.data.get("direction") == "inbound":
                other = by_rel.get(str(note.data.get("counterpart") or ""))
                whom = f": {other.title}" if other else ""
            lines.append(Line(3, 10, f"{note.container} · {note.title} — ждёт ответа{whom}",
                              "ждёт", note.rel))

    # 3б. Профиль устарел относительно накопленных отказов. Профиль — рабочие
    # гипотезы о человеке, и калибруют их его поправки (§9а контракта). Пока
    # пересмотр не привязан ни к чему, файл живёт вечно: он обновится, только
    # если человек сам принесёт новые данные. Считаем не дни, а поводы: сигнал
    # появляется, когда с последнего пересмотра накопилось достаточно отказов —
    # то есть система успела ошибиться о человеке столько раз, что пора
    # переписать её представление о нём.
    profile_conf = config.get("profile") or {}
    profile = by_rel.get(str(profile_conf.get("path") or "work/me/profil.md"))
    if profile is not None and log_entries is not None:
        since = profile.date_field("reviewed", "date")
        refusals = 0
        for entry in log_entries:
            if entry.event != "реакция" or len(entry.parts) < 2:
                continue
            if entry.part(0) not in {"отклонено", "поправлено", "отменено"}:
                continue
            if since is None or entry.stamp.date() > since:
                refusals += 1
        need = int(profile_conf.get("refusals_before_review", 5))
        if refusals >= need:
            when = f" (последний пересмотр {since:%d.%m})" if since else ""
            lines.append(Line(3, 5, f"Я · Профиль устарел: {refusals} поправок с "
                                    f"последнего пересмотра{when} — пора переписать, "
                                    "во что система про тебя верит",
                              "профиль", profile.rel))

    # 4. Система противоречит себе
    for note in notes:
        if note.status in {"resolved", "cancelled"} and not note.data.get("resolution"):
            lines.append(Line(4, 30, f"{note.rel} — закрыто без ссылки на результат", "целостность", note.rel))
        for blocked in note.data.get("blocks") or []:
            target = by_rel.get(blocked)
            if note.status in UNRESOLVED and target and target.status in UNRESOLVED:
                lines.append(Line(4, 20, f"{note.container} · «{target.title}» заблокировано вопросом «{note.title}»", "целостность", target.rel))
    # Горизонт закрылся, а цель всё ещё активна: календарь и статус спорят.
    # Молча продлевать нельзя — цель либо достигнута, либо снята, либо переносится
    # на следующий горизонт, и это три разных решения человека.
    for note in notes:
        if note.type != "goal" or note.status != "active":
            continue
        end = horizon_end(str(note.data.get("horizon") or ""))
        if end and end < today:
            lines.append(Line(4, 35, f"{note.container} · {note.title} — горизонт "
                                     f"{note.data.get('horizon')} закрылся {end:%d.%m}, "
                                     "а цель всё ещё активна", "целостность", note.rel))

    # Начато больше книг, чем помещается. Тот же приём, что лимит незавершённого
    # в работе, и по той же причине: пять начатых книг означают, что не читается
    # ни одна. Строка одна на всё превышение — лечится оно одним решением.
    in_reading = [n for n in notes if n.type == "reading" and n.status == "reading"]
    wip = int(reading_conf.get("wip_limit", 2))
    if len(in_reading) > wip:
        names = ", ".join(sorted(n.title for n in in_reading))
        lines.append(Line(4, 28, f"Чтение · начато {len(in_reading)} книг при пределе "
                                 f"{wip} — {names}", "целостность", "work/me/reading"))

    containers = [n for n in notes if n.type in {"client", "program", "self"}]
    for holder in containers:
        if str(holder.data.get("mode")) not in aging:
            continue
        folder = str(Path(holder.rel).parent)
        inside = [n for n in notes if n.rel.startswith(folder + "/")]
        questions = [n for n in inside if n.type == "question" and n.status in UNRESOLVED]
        commitments = [n for n in inside if n.type == "commitment"]
        if questions and not commitments:
            lines.append(Line(4, 25, f"{holder.title} · {len(questions)} вопросов и ни одного обязательства — никто не обязался ответить", "целостность", holder.rel))

    # 2е. Решение пересиживает свой вес. Стиль решений по профилю
    # (work/me/profil.md) — зависание: проверка лучшего варианта не кончается
    # сама, её кончает срок. Вес решения задаёт срок; просроченное поднимается.
    dec_conf = config.get("decisions") or {}
    if dec_conf:
        boxes = dec_conf.get("timebox_days") or {}
        default_weight = str(dec_conf.get("default_weight", "medium"))
        for note in notes:
            if note.type != "decision" or note.status != "proposed":
                continue
            if note.mode and note.mode not in aging:
                continue
            weight = str(note.data.get("weight") or default_weight)
            box = boxes.get(weight)
            age = age_days(note, today, by_rel)
            if box is None or age is None or age <= int(box):
                continue
            lines.append(Line(2, 800 + age, f"{note.container} · {note.title} — решение "
                                            f"висит {age} дн. при сроке {box} для веса "
                                            f"«{weight}»: пора решать", "решение", note.rel))

    # 2ж. Анализ копится без действия. «Умная прокрастинация» из профиля:
    # по контейнеру прибавляются вопросы и разборы, а дела не закрываются.
    # Наблюдение с вопросом, не вердикт — судит человек.
    drift_conf = config.get("analysis_drift") or {}
    if drift_conf:
        window = int(drift_conf.get("window_days", 14))
        min_new = int(drift_conf.get("min_new_analysis", 3))
        for holder in notes:
            if holder.type not in {"client", "program", "self"} \
                    or str(holder.data.get("mode")) != "active":
                continue
            folder = str(Path(holder.rel).parent)
            inside = [n for n in notes if n.rel.startswith(folder + "/")]
            fresh = [n for n in inside
                     if n.type in {"question", "digest", "hypothesis", "risk"}
                     and (lambda a: a is not None and a <= window)(age_days(n, today, by_rel))]
            closed = [n for n in inside if n.type == "commitment"
                      and (d := as_date(str(n.data.get("resolved") or ""))) is not None
                      and (today - d).days <= window]
            # Свежее событие контейнера в мире — встреча, интервью, публикация —
            # тоже действие: вопросы из живой сессии с клиентом — работа, а не
            # зависание. Без этого сигнал ловил бы фазу разбора любого проекта.
            world = [n for n in (raw or [])
                     if str(n.data.get("container") or "") == folder
                     and (d := as_date(str(n.data.get("date") or ""))) is not None
                     and (today - d).days <= window]
            if len(fresh) >= min_new and not closed and not world:
                lines.append(Line(2, 700, f"{holder.title} · за {window} дн. — "
                                          f"{len(fresh)} вопросов и разборов, ни одного "
                                          "закрытого дела: анализ без действия?",
                                  "анализ", holder.rel))

    # 2д. Наружу ничего не выходило. Слабейшая зона профиля (work/me/profil.md) —
    # вывод результата в мир: «сделано, но не оформлено» невидимо для клиента и
    # рынка. Окно без единой встречи, отправки или публикации — сигнал, что
    # работа копится в столе. Одной строкой: лечится одним внешним действием.
    outbound_conf = config.get("outbound") or {}
    if log_entries is not None and outbound_conf \
            and not outbound_recent(raw or [], log_entries, outbound_conf, today):
        window = int(outbound_conf.get("window_days", 7))
        lines.append(Line(2, 950, f"Наружу · за {window} дней ни встречи, ни отправки, "
                                  "ни публикации — работа копится в столе",
                          "наружу", "raw/log.md"))

    # 4п. Перегрев контуров. Профиль: пять активных проектов — не
    # мультизадачность, а перегрев. Строка одна на всё превышение: лечится
    # одним решением — что-то заморозить.
    tracker_conf = config.get("tracker") or {}
    max_active = tracker_conf.get("max_active_containers")
    if max_active is not None:
        live = [n for n in notes if n.type in {"client", "program", "self"}
                and str(n.data.get("mode")) == "active"]
        if len(live) > int(max_active):
            names = ", ".join(sorted(n.title for n in live))
            lines.append(Line(4, 26, f"Живых контуров {len(live)} при пределе {max_active} "
                                     f"({names}) — это перегрев, не мультизадачность: "
                                     "что замораживаем?", "перегрев", "work"))

    # 4н. Сделанное ночью. Из профиля: ночью тревожное возбуждение выдаёт себя
    # за ясность. Ночная работа не отменяется и не осуждается — она помечается
    # на утренний пересмотр, одной строкой на всю ночь.
    night_conf = config.get("night") or {}
    if log_entries is not None and now is not None and night_conf:
        night_entries = night_log_entries(log_entries, night_conf, now)
        if night_entries:
            last = max(night_entries)
            lines.append(Line(4, 27, f"Ночью {len(night_entries)} записей в журнале, последняя "
                                     f"{last:%H:%M} — принятое ночью пересмотреть утром",
                              "ночное", "raw/log.md"))

    lines.sort(key=lambda line: (line.demoted, line.rank, -line.weight))
    return lines


def render(lines: list[Line], config: dict, now: dt.datetime, total_notes: int) -> str:
    limit = int(config.get("limit", 7))
    shown, hidden = lines[:limit], lines[limit:]
    body = "\n".join(f"{i}. {line.text}" for i, line in enumerate(shown, 1)) or "— пусто: ни один сигнал не сработал."
    tail = f"\n\nЕщё {len(hidden)}: " + ", ".join(sorted({line.kind for line in hidden})) if hidden else ""
    return f"""---
type: attention
generated: true
generated_at: {now:%Y-%m-%dT%H:%M}
signals: {len(lines)}
---

# Требует внимания

{body}{tail}

---

Собрано из {total_notes} позиций по правилам `config/attention.yml`. Правила —
гипотеза: меняются настройкой конфига. Файл перезаписывается целиком, править
руками бесполезно.
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Сборка экрана внимания")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--today", type=dt.date.fromisoformat, default=None,
                        help="дата отбора; по умолчанию сегодня")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    root = args.root.resolve()

    config = yaml.safe_load((root / "config" / "attention.yml").read_text(encoding="utf-8")) or {}
    store = load(root, SORT)
    notes = store.notes
    # Молчание о непрочитанном — тот самый отказ: обязательство есть, экран пуст
    complaint = store.complain()
    if complaint:
        print(complaint)
    # Реальное время сборки, а не выдуманное: самоанализ сопоставляет показы с
    # правками, и фиксированные «08:30» делали утренний коммит «действием после
    # сигнала», хотя он был до него.
    now = dt.datetime.now().replace(microsecond=0)  # с секундами: коммит внутри минуты различим
    today = args.today or now.date()
    if args.today:
        now = dt.datetime.combine(args.today, now.time())
    pending, _ = welcome.unprocessed(root)
    pending_rels = {str(path.relative_to(root)) for path in pending}
    log_entries = activity.read(root)
    lines = build_lines(notes, config, today, load(root, "raw").notes, pending_rels,
                        log_entries=log_entries, now=now)
    content = render(lines, config, now, len(notes))

    if args.dry_run:
        print(content)
        return 0

    (root / "wiki" / "attention.md").write_text(content, encoding="utf-8")
    activity.append(root, ["экран собран", f"сигналов {len(lines)}"], now=now)
    print(f"экран внимания: {len(lines)} сигналов из {len(notes)} позиций")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
