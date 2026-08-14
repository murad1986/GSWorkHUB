#!/usr/bin/env python3
"""Карта развития: граф навыков, а не книг.

Вид, а не документ. Читает `work/`, строит граф из карточек `skill` и
перезаписывает `wiki/skill-map.md` целиком.

Три состояния узла, и они считаются из разных источников:

  **Доказан** — есть записи `evidence`; уровень равен наибольшему среди них.
  Чтение сюда не влияет вообще: книга поднимает изученную глубину, уровень
  поднимает сделанная работа.

  **Открыт** — предпосылки доказаны на пороге доступа, но своего доказательства
  ещё нет. Это и есть ближайшие ходы: делать можно уже сейчас.

  **Закрыт** — какая-то предпосылка ещё не доказана. Карта показывает, какая
  именно, иначе «закрыто» читается как «нельзя», а не как «сначала вот это».

Отдельно считается **изученная глубина** — по книгам, где навык размечен, и
только по прочитанным. Разрыв между изученным и доказанным — главное, что эта
карта должна показывать: прочитал и не применил.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
from pathlib import Path

import yaml
from store import Note, load

SORT = "work"
READ = {"read"}
DEFAULT_GATE = 3        # на каком уровне предпосылка считается пройденной


def skills(notes: list[Note]) -> list[Note]:
    return [n for n in notes if n.type == "skill"]


def ident(note: Note) -> str:
    return str(note.data.get("skill_id") or "")


def card_text(note: Note) -> dict:
    """Читаемая часть карточки: о чём навык и что значит каждый уровень.

    Нужна списочному виду: там узел раскрывается прямо на месте, и человек
    читает мерку, а не идёт за ней в файл.
    """
    body = note.path.read_text(encoding="utf-8").split("---", 2)[-1]
    after = re.split(r"^# .+$", body, maxsplit=1, flags=re.MULTILINE)[-1]
    about = ""
    for block in re.split(r"\n\s*\n", after.strip()):
        clean = block.strip()
        if clean and not clean.startswith("#"):
            about = " ".join(clean.split())
            break
    levels = []
    for match in re.finditer(r"^###\s*(\d)\s*[—-]\s*(.+?)$\n+(.*?)(?=^#{2,3}\s|\Z)",
                             after, flags=re.MULTILINE | re.DOTALL):
        levels.append({"n": int(match.group(1)), "name": match.group(2).strip(),
                       "text": " ".join(match.group(3).split())})
    proof = re.search(r"^## Чем подтверждается\s*\n+(.*?)(?=^## |\Z)", after,
                      flags=re.MULTILINE | re.DOTALL)
    # Первый шаг — единственное место в карточке, где написано действие на сегодня.
    # Без него карта отвечает «чему учиться», но не «с чего начать сейчас».
    first = re.search(r"^## Первый шаг\s*\n+(.*?)(?=^## |\Z)", after,
                      flags=re.MULTILINE | re.DOTALL)
    return {"about": about, "levels": levels,
            "proof": " ".join(proof.group(1).split())[:400] if proof else "",
            "first": " ".join(first.group(1).split())[:400] if first else ""}


def russian(note: Note) -> str:
    """Имя навыка по-русски — из заголовка карточки.

    В шапке `title` хранится имя из графа, английское: по нему сходятся
    спецификации и разметка книг. Читает же человек заголовок, который у
    переписанных карточек звучит на языке работы, — его и показываем.
    """
    head = re.search(r"^#\s+\S+\.\s+(.+)$",
                     note.path.read_text(encoding="utf-8"), flags=re.MULTILINE)
    return head.group(1).strip() if head else note.title


def build(notes: list[Note], gate: int) -> dict:
    nodes: dict[str, dict] = {}
    by_rel: dict[str, str] = {}
    for note in skills(notes):
        sid = ident(note)
        by_rel[note.rel] = sid
        domains = note.data.get("domains") or []
        target = note.data.get("target")
        nodes[sid] = {
            "id": sid, "rel": note.rel, "title": note.title, "ru": russian(note),
            **card_text(note),
            "domain": str(domains[0]) if domains else "",
            "domains": [str(d) for d in domains],
            "node": str(note.data.get("node") or "atomic"),
            "roles": [str(r) for r in (note.data.get("roles") or [])],
            "target": target if isinstance(target, int) else 0,
            "status": note.status,
            "requires": [], "proved": 0, "known": 0, "evidence": [], "books": [],
        }
    for note in skills(notes):
        deps = note.data.get("requires")
        nodes[ident(note)]["requires"] = ({str(k): int(v) for k, v in deps.items()
                                           if str(k) in nodes and isinstance(v, int)}
                                          if isinstance(deps, dict) else {})

    for note in notes:
        if note.type == "evidence":
            sid = by_rel.get(str(note.data.get("skill") or ""))
            level = note.data.get("level")
            if sid and isinstance(level, int):
                nodes[sid]["proved"] = max(nodes[sid]["proved"], level)
                nodes[sid]["evidence"].append({
                    "result": str(note.data.get("result") or ""), "level": level,
                    "date": str(note.data.get("date") or ""),
                    "origin": str(note.data.get("origin") or ""), "rel": note.rel,
                })
        elif note.type == "reading":
            marks = note.data.get("skills")
            if not isinstance(marks, dict):
                continue
            tags = {str(t) for t in (note.data.get("tags") or [])}
            for key, depth in marks.items():
                sid = str(key)
                if sid not in nodes or not isinstance(depth, int):
                    continue
                # Изученную глубину поднимает только прочитанное. Остальные книги
                # остаются кандидатами: чем закрывать разрыв до цели.
                if note.status in READ:
                    nodes[sid]["known"] = max(nodes[sid]["known"], depth)
                nodes[sid]["books"].append({
                    "title": note.title, "depth": depth, "status": note.status,
                    "owned": "в-наличии" in tags, "set": "набор-150" in tags,
                    "rel": note.rel, "author": str(note.data.get("author") or ""),
                })

    # Предпосылка пройдена, когда доказанный уровень достиг того, что она просит.
    # Общий порог был бы грубее: «после причинности» и «когда причинность
    # применяется самостоятельно» — разные условия, и разница в целую ступень.
    # Что читать дальше: книга полезна, если даёт глубину выше уже изученной.
    # Книга того же уровня — повторное столкновение: полезно, но потолок не
    # двигает. Своё на руках идёт первым: его не надо доставать.
    for node in nodes.values():
        node["next_books"] = sorted(
            [b for b in node["books"] if b["depth"] > node["known"]
             and b["status"] not in READ],
            key=lambda b: (-b["owned"], abs(b["depth"] - max(node["target"], 1)),
                           -b["depth"], b["title"]))[:3]

    for node in nodes.values():
        blockers = [dep for dep, need in node["requires"].items()
                    if nodes.get(dep, {}).get("proved", 0) < need]
        node["blockers"] = blockers
        node["state"] = ("доказан" if node["proved"] else
                         "открыт" if not blockers else "закрыт")
    return nodes


def render(nodes: dict, now: dt.datetime, gate: int) -> str:
    total = len(nodes)
    proved = [n for n in nodes.values() if n["state"] == "доказан"]
    open_now = [n for n in nodes.values() if n["state"] == "открыт"]
    closed = [n for n in nodes.values() if n["state"] == "закрыт"]
    gap = [n for n in nodes.values() if n["known"] > n["proved"]]

    lines = ["# Карта развития\n",
             (f"Навыков {total}: доказано {len(proved)}, открыто {len(open_now)}, "
              f"закрыто {len(closed)}. Каждая предпосылка открывает со своего "
              f"уровня — они записаны в карточках.\n")]

    if proved:
        lines.append("## Доказано работой\n")
        lines.append("| Навык | Уровень | Цель | Чем подтверждено |")
        lines.append("|---|---:|---:|---|")
        for n in sorted(proved, key=lambda n: (-n["proved"], n["id"])):
            what = n["evidence"][0]["result"][:70] if n["evidence"] else ""
            lines.append(f"| {n['id']} {n['title']} | {n['proved']} | {n['target']} | {what} |")
        lines.append("")

    if gap:
        lines.append("## Прочитано, но не применено\n")
        lines.append("Изученная глубина выше доказанного уровня — знание есть, "
                     "работы под него нет.\n")
        for n in sorted(gap, key=lambda n: -(n["known"] - n["proved"])):
            lines.append(f"- {n['id']} {n['title']}: изучено {n['known']}, "
                         f"доказано {n['proved']}")
        lines.append("")

    lines.append("## Открыто сейчас\n")
    lines.append("Предпосылки пройдены, своего доказательства ещё нет — "
                 "здесь можно действовать сегодня.\n")
    by_domain: dict[str, list[dict]] = {}
    for n in open_now:
        by_domain.setdefault(n["domain"], []).append(n)
    lines.append("| Навык | Цель | Изучено | Чем поднять |")
    lines.append("|---|---:|---:|---|")
    for n in sorted(open_now, key=lambda n: (n["domain"], n["id"])):
        book = n["next_books"][0] if n["next_books"] else None
        what = (f"{book['title'][:44]} · глубина {book['depth']}"
                + (" · на руках" if book["owned"] else "")) if book else "— книг нет"
        lines.append(f"| {n['id']} {n['title']} | {n['target']} | {n['known']} | {what} |")
    lines.append("")

    if closed:
        lines.append("## Закрыто, и чем именно\n")
        lines.append("| Навык | Не хватает |")
        lines.append("|---|---|")
        for n in sorted(closed, key=lambda n: (len(n["blockers"]), n["id"])):
            need = ", ".join(f"{b} до уровня {n['requires'][b]}" for b in n["blockers"])
            lines.append(f"| {n['id']} {n['title']} | {need} |")
        lines.append("")

    ready = [n for n in closed if len(n["blockers"]) == 1]
    if ready:
        lines.append("## Один шаг до открытия\n")
        for n in sorted(ready, key=lambda n: n["id"]):
            b = n["blockers"][0]
            lines.append(f"- {n['id']} {n['title']} — нужен {b} {nodes[b]['title']} "
                         f"на уровне {n['requires'][b]}")
        lines.append("")

    return f"""---
type: skill-map
generated: true
generated_at: {now:%Y-%m-%dT%H:%M}
skills: {total}
proved: {len(proved)}
---

{chr(10).join(lines)}
---

Считается из карточек `work/programs/adaptive-skill-rpg/` командой `make skills`.
Уровень поднимает только запись `evidence`; книги двигают изученную глубину и
никогда — доказанный уровень. Файл перезаписывается целиком.
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Карта развития по навыкам")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--json", type=Path, default=None, help="куда выгрузить граф")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    root = args.root.resolve()

    settings = root / "config" / "attention.yml"
    config = {}
    if settings.exists():
        config = yaml.safe_load(settings.read_text(encoding="utf-8")) or {}
    gate = int((config.get("skills") or {}).get("gate", DEFAULT_GATE))

    store = load(root, SORT)
    nodes = build(store.notes, gate)
    if not nodes:
        print("КАРТА РАЗВИТИЯ: ВАКУУМ — ни одного навыка. Это провал, а не пустая карта.")
        return 1

    if args.json:
        # Слой узла — длина самой длинной цепочки предпосылок до него. Это и есть
        # высота в дереве: чем позже открывается, тем дальше от начала.
        depth: dict[str, int] = {}

        def layer(sid: str, seen: frozenset[str] = frozenset()) -> int:
            if sid in depth:
                return depth[sid]
            if sid in seen:
                return 0
            deps = nodes[sid]["requires"]
            value = 0 if not deps else 1 + max(
                layer(dep, seen | {sid}) for dep in deps if dep in nodes)
            depth[sid] = value
            return value

        for sid in nodes:
            layer(sid)
        payload = {"nodes": [{**{k: v for k, v in n.items() if k != "evidence"},
                              "layer": depth[n["id"]],
                              "evidence": n["evidence"]} for n in nodes.values()]}
        args.json.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    now = dt.datetime.now().replace(microsecond=0)
    content = render(nodes, now, gate)
    if args.dry_run:
        print(content)
    else:
        (root / "wiki" / "skill-map.md").write_text(content, encoding="utf-8")

    proved = sum(1 for n in nodes.values() if n["state"] == "доказан")
    opened = sum(1 for n in nodes.values() if n["state"] == "открыт")
    print(f"карта развития: {len(nodes)} навыков, доказано {proved}, открыто {opened}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
