#!/usr/bin/env python3
"""Одна сделанная работа кормит несколько контуров.

Закрытое обязательство — это не только «дело сделано». Та же работа обычно
является ещё и доказательством навыка, и материалом для внешнего разговора.
Раньше эти контуры жили отдельно: `finish` закрывал дело, доказательства
заводились руками и потому почти не заводились (шесть на восемьдесят четыре
навыка), а витрина собиралась раз в неделю по журналу.

```
                     finish
                        │
              результат действительно есть
                        │
        ┌───────────────┼───────────────┐
        ↓               ↓               ↓
    дело закрыто   доказательство    наружу
                        │               │
                        ↓               ↓
                  карта развития   замысел материала
```

**Модуль ничего не пишет.** Он показывает кандидатов и объясняет, почему они
кандидаты; заводит их человек. Доказательство навыка — суждение о собственном
уровне, а замысел материала — решение говорить публично; ни то, ни другое
система за человека не решает.

**Молчание — нормальный исход.** Спрашивать после каждого закрытия «а чем это
ещё является» значит превратить закрытие дела в анкету, и человек перестанет
закрывать дела. Поэтому навык предлагается только при совпадении слов, а
материал — только когда результат действительно вышел наружу.
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
from dataclasses import dataclass, field
from pathlib import Path

import store

STOP = {"для", "как", "что", "или", "это", "при", "над", "под", "без", "его",
        "the", "and", "for", "with", "процесс", "процессы", "сделать", "работа"}
OUTSIDE = ("raw/meetings/", "raw/interviews/", "raw/sources/")


@dataclass
class SkillHit:
    skill: store.Note
    hits: set[str]
    proved_level: int
    target: int

    @property
    def gap(self) -> int:
        return max(0, self.target - self.proved_level)


@dataclass
class Harvest:
    note: store.Note
    skills: list[SkillHit] = field(default_factory=list)
    outward: str = ""          # чем работа вышла наружу, если вышла
    already: list[str] = field(default_factory=list)   # уже заведённые доказательства

    @property
    def empty(self) -> bool:
        return not self.skills and not self.outward


ENDING = re.compile(r"(ами|ями|ах|ях|ов|ев|ый|ий|ая|ое|ые|ых|ам|ям|ом|ем|"
                    r"у|ю|а|я|ы|и|е|о|ь|й)$")


def stem(word: str) -> str:
    """Грубая обрезка окончания.

    «Построил карту процесса» и «Карта процесса» — одна работа и один навык, но
    как строки не совпадают ни одним словом. Морфологию сюда тащить не за чем:
    достаточно отбросить окончание, чтобы «карту» и «карта» стали одним корнем.
    """
    return ENDING.sub("", word) if len(word) > 4 else word


def words(value: str) -> set[str]:
    return {stem(w) for w in re.findall(r"\w+", value.lower())
            if len(w) > 3 and w not in STOP}


def russian_name(note: store.Note) -> str:
    """Заголовок карточки навыка.

    В шапке навыка `title` английский («Process Mapping»), а работа описана
    по-русски — сопоставление по шапке не совпало бы ни разу. Русское имя стоит
    первым заголовком тела: «# BP02. Карта процесса».
    """
    try:
        text = note.path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    match = re.search(r"^#\s+(?:[A-Z]+\d+\.\s*)?(.+)$", text, re.MULTILINE)
    return match.group(1).strip() if match else ""


def proved_levels(notes: list[store.Note]) -> dict[str, int]:
    """Наибольший подтверждённый уровень по каждому навыку."""
    out: dict[str, int] = {}
    for note in notes:
        if note.type != "evidence":
            continue
        skill = str(note.data.get("skill") or "")
        try:
            level = int(note.data.get("level") or 0)
        except (TypeError, ValueError):
            level = 0
        out[skill] = max(out.get(skill, 0), level)
    return out


def collect(note: store.Note, notes: list[store.Note], limit: int = 3) -> Harvest:
    """Что ещё есть в этой работе, кроме закрытого дела."""
    result = Harvest(note)
    if note.type != "commitment" or note.status != "resolved":
        return result

    levels = proved_levels(notes)
    result.already = [n.rel for n in notes
                      if n.type == "evidence" and str(n.data.get("origin") or "") == note.rel]
    if result.already:
        return result          # с этой работы урожай уже собран

    seen = words(f"{note.title} {note.data.get('title') or ''}")
    hits: list[SkillHit] = []
    for skill in notes:
        if skill.type != "skill" or skill.status == "retired":
            continue
        domains = skill.data.get("domains") or []
        pool = (words(str(skill.title or "")) | words(russian_name(skill))
                | {w for d in domains for w in words(str(d).replace("-", " "))})
        common = seen & pool
        if not common:
            continue
        try:
            target = int(skill.data.get("target") or 0)
        except (TypeError, ValueError):
            target = 0
        hits.append(SkillHit(skill, common, levels.get(skill.rel, 0), target))

    # Одного общего слова мало: «ответ» роднит закрытие переписки с навыком
    # «отвечать за чужой провал». Такую же ошибку уже дала разметка чтения
    # щупом — 48 негодных пар из 354. Совпасть должны минимум два слова.
    hits = [h for h in hits if h.gap > 0 and len(h.hits) >= 2]
    hits.sort(key=lambda h: (-len(h.hits), -h.gap))
    result.skills = hits[:limit]

    resolution = str(note.data.get("resolution") or "")
    if resolution.startswith(OUTSIDE):
        result.outward = resolution
    return result


def render(result: Harvest, today: dt.date | None = None) -> list[str]:
    if result.already:
        return [f"С «{result.note.title}» урожай уже собран: "
                + ", ".join(result.already)]
    if result.empty:
        return []
    rows = [f"Закрыто: {result.note.title}"]
    if result.skills:
        rows.append("  Похоже на доказательство навыков:")
        for hit in result.skills:
            level = f"подтверждён {hit.proved_level}" if hit.proved_level else "не подтверждён"
            rows.append(f"    · {hit.skill.title} — {level}, цель {hit.target} "
                        f"(совпало: {', '.join(sorted(hit.hits))})")
        rows.append("    Завести? Нужен уровень и одна фраза, что именно сделано.")
    if result.outward:
        rows.append(f"  Результат вышел наружу: {result.outward}")
        rows.append("    Это материал для внешнего разговора — сохранить замыслом?")
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="что ещё есть в закрытой работе")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--item", help="путь к обязательству; без него — закрытые за окно")
    parser.add_argument("--days", type=int, default=7,
                        help="окно закрытых дел, если позиция не названа")
    parser.add_argument("--today", type=dt.date.fromisoformat, default=dt.date.today())
    args = parser.parse_args(argv)
    root = args.root.resolve()

    loaded = store.load(root, "work")
    if loaded.unreadable:
        print(loaded.complain())
        return 1
    notes = loaded.notes

    if args.item:
        target = next((n for n in notes if n.rel == args.item), None)
        if target is None:
            print(f"позиция не найдена: {args.item}")
            return 2
        chosen = [target]
    else:
        edge = args.today - dt.timedelta(days=args.days)
        chosen = [n for n in notes
                  if n.type == "commitment" and n.status == "resolved"
                  and (n.date_field("resolved") or dt.date.min) >= edge]

    printed = False
    for one in chosen:
        rows = render(collect(one, notes), args.today)
        if rows:
            print("\n".join(rows))
            printed = True
    if not printed:
        print("Собирать нечего: закрытые дела не совпали ни с одним открытым навыком "
              "и наружу не выходили.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
