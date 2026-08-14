#!/usr/bin/env python3
"""Карта чтения: связный граф из карточек `reading`.

Вид, а не документ. Читает `work/`, строит граф и перезаписывает
`wiki/reading-map.md` целиком; своего состояния не держит, поэтому разойтись с
каталогом не может.

Устройство карты — четыре правила, взятые из игровых деревьев навыков и
переложенные на данные, которые в складе уже есть:

  **Центр.** Системное мышление плюс книги, названные сразу в трёх и более
  списках. Это язык, общий для всех дисциплин, и первый этап обучения: он один
  для всех секторов, а не свой у каждого.

  **Секторы.** Остальные дисциплины. Каждый соединён с центром по построению —
  поэтому связность не обеспечивается вручную, она следует из устройства.

  **Кольца.** Ярусы: база, ядро, практика, продвинутый, экспертный. Справочники
  и дополнительное лежат сбоку от линии прохождения и порогов не двигают.

  **Пороги.** Следующее кольцо сектора открывается, когда в предыдущем прочитана
  заданная доля. Правило из Borderlands: без порога человек разбегается по всем
  веткам сразу и не закрывает ни одной.

Главное свойство: граф обязан быть связным. Изолированный узел означает книгу,
до которой нельзя дойти ни одним маршрутом, — такую карта прячет от человека,
и это ровно то молчание, которое контракт запрещает.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
from collections import deque
from pathlib import Path

import yaml
from store import Note, load

SORT = "work"
CENTER_TOPIC = "системное-мышление"
UNIVERSAL_AT = 3                    # в скольких списках книга — чтобы попасть в центр
RINGS = ["база", "ядро", "практика", "продвинутый", "экспертный"]
ASIDE = ["справочник", "дополнительно"]
READ = {"read"}


def books(notes: list[Note]) -> list[Note]:
    return [n for n in notes if n.type == "reading"]


def topics_of(note: Note) -> list[str]:
    value = note.data.get("topics")
    if isinstance(value, list):
        return [str(one).strip() for one in value if str(one or "").strip()]
    return [str(value).strip()] if str(value or "").strip() else []


def tier_of(note: Note) -> str:
    return str(note.data.get("tier") or "дополнительно")


def sector_of(note: Note) -> str:
    """Центр или сектор. Универсал уходит в центр независимо от своих дисциплин:
    книга, названная в трёх списках, принадлежит не сектору, а всем сразу."""
    topics = topics_of(note)
    # Системное мышление в любой позиции списка, а не только первой: порядок
    # дисциплин задан тем, в каком списке книга встретилась раньше, и это
    # случайность источника. «Искусство системного мышления» уехало в сектор
    # процессов ровно поэтому — а книга с таким названием обязана быть в центре.
    if len(topics) >= UNIVERSAL_AT or CENTER_TOPIC in topics:
        return "центр"
    return topics[0] if topics else "центр"


def build(notes: list[Note]) -> dict:
    """Узлы и рёбра. Ребро — это «дальше можно сюда», а не «похоже на»."""
    items = books(notes)
    nodes = {}
    for note in items:
        nodes[note.rel] = {
            "rel": note.rel, "key": note.key, "title": note.title,
            "author": str(note.data.get("author") or ""),
            "topics": topics_of(note), "tier": tier_of(note),
            "sector": sector_of(note), "status": note.status,
            "level": str(note.data.get("level") or "later"),
            "kind": str(note.data.get("kind") or "book"),
            "url": str(note.data.get("url") or ""),
        }

    by_sector: dict[str, dict[str, list[str]]] = {}
    for rel, node in nodes.items():
        by_sector.setdefault(node["sector"], {}).setdefault(node["tier"], []).append(rel)

    edges: list[tuple[str, str, str]] = []

    def chain(sector: str, tiers: list[str]) -> None:
        """Два вида рёбер: вдоль кольца и от кольца к кольцу.

        Вдоль кольца — потому что соседние книги одного яруса равнодоступны:
        закончив одну, идёшь в соседнюю, а не обязан прыгать наружу. Без этих
        рёбер карта рвалась на пары «книга и её продолжение»: межкольцевых связей
        хватало, чтобы соединить пару, и не хватало, чтобы соединить кольцо.

        От кольца к кольцу — раскладкой по кругу, чтобы связи не сходились
        веером в один узел.
        """
        previous: list[str] = []
        for tier in tiers:
            current = sorted(by_sector.get(sector, {}).get(tier, []))
            for one, two in zip(current, current[1:], strict=False):
                edges.append((one, two, "вдоль"))
            if previous and current:
                for i, rel in enumerate(current):
                    edges.append((previous[i % len(previous)], rel, "кольцо"))
            if current:
                previous = current
        return None

    chain("центр", RINGS)
    center_anchor = sorted(by_sector.get("центр", {}).get("база")
                           or by_sector.get("центр", {}).get("ядро") or [])

    for sector in sorted(by_sector):
        if sector == "центр":
            continue
        chain(sector, RINGS)
        # Вход сектора: первое непустое кольцо цепляем к центру. Без этого сектор
        # висит островом, а остров на карте — это книги, до которых нет пути.
        entry: list[str] = []
        for tier in RINGS:
            entry = sorted(by_sector[sector].get(tier, []))
            if entry:
                break
        for i, rel in enumerate(entry[:6]):
            if center_anchor:
                edges.append((center_anchor[i % len(center_anchor)], rel, "вход"))

    # Замки: книга, названная в двух списках, связывает свои дисциплины. Точка
    # входа распределяется по кольцу, а не берётся всегда первой: иначе два
    # десятка замков сходятся в один узел и карта читается как паутина из одной
    # точки — при том, что смысл замка в другом, «отсюда есть ход в ту сторону».
    entries: dict[str, list[str]] = {}
    for sector, tiers in by_sector.items():
        for tier in RINGS + ASIDE:
            if tiers.get(tier):
                entries[sector] = sorted(tiers[tier])
                break
    spread: dict[str, int] = {}
    for rel in sorted(nodes):
        for topic in nodes[rel]["topics"]:
            if topic == nodes[rel]["sector"] or topic not in entries:
                continue
            ring = entries[topic]
            i = spread.get(topic, 0)
            spread[topic] = i + 1
            target = ring[i % len(ring)]
            if target != rel:
                edges.append((rel, target, "замок"))

    # Боковое: справочники и дополнительное подвешиваются к своему сектору.
    for sector, tiers in by_sector.items():
        ring = entries.get(sector) or []
        for tier in ASIDE:
            for i, rel in enumerate(sorted(tiers.get(tier, []))):
                anchor = ring[i % len(ring)] if ring else ""
                if anchor and anchor != rel:
                    edges.append((anchor, rel, "сбоку"))

    return {"nodes": nodes, "edges": edges, "by_sector": by_sector}


def components(nodes: dict, edges: list[tuple[str, str, str]]) -> list[set[str]]:
    """Куски карты, между которыми нет пути. Их обязан быть ровно один."""
    graph: dict[str, set[str]] = {rel: set() for rel in nodes}
    for one, two, _ in edges:
        if one in graph and two in graph:
            graph[one].add(two)
            graph[two].add(one)
    seen: set[str] = set()
    out: list[set[str]] = []
    for start in graph:
        if start in seen:
            continue
        block, queue = {start}, deque([start])
        seen.add(start)
        while queue:
            for neighbour in graph[queue.popleft()]:
                if neighbour not in seen:
                    seen.add(neighbour)
                    block.add(neighbour)
                    queue.append(neighbour)
        out.append(block)
    return sorted(out, key=len, reverse=True)


def progress(graph: dict, threshold: float) -> list[dict]:
    """Что открыто и что откроется. Порог — доля прочитанного в кольце."""
    out = []
    for sector in sorted(graph["by_sector"], key=lambda s: (s != "центр", s)):
        tiers = graph["by_sector"][sector]
        opened = True
        for tier in RINGS:
            rels = tiers.get(tier) or []
            if not rels:
                continue
            done = sum(1 for rel in rels if graph["nodes"][rel]["status"] in READ)
            need = max(1, round(len(rels) * threshold))
            out.append({"sector": sector, "tier": tier, "total": len(rels),
                        "read": done, "need": need, "opened": opened})
            opened = opened and done >= need
    return out


WORKING_SET = "набор-150"


def working_set(notes: list[Note]) -> list[dict]:
    """Рабочий набор по дисциплинам: сколько отобрано, откуда взято, сколько закрыто.

    Каталог отвечает на вопрос «что вообще есть», а набор — на вопрос «что читаем».
    Их нельзя мерить одним числом: шестьсот книг в каталоге и полтораста в наборе
    описывают разные вещи, и смешать их значит потерять смысл обоих.
    """
    picked = [n for n in books(notes) if WORKING_SET in (n.data.get("tags") or [])]
    by_topic: dict[str, list[Note]] = {}
    for note in picked:
        for topic in topics_of(note):
            by_topic.setdefault(topic, []).append(note)
    out = []
    for topic in sorted(by_topic, key=lambda t: -len(by_topic[t])):
        group = sorted(by_topic[topic],
                       key=lambda n: (RINGS.index(tier_of(n)) if tier_of(n) in RINGS
                                      else len(RINGS), n.title))
        tags = [set(str(t) for t in (n.data.get("tags") or [])) for n in group]
        out.append({
            "topic": topic, "total": len(group),
            "owned": sum(1 for t in tags if "в-наличии" in t),
            "proposed": sum(1 for t in tags if "предложено-агентом" in t),
            "read": sum(1 for n in group if n.status in READ),
            "reading": sum(1 for n in group if n.status == "reading"),
            "items": group,
        })
    return out


def skill_coverage(notes: list[Note]) -> list[dict]:
    """Навыки против книг: докуда доводит прочитанное и докуда — доступное.

    Две разные величины, и путать их нельзя. Изученная глубина считается только
    по прочитанным книгам: пока книга лежит в очереди, она не научила ничему.
    Доступная глубина — потолок всего набора: докуда можно дойти, если прочитать
    всё размеченное. Владение здесь не считается вовсе — его поднимает работа, а
    не чтение, и живёт оно в записях `evidence`.
    """
    skills = [n for n in notes if n.type == "skill"]
    if not skills:
        return []
    marks: dict[str, list[tuple[int, Note]]] = {}
    for note in books(notes):
        table = note.data.get("skills")
        if not isinstance(table, dict):
            continue
        for ident, depth in table.items():
            if isinstance(depth, int):
                marks.setdefault(str(ident), []).append((depth, note))

    # Доказанное владение: наибольший уровень среди доказательств этого навыка.
    # Считается по ссылке из доказательства на карточку, а не по полю в карточке —
    # иначе уровень пришлось бы хранить дважды и он разошёлся бы с основаниями.
    proven: dict[str, list[int]] = {}
    by_rel = {n.rel: n for n in notes}
    for note in notes:
        if note.type != "evidence":
            continue
        target = by_rel.get(str(note.data.get("skill") or ""))
        level = note.data.get("level")
        if target is not None and isinstance(level, int):
            proven.setdefault(str(target.data.get("skill_id")), []).append(level)

    out = []
    for skill in sorted(skills, key=lambda n: str(n.data.get("skill_id"))):
        ident = str(skill.data.get("skill_id"))
        pairs = marks.get(ident, [])
        read = [d for d, n in pairs if n.status in READ]
        target = skill.data.get("target")
        out.append({
            "id": ident, "title": skill.title, "node": str(skill.data.get("node")),
            "target": target if isinstance(target, int) else 0,
            "known": max(read, default=0),
            "reachable": max((d for d, _ in pairs), default=0),
            "books": len(pairs),
            "proven": max(proven.get(ident, []), default=0),
            "proofs": len(proven.get(ident, [])),
        })
    return out


def render(graph: dict, rows: list[dict], parts: list[set[str]], now: dt.datetime,
           threshold: float, plan: list[dict] | None = None,
           skills: list[dict] | None = None) -> str:
    nodes = graph["nodes"]
    total = len(nodes)
    read = sum(1 for n in nodes.values() if n["status"] in READ)
    in_hand = [n for n in nodes.values() if n["status"] == "reading"]
    locks = sum(1 for _, _, kind in graph["edges"] if kind == "замок")

    lines = ["# Карта чтения\n",
             f"Книг на карте {total}, прочитано {read}. "
             f"Связных кусков {len(parts)} — карта "
             + ("цела." if len(parts) == 1 else "РАЗОРВАНА, есть недостижимые книги.")
             + f" Переходов между дисциплинами {locks}.\n"]
    if in_hand:
        lines.append("**Сейчас в руках:** "
                     + "; ".join(f"{n['title']}" for n in in_hand) + "\n")

    if plan:
        picked = sum(row["total"] for row in plan)
        unique = len({row["topic"] for row in plan})
        lines.append("## Рабочий набор\n")
        lines.append(f"Двенадцать дисциплин, {picked} мест на {unique} направлений. "
                     "Книга, работающая на две дисциплины, занимает место в каждой, "
                     "но читается один раз.\n")
        lines.append("| Дисциплина | В наборе | На руках | Предложено | Читаю | Прочитано |")
        lines.append("|---|---:|---:|---:|---:|---:|")
        for row in plan:
            lines.append(f"| {row['topic']} | {row['total']} | {row['owned']} | "
                         f"{row['proposed']} | {row['reading']} | {row['read']} |")
        lines.append("")
        # Состав, а не только числа: «наполнено» проверяется по именам книг, и
        # если состав виден только в карточках, набор нельзя ни оспорить, ни
        # обсудить целиком.
        for row in plan:
            lines.append(f"### {row['topic']} — {row['total']}\n")
            for note in row["items"]:
                tags = {str(t) for t in (note.data.get("tags") or [])}
                where = ("на руках" if "в-наличии" in tags
                         else "предложено" if "предложено-агентом" in tags else "каталог")
                mark = {"read": " — прочитано", "reading": " — читаю"}.get(note.status, "")
                author = str(note.data.get("author") or "")
                lines.append(f"- {note.title}" + (f" — {author}" if author else "")
                             + f" · {tier_of(note)} · {where}{mark}")
            lines.append("")

    if skills:
        no_book = [s for s in skills if s["books"] == 0 and s["node"] == "atomic"]
        short = [s for s in skills if s["reachable"] < s["target"] and s["books"]]
        lines.append("## Навыки против книг\n")
        lines.append(
            f"Навыков в графе {len(skills)}, книгами закрыто "
            f"{sum(1 for s in skills if s['books'])}. "
            "Изученная глубина считается только по прочитанному; доступная — потолок "
            "того, что дадут книги набора. Доказанное владение приходит не отсюда: "
            "его поднимает сделанная работа, записанная доказательствами.\n")
        lines.append("| Навык | Тип | Цель | Доступно | Изучено | Доказано | Книг |")
        lines.append("|---|---|---:|---:|---:|---:|---:|")
        for s in skills:
            lines.append(f"| {s['id']} {s['title']} | {s['node']} | {s['target']} | "
                         f"{s['reachable']} | {s['known']} | {s['proven']} | "
                         f"{s['books']} |")
        lines.append("")
        if no_book:
            lines.append("**Самостоятельные навыки без единой книги:** "
                         + ", ".join(f"{s['id']} {s['title']}" for s in no_book)
                         + ". Их нечем изучать в текущем наборе.\n")
        if short:
            lines.append("**Набора не хватает до цели:** "
                         + ", ".join(f"{s['id']} ({s['reachable']} из {s['target']})"
                                     for s in short)
                         + ". Разница закрывается практикой или другими книгами.\n")

    lines.append("## Кольца и пороги\n")
    lines.append(f"Кольцо открывается, когда в предыдущем прочитано "
                 f"{threshold:.0%}.\n")
    lines.append("| Сектор | Кольцо | Прочитано | Нужно | Состояние |")
    lines.append("|---|---|---|---|---|")
    for row in rows:
        state = "открыто" if row["opened"] else "закрыто"
        lines.append(f"| {row['sector']} | {row['tier']} | {row['read']} из "
                     f"{row['total']} | {row['need']} | {state} |")
    if len(parts) > 1:
        lines.append("\n## Недостижимое\n")
        for block in parts[1:]:
            for rel in sorted(block)[:10]:
                lines.append(f"- {nodes[rel]['title']} — `{rel}`")
    return f"""---
type: reading-map
generated: true
generated_at: {now:%Y-%m-%dT%H:%M}
books: {total}
components: {len(parts)}
---

{chr(10).join(lines)}

---

Считается из карточек `work/me/reading/` командой `make map`. Файл
перезаписывается целиком, править руками бесполезно.
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Карта чтения")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--json", type=Path, default=None, help="куда выгрузить граф")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    root = args.root.resolve()

    # Конфига может не быть: карта обязана собираться и на голом складе, иначе
    # инструмент падает трейсбеком там, где достаточно значения по умолчанию.
    settings = root / "config" / "attention.yml"
    config = {}
    if settings.exists():
        config = yaml.safe_load(settings.read_text(encoding="utf-8")) or {}
    threshold = float((config.get("reading") or {}).get("ring_threshold", 0.5))

    store = load(root, SORT)
    if store.complain():
        print(store.complain())
    graph = build(store.notes)
    if not graph["nodes"]:
        print("КАРТА: ВАКУУМ — ни одной книги. Это провал, а не пустая карта.")
        return 1
    parts = components(graph["nodes"], graph["edges"])
    rows = progress(graph, threshold)
    now = dt.datetime.now().replace(microsecond=0)
    content = render(graph, rows, parts, now, threshold,
                     working_set(store.notes), skill_coverage(store.notes))

    if args.json:
        # Книга без разметки по навыкам и без пометок набора — просто название.
        # Виду нужно знать, что она даёт и откуда взялась, поэтому выгружаем и то,
        # и другое, а вместе с ними читаемые имена навыков.
        extra = {n.rel: n for n in store.notes if n.type == "reading"}
        payload = []
        for rel, node in graph["nodes"].items():
            note = extra.get(rel)
            marks = (note.data.get("skills") if note else None) or {}
            payload.append({**node,
                            "skills": marks if isinstance(marks, dict) else {},
                            "tags": [str(t) for t in ((note.data.get("tags") or [])
                                                      if note else [])],
                            "source": str((note.data.get("source") or "") if note else "")})
        names = {}
        for note in store.notes:
            if note.type != "skill":
                continue
            head = re.search(r"^#\s+\S+\.\s+(.+)$",
                             note.path.read_text(encoding="utf-8"), flags=re.MULTILINE)
            names[str(note.data.get("skill_id"))] = {
                "ru": head.group(1).strip() if head else note.title,
                "domain": str((note.data.get("domains") or [""])[0]),
                "target": note.data.get("target"),
            }
        args.json.write_text(json.dumps(
            {"nodes": payload, "skills": names,
             "edges": [{"from": a, "to": b, "kind": k} for a, b, k in graph["edges"]]},
            ensure_ascii=False), encoding="utf-8")

    if args.dry_run:
        print(content)
    else:
        (root / "wiki" / "reading-map.md").write_text(content, encoding="utf-8")
    print(f"карта: {len(graph['nodes'])} книг, {len(graph['edges'])} связей, "
          f"кусков {len(parts)}")
    return 0 if len(parts) == 1 else 1


if __name__ == "__main__":
    raise SystemExit(main())
