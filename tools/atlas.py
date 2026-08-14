#!/usr/bin/env python3
"""Атлас развития: карта навыков и каталог ресурсов одной читаемой страницей.

Два вида уже есть — `wiki/skill-map.md` и `wiki/reading-map.md`. Они отвечают на
вопрос «что в складе», но не на вопрос «что мне с этим делать»: у навыка ступени
описаны в его карточке, книги под него — в другом файле, доказательства — в
третьем, и чтобы собрать одну мысль, человек открывает четыре места.

Атлас собирает их в одну страницу с адресом на каждую сущность: `#/skill/bp04`,
`#/reading/<файл>`, `#/domain/strategy`. Ссылку можно кинуть, положить в закладку,
вернуться кнопкой «назад».

Считает не сам: узлы берутся из `skill_map.build`, ресурсы — из `reading_map`.
Свой счёт был бы вторым источником правды и разошёлся бы с картами на первой же
правке. Отсюда же и правило вывода: изученность поднимает прочитанная книга,
владение — только доказательство, и на странице это две отдельные шкалы.

Пишет одним самодостаточным файлом: ни сети, ни соседних файлов ему не нужно,
поэтому его можно открыть с телефона, приложить к письму и убрать в архив.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
from pathlib import Path

import reading_map
import skill_map
import yaml
from store import Note, load

ROOT = Path(__file__).resolve().parents[1]
SORT = "work"
SET_TAG = "набор-150"
OWNED_TAG = "в-наличии"

LEVEL_RU = {1: "узнаёт", 2: "объясняет", 3: "применяет сам",
            4: "соединяет с другими", 5: "переносит на новое"}
KIND_RU = {"book": "книга", "article": "статья", "paper": "работа",
           "course": "курс", "talk": "доклад"}
STATUS_RU = {"queued": "в очереди", "reading": "в чтении",
             "read": "прочитано", "dropped": "брошено"}
NODE_RU = {"atomic": "простой", "compound": "составной", "meta": "мета-узел"}


def slug(rel: str) -> str:
    """Адрес ресурса — имя его файла. Путь и есть опознавательный знак записи."""
    return rel.split("/")[-1][:-3]


def domains(root: Path) -> dict[str, dict]:
    """Названия дисциплин по-человечески: сперва карточка `type: domain`, если она
    написана, иначе короткая строка из `config/domains.yml`.

    Дисциплина заведена записью склада не сразу — сначала жила настройкой
    вывода. Карточка даёт разворачиваемый текст («что это», «в каком порядке
    брать»), настройка — только имя и подпись; там, где карточки ещё нет,
    страница не должна остаться пустой.
    """
    settings = root / "config" / "domains.yml"
    names = yaml.safe_load(settings.read_text(encoding="utf-8")) or {} \
        if settings.exists() else {}
    out = {k: dict(v) for k, v in names.items()}
    for note in load(root, "work").notes:
        if note.type != "domain":
            continue
        key = str(note.data.get("domain_id") or "")
        if not key:
            continue
        body = note.path.read_text(encoding="utf-8").split("---", 2)[-1]
        raw: dict[str, str] = {}
        for m in re.finditer(r"^##\s+(.+?)\s*\n+(.*?)(?=^##\s|\Z)",
                              body, flags=re.MULTILINE | re.DOTALL):
            raw[m.group(1).strip()] = m.group(2)
        entry = out.setdefault(key, {})
        entry["ru"] = str(note.data.get("title") or entry.get("ru") or key)
        entry["order"] = note.data.get("order")
        entry["about"] = plain(" ".join(
            raw.get("Что это и зачем она тебе", entry.get("about", "")).split()))
        entry["teaser"] = first_sentence(entry["about"])
        entry["composition"] = numbered_list(raw.get("Из чего состоит", ""))
        entry["order_hint"] = plain(" ".join(raw.get("В каком порядке брать", "").split()))
        entry["closure"] = plain(" ".join(raw.get("Чем закрывается", "").split()))
    return out


def plain(text: str) -> str:
    """Текст без ссылок разметки: остаётся то, что человек читает."""
    return re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text).replace("**", "")


def first_sentence(text: str, limit: int = 160) -> str:
    """Анонс дисциплины на плитке карты: одно предложение, не абзац.

    Плитка карты — обзор двенадцати дисциплин разом, ей нужен анонс, а не
    объяснение; развёрнутый текст остаётся на странице самой дисциплины.
    Резать по границе предложения, а не просто по длине — обрыв на запятой
    читается как баг, обрыв на точке — как оформление.
    """
    m = re.search(rf"^.{{1,{limit}}}?[.!?](?=\s|$)", text)
    if m:
        return m.group(0)
    return text[:limit].rsplit(" ", 1)[0] + "…" if len(text) > limit else text


# Двенадцать карточек написаны двенадцатью проходами одного агента-семейства,
# и «Из чего состоит» вышло в двух разных шаблонах: «Заголовок (`ID`)» в одних
# доменах, «ID — Заголовок (English)» в других. Разбор пробует оба по очереди —
# требовать от текста одной формы означало бы переписывать половину карточек
# заново вместо того, чтобы один раз научить разбор их читать.
ITEM_TITLE_FIRST = re.compile(
    r"^(.*?)\s*\(`([A-Za-z0-9]+)`(?:,\s*([^)]+))?\)\s*[—-]?\s*(.*)$", re.DOTALL)
ITEM_ID_FIRST = re.compile(
    r"^([A-ZА-Я]{1,3}\d{1,2})\s*[—-]\s*(.+?)\s*\(([^)]*)\)\.?\s*(.*)$", re.DOTALL)
NOTE_WORD = re.compile(r"составн|мета", re.IGNORECASE)


def numbered_list(text: str) -> dict:
    """Раздел «Из чего состоит» — список ссылок на навыки, не сырая нотация.

    Прежняя сборка схлопывала все переносы строк в один пробел ради «about»,
    где это уместно: там сплошной текст. Список из семи навыков, склеенный
    той же функцией, превращается в стену без единого разрыва — семь мыслей
    читаются как одна.

    Дальше — вторая правка: агент писал «Разбор работы на шаги (`AI02`)» —
    это адрес для машины, не для читателя. Название навыка уже сказано
    по-русски прямо перед скобкой; из скобки нужен только идентификатор для
    ссылки, а саму нотацию читатель видеть не должен.

    Часть карточек начинает раздел вступительной фразой до самого списка
    («Дисциплина строится по одной линии…») — она не пункт и не разбирается
    как пункт, а идёт отдельной строкой перед списком.
    """
    chunks = re.split(r"(?m)^\s*\d+\.\s+", text.strip())
    intro = plain(" ".join(chunks[0].split())) if chunks and chunks[0].strip() else ""
    items = [plain(" ".join(item.split())) for item in chunks[1:] if item.strip()]
    out = []
    for item in items:
        m = ITEM_TITLE_FIRST.match(item)
        if m:
            out.append({"title": m.group(1).strip(), "id": m.group(2),
                        "note": (m.group(3) or "").strip(), "desc": m.group(4).strip()})
            continue
        m = ITEM_ID_FIRST.match(item)
        if m:
            sid, title, paren, desc = m.groups()
            parts = [p.strip() for p in paren.split(",")]
            note = parts[-1] if parts and NOTE_WORD.search(parts[-1]) else ""
            out.append({"title": title.strip(), "id": sid, "note": note,
                        "desc": desc.strip()})
            continue
        out.append({"title": "", "id": "", "note": "", "desc": item})
    return {"intro": intro, "items": out}


def tags_of(note: Note) -> set[str]:
    return {str(t) for t in (note.data.get("tags") or [])}


# Служебные пометки в теле карточки: как книга попала в склад, кем размечена,
# когда выведена из набора. Это следы работы над записью, а не её содержание.
# Резать по одному разделителю не вышло — формулировки писались разными
# заходами и стоят вперемешку с описанием; поэтому абзац отбрасывается по
# тому, с чего он начинается.
SERVICE_START = re.compile(
    r"^(Книга предложена|Ресурс предложен|Повод —|Дисциплина уточнена|"
    r"Дисциплины:|Разметка по навыкам|Выведена из рабочего|Введена в рабочий|"
    r"Автор назван человеком|Названа в списках|Ярус:|Содержание не опознано|"
    r"Подобран |Вторая карточка на ту же книгу)")


def book_about(path: Path) -> str:
    """Описание книги — то, о чём она, без следов работы над карточкой.

    Читателю на странице нужно содержание книги. Всё остальное — что её
    предложил агент, что дисциплину уточнили щупом, что разметка это суждение —
    правда о записи, а не о книге, и выдавать её за описание нельзя: сорок две
    карточки из шестисот двадцати семи так и показывали служебку вместо смысла,
    а у части книг за ней вообще не было ни строки описания.
    """
    body = path.read_text(encoding="utf-8").split("---", 2)[-1]
    after = re.split(r"^\*\*.+?\*\*\s*$", body, maxsplit=1, flags=re.MULTILINE)[-1]
    keep = []
    for block in re.split(r"\n\s*\n", after.strip()):
        clean = " ".join(block.split())
        if clean and not SERVICE_START.match(clean):
            keep.append(clean)
    return plain(" ".join(keep))


def first_para(path: Path) -> str:
    """Первый абзац тела карточки — вступление до первого раздела.

    У профиля тело устроено иначе, чем у книги: сразу после заголовка идёт
    вступление, дальше разделы «Откуда веса», «Как считается». На страницу нужно
    только вступление — разделы объясняют устройство и живут в самой карточке.
    """
    body = path.read_text(encoding="utf-8").split("---", 2)[-1]
    after = re.split(r"^#\s+.+$", body, maxsplit=1, flags=re.MULTILINE)[-1]
    head = after.split("\n##")[0]
    for block in re.split(r"\n\s*\n", head.strip()):
        clean = " ".join(block.split())
        if clean:
            return plain(clean)
    return ""


def covers(root: Path) -> dict[str, str]:
    """Слепок обложек из Open Library: файл `slug → картинка, целиком, base64`.

    Не факт склада, а кэш стороннего сервиса — переиндексируется он, не мы.
    Обновляется `tools/coverkit/fetch_covers.py`, вручную не трогается. Картинка
    внутри, не ссылкой: страница обещана самодостаточной — «положить в закладку,
    приложить к письму, открыть с телефона без сети», — и внешний адрес это
    обещание рвёт молча: письмо без интернета покажет дыры вместо обложек.
    """
    path = root / "tools" / "coverkit" / "covers.json"
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {k: {"data": v.get("data"), "year": v.get("year"), "pages": v.get("pages")}
            for k, v in raw.items() if v.get("data")}


def collect(root: Path, gate: int) -> dict:
    """Данные атласа: навыки, ресурсы, дисциплины — из тех же сборок, что и карты."""
    notes = load(root, SORT).notes
    nodes = skill_map.build(notes, gate)
    names = domains(root)
    art = covers(root)

    shelf: dict[str, dict] = {}
    for note in reading_map.books(notes):
        marks = note.data.get("skills")
        tags = tags_of(note)
        book_slug = slug(note.rel)
        meta = art.get(book_slug) or {}
        shelf[book_slug] = {
            "slug": book_slug, "title": note.title,
            "author": str(note.data.get("author") or ""),
            "kind": str(note.data.get("kind") or "book"),
            "tier": reading_map.tier_of(note),
            "status": note.status,
            "set": SET_TAG in tags, "owned": OWNED_TAG in tags,
            "no": int(re.sub(r"\D", "", str(note.data.get("key") or "0")) or 0),
            "url": str(note.data.get("url") or ""),
            "cover": meta.get("data"),
            "year": meta.get("year"),
            "pages": meta.get("pages"),
            "about": book_about(note.path),
            "skills": sorted(((str(k), int(v)) for k, v in marks.items()
                              if str(k) in nodes and isinstance(v, int)),
                             key=lambda kv: -kv[1]) if isinstance(marks, dict) else [],
        }

    # Кто на кого опирается — обратная сторона предпосылок. Хранить её у навыка
    # руками значило бы вести список связей вручную там, где он вычисляется.
    opens: dict[str, list] = {sid: [] for sid in nodes}
    for sid, node in nodes.items():
        for dep, need in (node.get("requires") or {}).items():
            if dep in opens:
                opens[dep].append((sid, nodes[sid]["ru"], need))

    skills = {}
    for sid, n in sorted(nodes.items()):
        skills[sid] = {
            "id": sid, "ru": n["ru"], "en": n["title"], "about": n.get("about", ""),
            "domains": [str(d) for d in (n.get("domains") or [])],
            "node": n["node"], "roles": n.get("roles") or [], "target": n["target"],
            "known": n["known"], "proved": n["proved"], "state": n["state"],
            "proof": plain(n.get("proof", "")),
            "first": plain(n.get("first", "")),
            "levels": [[lv["n"], LEVEL_RU.get(lv["n"], lv.get("name", "")), lv["text"]]
                       for lv in n.get("levels") or []],
            "requires": [[dep, need, nodes[dep]["ru"],
                          1 if nodes[dep]["proved"] >= need else 0]
                         for dep, need in sorted((n.get("requires") or {}).items())
                         if dep in nodes],
            "opens": [[o, ru, need] for o, ru, need in sorted(opens[sid])],
            # Порядок чтения, а не алфавит: сперва глубина (книга, заводящая
            # дальше, — раньше), при равной глубине — та, что уже на руках:
            # её можно открыть сегодня, остальные надо сперва добыть.
            "books": [[b["rel"].split("/")[-1][:-3], b["depth"]]
                      for b in sorted(n.get("books") or [],
                                      key=lambda b: (-b["depth"], not b.get("owned"),
                                                     not b.get("set"), b["title"]))
                      if b.get("rel")],
            "evidence": [[e["result"], e["level"], e["date"]]
                         for e in n.get("evidence") or []],
        }

    # Слой навыка — длина самой длинной цепочки предпосылок до него. Это и есть
    # его высота в дереве: чем позже открывается, тем дальше от начала. Считается
    # обходом с защитой от петли — петлю ловит линтер, но вид не должен от неё
    # зависать, пока её не починили.
    seen_depth: dict[str, int] = {}

    def layer_of(sid: str, seen: frozenset[str] = frozenset()) -> int:
        if sid in seen_depth:
            return seen_depth[sid]
        if sid in seen:
            return 0
        deps = [r[0] for r in skills[sid]["requires"] if r[0] in skills]
        value = 0 if not deps else 1 + max(layer_of(d, seen | {sid}) for d in deps)
        seen_depth[sid] = value
        return value

    for sid in skills:
        skills[sid]["layer"] = layer_of(sid)

    used = {d for s in skills.values() for d in s["domains"]}
    doms = {}
    # Порядок дисциплин — из их карточек (`order`), а не по ключу. Ключи
    # английские, и алфавит по ним ставил «ai-automation» первым, а
    # «systems-thinking» последним — ровно наоборот тому, как дисциплины
    # выстроены в складе, где системное мышление названо фундаментом карты.
    for key in sorted(used, key=lambda k: ((names.get(k) or {}).get("order") or 99, k)):
        # Навык числится в дисциплине один раз — в основной, первой в списке.
        # Иначе «Диагноз бизнеса» попадает в счёт пяти дисциплин сразу: одна
        # доказанная работа зажигает пять полосок, и карта показывает движение
        # там, где сделано одно дело. Остальные дисциплины навык не считают, но
        # показывают его отдельно — связь-то настоящая.
        ids = [s for s in skills if skills[s]["domains"][:1] == [key]]
        guests = [s for s in skills if key in skills[s]["domains"][1:]]
        depth: dict[str, int] = {}
        for sid in ids + guests:
            for bslug, d in skills[sid]["books"]:
                depth[bslug] = max(depth.get(bslug, 0), d)
        card = names.get(key) or {}
        doms[key] = {
            "key": key,
            "ru": card.get("ru") or key,
            "order": card.get("order") or 99,
            "about": card.get("about") or "",
            "teaser": card.get("teaser") or card.get("about") or "",
            "composition": card.get("composition") or {"intro": "", "items": []},
            "order_hint": card.get("order_hint") or "",
            "closure": card.get("closure") or "",
            "skills": sorted(ids),
            "guests": sorted(guests),
            "books": [b for b, _ in sorted(depth.items(), key=lambda kv: (-kv[1], kv[0]))],
            "proved": sum(1 for s in ids if skills[s]["state"] == "доказан"),
            "open": sum(1 for s in ids if skills[s]["state"] == "открыт"),
            "shut": sum(1 for s in ids if skills[s]["state"] == "закрыт"),
        }

    totals = {
        "skills": len(skills),
        "proved": sum(1 for s in skills.values() if s["state"] == "доказан"),
        "open": sum(1 for s in skills.values() if s["state"] == "открыт"),
        "shut": sum(1 for s in skills.values() if s["state"] == "закрыт"),
        "catalog": len(shelf),
        "set": sum(1 for b in shelf.values() if b["set"]),
        "owned": sum(1 for b in shelf.values() if b["owned"]),
        "read": sum(1 for b in shelf.values() if b["status"] == "read"),
        "marked": sum(1 for b in shelf.values() if b["skills"]),
        "evidence": sum(len(s["evidence"]) for s in skills.values()),
    }
    return {"skills": skills, "books": shelf, "domains": doms,
            "builds": builds(notes, skills), "whoami": whoami(root),
            "domRu": {k: v.get("ru", k) for k, v in names.items()}, "totals": totals}


def whoami(root: Path) -> str:
    """Одна фраза о человеке из его профиля — «кто я» на входе в карту."""
    card = root / "work" / "me" / "profil.md"
    if not card.exists():
        return ""
    m = re.search(r"^## Тип одной фразой\s*\n+(.*?)(?=^## |\Z)",
                  card.read_text(encoding="utf-8"), re.MULTILINE | re.DOTALL)
    return " ".join(m.group(1).split())[:220] if m else ""


def builds(notes: list, skills: dict) -> list[dict]:
    """Целевые профили: сколько из требуемого уже доказано, а сколько прочитано.

    Готовность считается с ограничением сверху — `min(владение / цель, 1)` по
    каждому навыку, взвешенно. Ограничение принципиально: пятый уровень при цели
    четыре даёт сто процентов, а не сто двадцать пять, и не компенсирует ноль по
    другому требованию. Иначе профиль «достигается» перекосом в сильную сторону,
    а собран он ровно затем, чтобы перекос было видно.

    Покрытие теорией считается теми же весами и рядом, но не складывается с
    готовностью: одно говорит, что было доступно прочитать, второе — что
    доказано работой. Смешать их значит вернуться к измерению книгами.
    """
    out = []
    for note in notes:
        if note.type != "build":
            continue
        raw = note.data.get("requires")
        if not isinstance(raw, dict):
            continue
        rows, total = [], 0.0
        done_m, done_k = 0.0, 0.0
        for sid, req in raw.items():
            s = skills.get(str(sid))
            if not s or not isinstance(req, dict):
                continue
            need = int(req.get("level") or s["target"])
            weight = float(req.get("weight") or 1)
            mastery = min(s["proved"] / need, 1) if need else 0
            theory = min(s["known"] / need, 1) if need else 0
            total += weight
            done_m += weight * mastery
            done_k += weight * theory
            rows.append({"id": str(sid), "ru": s["ru"], "need": need,
                         "weight": weight, "proved": s["proved"], "known": s["known"],
                         "state": s["state"], "domains": s["domains"]})
        rows.sort(key=lambda r: (-r["weight"], r["id"]))

        # Фундамент профиля — все навыки, через которые к нему идёт путь: сами
        # требования плюс их предпосылки вглубь. Без этого числа профиль,
        # собранный из вершин графа, показывает ноль до самого конца — хотя
        # доказанные шесть навыков лежат именно здесь, в основании.
        base: set[str] = set()
        queue = [r["id"] for r in rows]
        while queue:
            sid = queue.pop()
            if sid in base or sid not in skills:
                continue
            base.add(sid)
            queue.extend(d[0] for d in skills[sid]["requires"])

        out.append({
            "base": len(base),
            "baseProved": sum(1 for s in base if skills[s]["state"] == "доказан"),
            "baseOpen": sum(1 for s in base if skills[s]["state"] == "открыт"),
            "id": str(note.data.get("build_id") or ""),
            "title": note.title,
            "status": note.status,
            "about": first_para(note.path),
            "rows": rows,
            "mastery": round(100 * done_m / total) if total else 0,
            "theory": round(100 * done_k / total) if total else 0,
            # «Достигнут» — не средний процент, а два условия сразу: взвешенная
            # готовность полная и каждое требование дошло до своей цели.
            "complete": bool(rows) and all(r["proved"] >= r["need"] for r in rows),
        })
    return out


STYLE = """
:root{
  --paper:#f5f3ee; --card:#fffefb; --ink:#191e1c; --soft:#6a716e; --faint:#949c99;
  --rule:#e3dfd6; --hair:#edeae3;
  --proved:oklch(0.55 0.10 68); --open:oklch(0.55 0.10 208); --shut:#a8afac;
  --wash-p:oklch(0.94 0.03 68); --wash-o:oklch(0.94 0.03 208);
  --fd:"Avenir Next","Segoe UI Variable Display",ui-sans-serif,system-ui,sans-serif;
  --fb:ui-sans-serif,system-ui,"Segoe UI",sans-serif;
  --fm:ui-monospace,"SF Mono",Menlo,Consolas,monospace;
}
@media (prefers-color-scheme:dark){:root{
  --paper:#0f1416; --card:#161d20; --ink:#e8ede9; --soft:#98a29f; --faint:#66726f;
  --rule:#26302f; --hair:#1b2325; --proved:oklch(0.75 0.11 68);
  --open:oklch(0.72 0.10 208); --shut:#5c6866;
  --wash-p:oklch(0.28 0.04 68); --wash-o:oklch(0.28 0.04 208);}}
:root[data-theme="dark"]{
  --paper:#0f1416; --card:#161d20; --ink:#e8ede9; --soft:#98a29f; --faint:#66726f;
  --rule:#26302f; --hair:#1b2325; --proved:oklch(0.75 0.11 68);
  --open:oklch(0.72 0.10 208); --shut:#5c6866;
  --wash-p:oklch(0.28 0.04 68); --wash-o:oklch(0.28 0.04 208);}
:root[data-theme="light"]{
  --paper:#f5f3ee; --card:#fffefb; --ink:#191e1c; --soft:#6a716e; --faint:#949c99;
  --rule:#e3dfd6; --hair:#edeae3; --proved:oklch(0.55 0.10 68);
  --open:oklch(0.55 0.10 208); --shut:#a8afac;
  --wash-p:oklch(0.94 0.03 68); --wash-o:oklch(0.94 0.03 208);}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font-family:var(--fb);
  line-height:1.6;-webkit-font-smoothing:antialiased}
a{color:inherit;text-decoration:none}
a:hover{color:var(--open)}
.wrap{max-width:1000px;margin:0 auto;padding:0 24px 72px}
.top{position:sticky;top:0;z-index:20;border-bottom:1px solid var(--rule);
  background:color-mix(in srgb,var(--paper) 88%,transparent);backdrop-filter:blur(14px)}
.topin{max-width:1000px;margin:0 auto;padding:11px 24px;display:flex;
  align-items:center;gap:18px;flex-wrap:wrap}
.mark{font-family:var(--fd);font-size:15px;white-space:nowrap;order:1}
.nav{display:flex;gap:16px;font-size:13.5px;color:var(--soft);order:2}
.nav a.on{color:var(--ink)}
.grow{flex:1;order:3}
.find{position:relative;width:236px;order:4}
.theme{order:5}
.find input{width:100%;padding:7px 11px;font:inherit;font-size:13px;color:var(--ink);
  background:var(--card);border:1px solid var(--rule);border-radius:8px}
.find input:focus{outline:2px solid var(--open);outline-offset:1px}
.pop{position:absolute;top:38px;right:0;width:min(380px,calc(100vw - 32px));
  max-height:60vh;overflow:auto;background:var(--card);border:1px solid var(--rule);
  border-radius:11px;padding:6px;box-shadow:0 12px 34px rgba(0,0,0,.13)}
@media (max-width:640px){
  .topin{padding:10px 16px;gap:8px 12px}
  .grow{display:none}
  .find{order:6;flex:1 1 100%;width:auto}
}
.pop a{display:grid;grid-template-columns:46px 1fr;gap:10px;padding:7px 9px;
  border-radius:7px;font-size:13.5px;align-items:baseline}
.pop a:hover{background:var(--hair);color:inherit}
.pop .k{font-family:var(--fm);font-size:10.5px;color:var(--faint);
  text-transform:uppercase;letter-spacing:.07em}
.pop .sub{display:block;color:var(--soft);font-size:12px}
.pop .none{padding:10px;font-size:13px;color:var(--soft)}
.theme{font:inherit;font-size:15px;line-height:1;padding:6px 9px;cursor:pointer;
  color:var(--soft);background:transparent;border:1px solid var(--rule);border-radius:8px}
.crumb{display:flex;flex-wrap:wrap;gap:8px;font-size:12.5px;color:var(--faint);
  padding:22px 0 0;font-family:var(--fm)}
.crumb a:hover{color:var(--open)}
h1{font-family:var(--fd);font-weight:500;font-size:34px;line-height:1.18;margin:12px 0 0;
  letter-spacing:-.012em;text-wrap:pretty}
.lede{margin:10px 0 0;font-size:15.5px;color:var(--soft);max-width:62ch;text-wrap:pretty}
.en{font-family:var(--fm);font-size:12px;color:var(--faint);margin:7px 0 0}
.chips{display:flex;flex-wrap:wrap;gap:7px;margin:15px 0 0}
.chip{font-size:12px;padding:3px 10px;border-radius:20px;background:var(--hair);
  color:var(--soft);border:1px solid transparent}
a.chip:hover{border-color:var(--open);color:var(--open)}
.code-link{display:inline-block;font-family:var(--fm);font-size:11.5px;line-height:1.35;
  padding:1px 7px;border-radius:6px;background:var(--hair);color:var(--open);
  border:1px solid transparent;white-space:nowrap;vertical-align:baseline}
.code-link:hover{border-color:var(--open);color:var(--open)}
.sect{margin:42px 0 0}
.sect>h2{font-family:var(--fm);font-size:10.5px;letter-spacing:.16em;
  text-transform:uppercase;color:var(--faint);font-weight:400;margin:0 0 14px;
  padding-bottom:9px;border-bottom:1px solid var(--rule);display:flex;
  justify-content:space-between;gap:16px}
.note{font-size:14px;color:var(--soft);margin:0;max-width:66ch;text-wrap:pretty}
.comp{margin:0;padding:0;list-style:none;counter-reset:comp}
.comp li{counter-increment:comp;display:grid;grid-template-columns:26px 1fr;gap:10px;
  max-width:70ch;padding:11px 0;border-bottom:1px solid var(--hair);align-items:baseline}
.comp li:last-child{border-bottom:none}
.comp li::before{content:counter(comp);font-family:var(--fm);font-size:12px;
  color:var(--faint)}
.comp li>div{grid-column:2}
.comp-t{font-size:14.5px;font-weight:500}
.comp-t:hover{color:var(--open)}
.comp li .chip{margin-left:8px}
.comp-d{grid-column:2;margin:5px 0 0;font-size:13.5px;color:var(--soft);text-wrap:pretty}
.tree{margin-top:30px;position:relative}
.tlayer{position:relative;padding:0 0 26px 26px;border-left:1px solid var(--rule)}
.tlayer:last-child{border-left-color:transparent}
.tlayer::before{content:"";position:absolute;left:-5px;top:6px;width:9px;height:9px;
  border-radius:50%;background:var(--rule)}
.thead{display:flex;align-items:baseline;gap:12px;margin:0 0 12px}
.tlv{font-family:var(--fm);font-size:10.5px;letter-spacing:.16em;text-transform:uppercase;
  color:var(--faint)}
.tcnt{font-family:var(--fm);font-size:10.5px;color:var(--faint);letter-spacing:.06em}
.tnodes{display:grid;grid-template-columns:repeat(auto-fill,minmax(186px,1fr));gap:8px}
.tnode{display:block;padding:9px 12px;border-radius:9px;background:var(--card);
  border:1px solid var(--rule);transition:border-color .16s,transform .16s}
.tnode:hover{border-color:var(--open);transform:translateY(-1px);color:inherit}
.tid{display:block;font-family:var(--fm);font-size:10.5px;color:var(--faint)}
.tnm{display:block;font-size:13.5px;margin-top:2px;text-wrap:pretty}
.tdom{display:block;font-size:11.5px;color:var(--faint);margin-top:3px}
.tnode.p{border-color:color-mix(in srgb,var(--proved) 50%,var(--rule))}
.tnode.p .tid{color:var(--proved)}
.tnode.o{border-color:color-mix(in srgb,var(--open) 42%,var(--rule))}
.tnode.o .tid{color:var(--open)}
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(232px,1fr));gap:12px}
.tile{display:block;padding:16px 17px 15px;background:var(--card);
  border:1px solid var(--rule);border-radius:12px;transition:border-color .16s,transform .16s}
.tile:hover{border-color:var(--open);transform:translateY(-1px);color:inherit}
.tile h3{font-family:var(--fd);font-size:17px;font-weight:500;margin:0}
.tile p{margin:6px 0 0;font-size:13px;color:var(--soft);line-height:1.5;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;
  overflow:hidden;min-height:calc(1.5em * 2)}
.tile .foot{margin-top:13px;font-family:var(--fm);font-size:11px;color:var(--faint)}
.meter{display:flex;gap:3px;margin-top:11px}
.meter i{height:4px;flex:1;border-radius:2px;background:var(--hair)}
.meter i.p{background:var(--proved)}
.meter i.o{background:var(--open)}
.meter i.s{background:var(--shut);opacity:.5}
.stats{display:flex;flex-wrap:wrap;gap:30px;margin:26px 0 0}
.stat b{display:block;font-family:var(--fd);font-size:30px;font-weight:500;line-height:1.1}
.stat span{font-family:var(--fm);font-size:10.5px;letter-spacing:.12em;
  text-transform:uppercase;color:var(--faint)}
.stat.p b{color:var(--proved)}
.stat.o b{color:var(--open)}
.rows{display:flex;flex-direction:column}
.row{display:grid;grid-template-columns:50px 1fr 92px 104px;gap:12px;align-items:center;
  padding:11px 8px;margin:0 -8px;border-bottom:1px solid var(--hair);border-radius:6px}
.row:hover{background:var(--hair);color:inherit}
.row .id{font-family:var(--fm);font-size:11.5px;color:var(--faint)}
.row.p .id,.row.p .st{color:var(--proved)}
.row.o .id,.row.o .st{color:var(--open)}
.row .nm{font-size:14.5px}
.row .nm em{display:block;font-style:normal;font-size:12.5px;color:var(--soft)}
.row .st{font-family:var(--fm);font-size:10.5px;letter-spacing:.09em;
  text-transform:uppercase;color:var(--faint);text-align:right}
.steps{display:flex;gap:2px}
.steps i{width:13px;height:13px;border-radius:3px;background:var(--hair);
  border:1px solid transparent}
.steps i.k{background:transparent;border-color:var(--proved)}
.steps i.v{background:var(--proved)}
.steps i.t{border-color:var(--faint);border-style:dashed}
.gauge{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:14px;
  margin:24px 0 0}
.gau{padding:15px 16px;border-radius:12px;background:var(--card);border:1px solid var(--rule)}
.gau h4{margin:0;font-family:var(--fm);font-size:10.5px;letter-spacing:.13em;
  text-transform:uppercase;color:var(--faint);font-weight:400}
.gau .val{font-family:var(--fd);font-size:27px;font-weight:500;line-height:1.2;margin-top:3px}
.gau .val small{font-size:15px;color:var(--faint)}
.gau p{margin:8px 0 0;font-size:12.5px;color:var(--soft);line-height:1.5}
.gau p.warn{color:var(--ink);border-top:1px solid var(--hair);padding-top:7px}
.rolebar{display:flex;justify-content:space-between;gap:18px;align-items:flex-start;
  margin-top:18px;padding:14px 16px;border-radius:12px;background:var(--card);
  border:1px solid var(--rule);text-decoration:none;color:inherit}
.rolebar b{font-family:var(--fd);font-size:17px;font-weight:500;display:block}
.rolebar span{font-size:12.5px;color:var(--soft);display:block;margin-top:3px}
.rolebar em{font-style:normal;font-size:12px;color:var(--faint);max-width:38ch;
  text-align:right;line-height:1.45}
.nowbook{display:flex;flex-wrap:nowrap;gap:14px;align-items:center;padding:12px 14px;
  border-radius:12px;background:var(--card);border:1px solid var(--rule);
  text-decoration:none;color:inherit}
.nowbook>div{min-width:0}
.nowbook img{width:44px;height:64px;object-fit:cover;border-radius:4px;flex:none}
.nowbook .nc{width:44px;height:64px;display:grid;place-items:center;flex:none;
  border-radius:4px;background:var(--wash-o);font-family:var(--fd);font-size:20px;
  color:var(--soft)}
.nowbook .cv{flex:none;display:block}
.nowbook .ttl{font-family:var(--fd);font-size:16px;font-weight:500;display:block;
  color:inherit;text-decoration:none}
.nowbook span{display:block;font-size:12.5px;color:var(--soft);margin-top:2px}
.nowbook .lifts{margin-top:5px;font-size:12px;white-space:normal}
.nowbook .lifts a{color:var(--open);text-decoration:none}
.take{padding:15px 17px;border-radius:12px;background:var(--card);
  border:1px solid var(--rule)}
.take .head{display:flex;gap:10px;align-items:baseline;text-decoration:none;color:inherit}
.take .head .id{font-family:var(--fm);font-size:11px;color:var(--faint)}
.take .head b{font-family:var(--fd);font-size:18px;font-weight:500}
.take .head em{font-style:normal;font-size:11.5px;color:var(--faint);margin-left:auto}
.take p{margin:10px 0 0;font-size:14px;line-height:1.6;color:var(--ink);max-width:70ch}
.take .src{display:inline-block;margin-top:11px;font-size:13px;color:var(--open);
  text-decoration:none;border-bottom:1px solid var(--wash-o)}
.take .alt{margin-top:10px;font-size:12.5px;color:var(--soft)}
.take .alt a{color:var(--soft)}
.who{margin:12px 0 0;font-size:14.5px;color:var(--ink);max-width:60ch;
  padding-left:12px;border-left:2px solid var(--wash-p)}
.gau .steps{margin-top:11px}
.ladder{display:flex;flex-direction:column;gap:1px}
.rung{display:grid;grid-template-columns:162px 1fr;gap:16px;padding:12px 14px;
  border-radius:9px;align-items:baseline}
.rung .lb{font-family:var(--fm);font-size:11.5px;color:var(--faint)}
.rung .tx{font-size:14.5px;max-width:62ch;text-wrap:pretty}
.rung.reached{background:var(--wash-p)}
.rung.reached .lb{color:var(--proved)}
.rung.goal .lb::after{content:" · цель";color:var(--faint)}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(216px,1fr));gap:10px}
.mini{display:block;padding:12px 14px;border-radius:10px;background:var(--card);
  border:1px solid var(--rule)}
.mini:hover{border-color:var(--open);color:inherit}
.mini .id{font-family:var(--fm);font-size:11px;color:var(--faint)}
.mini .nm{font-size:14px;margin-top:2px}
.mini .cond{font-family:var(--fm);font-size:11px;margin-top:7px;color:var(--faint)}
.mini.done{border-color:color-mix(in srgb,var(--proved) 45%,var(--rule))}
.mini.done .cond,.mini.done .id{color:var(--proved)}
.brow{display:grid;grid-template-columns:28px 34px 1fr 168px;gap:12px;align-items:center;
  padding:10px 8px;margin:0 -8px;border-bottom:1px solid var(--hair);border-radius:6px}
.brow:hover{background:var(--hair);color:inherit}
.brow .d{font-family:var(--fm);font-size:12px;color:var(--proved)}
.brow .t{font-size:14.5px;text-wrap:pretty}
.brow .t em{display:block;font-style:normal;font-size:12.5px;color:var(--soft)}
.brow .m{font-family:var(--fm);font-size:10.5px;color:var(--faint);text-align:right;
  letter-spacing:.06em}
.cov-s{width:28px;height:40px;object-fit:cover;border-radius:2px;background:var(--hair);
  display:flex;align-items:center;justify-content:center;font-size:12px;color:var(--faint);
  font-family:var(--fd)}
.cov-l{width:132px;height:188px;object-fit:cover;border-radius:6px;background:var(--hair);
  display:flex;align-items:center;justify-content:center;font-size:40px;color:var(--faint);
  font-family:var(--fd);flex:none;box-shadow:0 4px 16px rgba(0,0,0,.14)}
.cov-l.ph,.cov-s.ph{border:1px solid var(--rule)}
.bk-head{display:flex;gap:22px;align-items:flex-start}
.bk-head>div{min-width:0}
@media (max-width:560px){.bk-head{flex-wrap:wrap}}
.facts{margin:6px 0 0;font-family:var(--fm);font-size:11.5px;color:var(--faint);
  letter-spacing:.04em}
.bk-about{margin:26px 0 0;font-size:15px;line-height:1.65;color:var(--ink);
  max-width:68ch;text-wrap:pretty}
.ev{padding:15px 17px;border-radius:11px;background:var(--wash-p);margin-bottom:9px;
  border:1px solid color-mix(in srgb,var(--proved) 30%,transparent)}
.ev .res{font-size:14.5px;text-wrap:pretty}
.ev .meta{font-family:var(--fm);font-size:11px;color:var(--soft);margin-top:8px}
.empty{padding:15px 17px;border-radius:11px;border:1px dashed var(--rule);font-size:14px;
  color:var(--soft);max-width:66ch;text-wrap:pretty}
.bar{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 16px}
.bar button{font:inherit;font-size:12.5px;padding:6px 13px;border-radius:20px;
  cursor:pointer;color:var(--soft);background:var(--card);border:1px solid var(--rule)}
.bar button.on{color:var(--ink);border-color:var(--faint);background:var(--hair)}
@media (min-width:860px){.split{display:grid;grid-template-columns:1fr 250px;gap:40px;
  align-items:start}}
.rail{font-size:13px;color:var(--soft)}
.rail dl{margin:0;display:grid;grid-template-columns:1fr;gap:12px}
.rail dt{font-family:var(--fm);font-size:10px;letter-spacing:.13em;
  text-transform:uppercase;color:var(--faint)}
.rail dd{margin:2px 0 0;color:var(--ink);font-size:13.5px}
.rail dd.path{font-family:var(--fm);font-size:11.5px;overflow-wrap:anywhere}
.foot{margin-top:56px;padding-top:16px;border-top:1px solid var(--rule);font-size:12.5px;
  color:var(--faint);max-width:74ch;font-family:var(--fm);line-height:1.7}
.more{margin-top:14px;font-size:13px;color:var(--soft)}
.more button{font:inherit;font-size:13px;color:var(--open);background:none;border:0;
  cursor:pointer;padding:0;border-bottom:1px dashed currentColor}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
"""

SCRIPT = r"""
const W = window.WH;
const S = W.skills, B = W.books, D = W.domains, T = W.totals;
const esc = (s) => String(s == null ? "" : s)
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;");

// «В каком порядке брать» и «Чем закрывается» — проза, а не список: там, где
// агент назвал навык кодом в обратных кавычках, а не по-русски рядом, код без
// перевода так и остаётся адресом для машины на странице для человека.
// Оставлять нечитаемым нельзя, переводить в имя рискованно — регистр падежа
// в русском тексте вокруг кода непредсказуем; ссылка кодом решает оба: кликнуть
// можно, а само слово в тексте не врёт придуманным склонением.
function linkCodes(text) {
  return esc(text).replace(/`([A-ZА-Я]{1,3}\d{1,2})`/g, (m, id) =>
    '<a class="code-link" href="#/skill/' + id.toLowerCase() + '">' + id + "</a>");
}

// Русское число согласуется с существительным: «4 навыков» человек читает как
// ошибку раньше, чем как данные.
function plural(n, one, few, many) {
  const a = Math.abs(n) % 100, b = a % 10;
  if (a > 10 && a < 20) return n + " " + many;
  if (b > 1 && b < 5) return n + " " + few;
  if (b === 1) return n + " " + one;
  return n + " " + many;
}

const domRu = (k) => (D[k] && D[k].ru) || W.domRu[k] || k;
const stateCls = (base, st) =>
  base + (st === "доказан" ? " p" : st === "открыт" ? " o" : "");

// Пять клеток шкалы: заполненная — доказанный уровень, обведённая — изученный,
// пунктирная — цель. Две мерки видны рядом и при этом не смешиваются.
function steps(proved, known, target) {
  let out = "";
  for (let i = 1; i <= 5; i++) {
    const c = i <= proved ? "v" : i <= known ? "k" : i === target ? "t" : "";
    out += '<i class="' + c + '"></i>';
  }
  return '<span class="steps">' + out + "</span>";
}

function skillRow(s, tail) {
  const dom = s.domains.map(domRu).join(" · ");
  return '<a class="' + stateCls("row", s.state) + '" href="#/skill/' +
    s.id.toLowerCase() + '"><span class="id">' + s.id + '</span><span class="nm">' +
    esc(s.ru) + "<em>" + esc(dom) + "</em></span>" +
    steps(s.proved, s.known, s.target) +
    '<span class="st">' + esc(tail || s.state) + "</span></a>";
}

// Обложка встроена в саму страницу base64 — не ссылкой на чужой сервис: атлас
// обещан самодостаточным, ссылка это обещание рвёт молча при чтении офлайн.
// Нет обложки — плашка с первой буквой названия, ряд книг не превращается в
// ряд дыр разной ширины.
function coverImg(b, cls) {
  if (b.cover) {
    return '<img class="' + cls + '" loading="lazy" alt="" src="' +
      "data:image/jpeg;base64," + b.cover + '">';
  }
  const ch = esc((b.title.replace(/[«»"]/g, "")[0] || "?").toUpperCase());
  return '<span class="' + cls + ' ph">' + ch + "</span>";
}

function bookRow(b, depth) {
  const top = depth != null ? depth
    : b.skills.length ? Math.max.apply(null, b.skills.map((x) => x[1])) : 0;
  const marks = [];
  if (b.owned) marks.push("на руках");
  if (b.set) marks.push("в наборе");
  if (b.status === "read") marks.push("прочитано");
  return '<a class="brow" href="#/reading/' + encodeURIComponent(b.slug) +
    '">' + coverImg(b, "cov-s") + '<span class="d">' + (top || "—") +
    '</span><span class="t">' + esc(b.title) +
    "<em>" + esc(b.author || "автор не назван") + "</em></span>" +
    '<span class="m">' + esc(marks.join(" · ") || b.tier) + "</span></a>";
}

function section(title, right, body) {
  return '<div class="sect"><h2><span>' + esc(title) + "</span><span>" +
    esc(right) + "</span></h2>" + body + "</div>";
}

// Длинные списки режутся не молча: сколько скрыто, написано на кнопке. Тихое
// «первые двадцать» читается как «это всё», и человек делает вывод по обрезку.
let expanded = {};
function capped(key, items, render, step) {
  step = step || 25;
  const open = expanded[key];
  const shown = open ? items : items.slice(0, step);
  let out = shown.map(render).join("");
  if (!open && items.length > step) {
    out += '<p class="more"><button data-more="' + esc(key) + '">показать все ' +
      items.length + " · сейчас видно " + step + "</button></p>";
  }
  return out;
}

function mapPage() {
  const tiles = Object.values(D).map((d) => {
    let cells = "";
    for (let i = 0; i < d.proved; i++) cells += '<i class="p"></i>';
    for (let i = 0; i < d.open; i++) cells += '<i class="o"></i>';
    for (let i = 0; i < d.shut; i++) cells += '<i class="s"></i>';
    return '<a class="tile" href="#/domain/' + d.key + '"><h3>' + esc(d.ru) +
      "</h3><p>" + esc(d.teaser || d.about) + '</p><div class="meter">' + cells +
      '</div><div class="foot">' +
      plural(d.skills.length, "навык", "навыка", "навыков") + " · " +
      plural(d.books.length, "книга", "книги", "книг") +
      " · доказано " + d.proved + "</div></a>";
  }).join("");

  return '<div class="crumb"><a href="#/">склад GSWorkHUB</a><span>/</span>' +
    "<span>карта</span></div><h1>Карта развития</h1>" +
    '<p class="lede">' + plural(T.skills, "навык", "навыка", "навыков") +
    " с описанными ступенями, ресурсы, которые их поднимают, и доказательства, " +
    "которые их подтверждают. Прочитанное поднимает изученность; владение " +
    "поднимает только сделанная работа.</p>" +
    '<div class="stats">' +
    '<div class="stat p"><b>' + T.proved + "</b><span>доказано работой</span></div>" +
    '<div class="stat o"><b>' + T.open + "</b><span>открыто к взятию</span></div>" +
    '<div class="stat"><b>' + T.set + "</b><span>книг в наборе</span></div>" +
    '<div class="stat"><b>' + T.owned + "</b><span>на руках</span></div></div>" +
    section("Дисциплины", plural(Object.keys(D).length, "дисциплина", "дисциплины",
      "дисциплин"), '<div class="tiles">' + tiles + "</div>");
}

function home() {
  // Роль коротко. Веса нужны раньше списка: они задают приоритет сортировки.
  const buildEarly = (W.builds || []).filter((x) => x.status === "active")[0];
  const wEarly = {};
  if (buildEarly) buildEarly.rows.forEach((r) => { wEarly[r.id] = r.weight; });
  // Порядок: сначала то, что весит в роли, потом — от простого к сложному.
  // «Проще» значит ближе к основанию графа: меньше слоёв предпосылок под собой
  // и ниже целевая ступень. Так список читается как очередь, а не как витрина.
  const open = Object.values(S)
    .filter((s) => s.state === "открыт" || s.state === "доказан")
    .sort((a, b) =>
      (wEarly[b.id] || 0) - (wEarly[a.id] || 0) ||
      (a.layer || 0) - (b.layer || 0) ||
      a.target - b.target ||
      a.id.localeCompare(b.id));

  // Роль коротко. Веса нужны и здесь: они решают, что предложить взять первым.
  const build = buildEarly, weight = wEarly;
  const who = W.whoami
    ? '<p class="who">' + esc(W.whoami) + "</p>" : "";
  const role = build
    ? '<a class="rolebar" href="#/build"><div><b>' + esc(build.title) + "</b>" +
      "<span>" + plural(build.rows.length, "требование", "требования", "требований") +
      " · владение " + Math.round(build.mastery * 100) + "% · теория " +
      Math.round(build.theory * 100) + "%</span></div>" +
      "<em>Владение поднимается только доказанной работой, поэтому у роли оно " +
      "растёт последним — когда основание уже собрано.</em></a>"
    : "";

  // Что читается прямо сейчас. Пустая полка — тоже ответ, и его лучше сказать.
  const now = Object.values(B).filter((x) => x.status === "reading");
  const nowBlock = now.length
    ? now.map((b) => {
        const top = b.skills.filter((m) => S[m[0]]).slice(0, 3)
          .map((m) => '<a href="#/skill/' + m[0].toLowerCase() + '">' + m[0] + " " +
            esc(S[m[0]].ru) + "</a>").join(" · ");
        return '<div class="nowbook">' +
          '<a class="cv" href="#/reading/' + b.slug + '">' +
          (b.cover ? '<img src="data:image/jpeg;base64,' + b.cover + '" alt="">'
                   : '<span class="nc">' + esc((b.title || "?").slice(0, 1)) + "</span>") +
          "</a><div>" +
          '<a class="ttl" href="#/reading/' + b.slug + '">' + esc(b.title) + "</a>" +
          "<span>" + esc(b.author || "") + "</span>" +
          (top ? '<span class="lifts">поднимает: ' + top + "</span>" : "") +
          "</div></div>";
      }).join("")
    : '<p class="note">Сейчас ничего не читается. Возьми что-нибудь из набора — ' +
      "или из того, что предложено ниже.</p>";

  // Что взять сейчас: из открытых берём тот, что весит в роли больше прочих;
  // при равном весе — тот, который открывает больше следующих навыков.
  const pick = open.slice().sort((a, b) =>
    (weight[b.id] || 0) - (weight[a.id] || 0) ||
    b.opens.length - a.opens.length || a.id.localeCompare(b.id))[0];
  const alt = open.slice().sort((a, b) =>
    (weight[b.id] || 0) - (weight[a.id] || 0) ||
    b.opens.length - a.opens.length || a.id.localeCompare(b.id))[1];
  const res = pick
    ? pick.books.map((x) => B[x[0]]).filter(Boolean)
        .sort((a, b) => (b.set | 0) - (a.set | 0) || (b.owned | 0) - (a.owned | 0))[0]
    : null;
  const takeBlock = pick
    ? '<div class="take"><a class="head" href="#/skill/' + pick.id.toLowerCase() +
      '"><span class="id">' + pick.id + "</span><b>" + esc(pick.ru) + "</b>" +
      "<em>" + (weight[pick.id]
        ? "требование роли — и единственное из них, что сейчас открыто"
        : "открывает следующих: " + pick.opens.length) + "</em></a>" +
      (pick.first ? "<p>" + esc(pick.first) + "</p>" : "") +
      (res ? '<a class="src" href="#/reading/' + res.slug + '">чем закрывать: ' +
        esc(res.title) + "</a>" : "") +
      (alt ? '<p class="alt">Не идёт — возьми <a href="#/skill/' +
        alt.id.toLowerCase() + '">' + alt.id + " " + esc(alt.ru) + "</a>: " +
        (weight[alt.id] ? "тоже требование роли."
          : "не в роли, но открывает " + alt.opens.length + " следующих.") + "</p>" : "") +
      "</div>"
    : '<p class="note">Открытых навыков нет: сначала нужны доказательства по тем, ' +
      "что стоят перед ними.</p>";

  return '<div class="crumb"><span>склад GSWorkHUB</span></div>' +
    "<h1>Развитие</h1>" + who + role +
    section("Читаю сейчас", now.length ? "" : "полка пуста", nowBlock) +
    section("Взять сейчас", pick ? "первый шаг на полчаса" : "", takeBlock) +
    section("Открыто к взятию", plural(open.length, "навык", "навыка", "навыков"),
      '<p class="note">Предпосылки пройдены — за навык можно браться. ' +
      'Устройство карты целиком — <a href="#/map">на отдельной странице</a>.</p>' +
      '<div class="rows" style="margin-top:14px">' +
      capped("home-open", open, (s) => skillRow(s)) + "</div>");
}

function domainPage(key) {
  const d = D[key];
  if (!d) return lost("Дисциплины «" + key + "» в складе нет.");
  const rows = d.skills.map((x) => skillRow(S[x])).join("");
  const books = d.books.map((x) => {
    const b = B[x];
    if (!b) return "";
    const own = b.skills.filter((m) => d.skills.indexOf(m[0]) >= 0);
    return bookRow(b, own.length ? Math.max.apply(null, own.map((m) => m[1])) : 0);
  });
  return '<div class="crumb"><a href="#/">развитие</a><span>/</span>' +
    "<span>дисциплина</span></div><h1>" + esc(d.ru) + '</h1><p class="lede">' +
    esc(d.about) + "</p>" +
    '<div class="stats"><div class="stat p"><b>' + d.proved +
    "</b><span>доказано</span></div>" +
    '<div class="stat o"><b>' + d.open + "</b><span>открыто</span></div>" +
    '<div class="stat"><b>' + d.shut + "</b><span>закрыто</span></div>" +
    '<div class="stat"><b>' + d.books.length + "</b><span>книг поднимают</span></div></div>" +
    (d.composition.items.length ? section("Из чего состоит",
      plural(d.composition.items.length, "навык", "навыка", "навыков"),
      (d.composition.intro ? '<p class="note" style="margin-bottom:14px">' +
        esc(d.composition.intro) + "</p>" : "") +
      '<ol class="comp">' + d.composition.items.map((x) => "<li><div>" +
        (x.id
          ? '<a class="comp-t" href="#/skill/' + x.id.toLowerCase() + '">' +
            esc(x.title) + "</a>" + (x.note ? '<span class="chip">' + esc(x.note) +
            "</span>" : "")
          : "") +
        '</div><p class="comp-d">' + esc(x.desc) + "</p></li>").join("") +
      "</ol>") : "") +
    (d.order_hint ? section("В каком порядке брать", "", '<p class="note">' +
      linkCodes(d.order_hint) + "</p>") : "") +
    section("Навыки", plural(d.skills.length, "навык", "навыка", "навыков"),
      '<div class="rows">' + rows + "</div>") +
    (d.guests.length ? section("Нужны и здесь",
      plural(d.guests.length, "навык", "навыка", "навыков"),
      '<p class="note">Эти навыки живут в других дисциплинах, но без них здесь ' +
      "не обойтись. В счёт дисциплины они не идут — иначе одна доказанная работа " +
      'зажигала бы несколько полосок сразу.</p><div class="rows" ' +
      'style="margin-top:14px">' +
      d.guests.map((x) => skillRow(S[x])).join("") + "</div>") : "") +
    section("Чем поднимать", plural(d.books.length, "книга", "книги", "книг"),
      '<div class="rows">' + capped("dom-" + key, books, (x) => x) + "</div>") +
    (d.closure ? section("Чем закрывается", "", '<p class="note">' +
      linkCodes(d.closure) + "</p>") : "");
}

function skillPage(id) {
  const s = S[id];
  if (!s) return lost("Навыка " + id + " в складе нет.");
  const nodeRu = s.node === "meta" ? "мета-узел"
    : s.node === "compound" ? "составной" : "простой";
  const chips = s.domains.map((d) =>
    '<a class="chip" href="#/domain/' + d + '">' + esc(domRu(d)) + "</a>").join("") +
    '<span class="chip">' + nodeRu + "</span>";
  const ladder = s.levels.map((l) => {
    let cls = "rung";
    if (l[0] <= s.proved) cls += " reached";
    if (l[0] === s.target) cls += " goal";
    return '<div class="' + cls + '"><span class="lb">L' + l[0] + " · " + esc(l[1]) +
      '</span><span class="tx">' + esc(l[2]) + "</span></div>";
  }).join("");
  const req = s.requires.map((r) =>
    '<a class="mini' + (r[3] ? " done" : "") + '" href="#/skill/' + r[0].toLowerCase() +
    '"><span class="id">' + r[0] + '</span><div class="nm">' + esc(r[2]) +
    '</div><div class="cond">нужен уровень ' + r[1] + (r[3] ? " — есть" : "") +
    "</div></a>").join("");
  const opens = s.opens.map((o) =>
    '<a class="mini" href="#/skill/' + o[0].toLowerCase() + '"><span class="id">' +
    o[0] + '</span><div class="nm">' + esc(o[1]) + '</div><div class="cond">просит ' +
    "уровень " + o[2] + "</div></a>").join("");
  const books = s.books.filter((x) => B[x[0]]).map((x) => bookRow(B[x[0]], x[1]));
  const reach = s.books.reduce((m, x) => (x[1] > m ? x[1] : m), 0);
  const ev = s.evidence.map((e) =>
    '<div class="ev"><div class="res">' + esc(e[0]) + '</div><div class="meta">уровень ' +
    e[1] + " · " + esc(e[2]) + "</div></div>").join("");
  // Не «нет намеренно»: у двадцати восьми составных узлов из тридцати одного
  // ресурсы размечены. Сказать про оставшиеся, что так и задумано, значит выдать
  // несделанную работу за замысел — читатель перестанет искать то, что стоило бы
  // найти.
  const noBooksWhy = s.node === "atomic"
    ? "Ни один ресурс каталога пока не размечен под этот навык."
    : "Ресурсов под этот навык пока не размечено. Он собирается из тех, что стоят " +
      "перед ним, и берётся прежде всего работой — но и читать под него есть что.";
  const dom0 = s.domains[0] || "";
  return '<div class="crumb"><a href="#/">развитие</a><span>/</span>' +
    '<a href="#/domain/' + dom0 + '">' + esc(domRu(dom0)) + "</a><span>/</span><span>" +
    s.id + '</span></div><div class="split"><div>' +
    "<h1>" + esc(s.ru) + '</h1><p class="en">' + esc(s.en) + '</p>' +
    '<p class="lede">' + linkCodes(s.about) + '</p><div class="chips">' + chips + "</div>" +
    '<div class="gauge"><div class="gau"><h4>Владение</h4><div class="val">' + s.proved +
    "<small> / " + s.target + "</small></div>" + steps(s.proved, 0, s.target) + "<p>" +
    (s.proved > 0 ? "Поднимается только сделанной работой с названным результатом."
      : "Ни одной работы, где навык применён. Пока это ноль, а не «мало».") +
    '</p></div><div class="gau"><h4>Изученность</h4><div class="val">' + s.known +
    "<small> / " + s.target + "</small></div>" + steps(0, s.known, s.target) + "<p>" +
    (s.known > 0 ? "Наибольшая глубина среди прочитанных книг. Глубины не складываются."
      : "Ни одна из книг под этот навык ещё не прочитана.") +
    // Потолок каталога, а не прочитанного. Без него страница показывает ноль
    // изученности и полку ресурсов рядом — и умалчивает, что полка до цели не
    // достаёт. Это разные новости: «ещё не читал» и «читать нечего».
    (reach && reach < s.target
      ? '</p><p class="warn">Прочитав всё размеченное, дойдёшь до ' + reach +
        ". Источника на ступень " + s.target + " под этот навык нет."
      : "") + "</p></div></div>" +
    section("Ступени", s.levels.length
      ? "L" + s.levels[0][0] + " – L" + s.levels[s.levels.length - 1][0] : "нет",
      '<div class="ladder">' + ladder + "</div>") +
    section("Что нужно до", s.requires.length
      ? s.requires.filter((r) => r[3]).length + " из " + s.requires.length + " пройдено"
      : "ничего",
      s.requires.length ? '<div class="cards">' + req + "</div>"
        : '<p class="note">Ничего не требует — с него можно начинать.</p>') +
    (s.opens.length ? section("Что откроет",
      plural(s.opens.length, "навык", "навыка", "навыков"),
      '<div class="cards">' + opens + "</div>") : "") +
    section("Чем поднять изученность", books.length
      ? plural(books.length, "книга", "книги", "книг") : "нет",
      books.length ? '<div class="rows">' + capped("sk-" + s.id, books, (x) => x) + "</div>"
        : '<p class="empty">' + noBooksWhy + "</p>") +
    section("Доказательства", s.evidence.length
      ? plural(s.evidence.length, "запись", "записи", "записей") : "нет",
      s.evidence.length ? ev : '<p class="empty">' + linkCodes(s.proof) + "</p>") +
    '</div><aside class="rail"><dl>' +
    "<div><dt>Состояние</dt><dd>" + esc(s.state) + "</dd></div>" +
    "<div><dt>Цель</dt><dd>уровень " + s.target + " из 5</dd></div>" +
    "<div><dt>Тип узла</dt><dd>" + nodeRu +
    (s.node === "meta" ? " — нижних ступеней нет, проверяется с третьей"
      : s.node === "compound" ? " — собирается из нескольких простых"
        : " — берётся напрямую") + "</dd></div>" +
    "</dl></aside></div>";
}

function bookPage(key) {
  const b = B[key];
  if (!b) return lost("Такого ресурса в складе нет.");
  const chips = [];
  if (b.set) chips.push("рабочий набор");
  if (b.owned) chips.push("на руках");
  if (b.tier) chips.push("ярус: " + b.tier);
  chips.push(W.kindRu[b.kind] || b.kind);
  const rows = b.skills.filter((m) => S[m[0]]).map((m) => {
    const s = S[m[0]];
    return '<a class="' + stateCls("row", s.state) + '" href="#/skill/' +
      s.id.toLowerCase() + '"><span class="id">' + s.id + '</span><span class="nm">' +
      esc(s.ru) + "<em>" + esc(s.domains.map(domRu).join(" · ")) + "</em></span>" +
      steps(s.proved, m[1], s.target) +
      '<span class="st">до ' + m[1] + " · цель " + s.target + "</span></a>";
  }).join("");
  // Выходные данные — одной строкой под автором: год и объём отвечают на
  // «когда написано» и «во что я ввязываюсь», а порознь каждое из них не
  // стоит собственной строки в правой колонке.
  const facts = [];
  if (b.year) facts.push(b.year + " год");
  if (b.pages) facts.push(plural(b.pages, "страница", "страницы", "страниц"));
  facts.push(W.kindRu[b.kind] || b.kind);
  // Дисциплины книги считаются из её навыков, а не из поля `topics`: то поле —
  // раздел присланной библиотеки, отдельный словарь на русском («системное-
  // мышление»), который с двенадцатью дисциплинами графа не сходится ни ключом,
  // ни составом. Показать его под именем «дисциплины» значило бы выдать раздел
  // чужого списка за наш собственный разбор.
  const domSet = [];
  b.skills.forEach((m) => (S[m[0]] ? S[m[0]].domains : []).forEach((d) => {
    if (domSet.indexOf(d) < 0) domSet.push(d);
  }));
  const topics = domSet.map((d) =>
    '<a class="chip" href="#/domain/' + d + '">' + esc(domRu(d)) + "</a>").join("");
  return '<div class="crumb"><a href="#/">развитие</a><span>/</span>' +
    '<a href="#/books">Ресурсы</a><span>/</span><span>' +
    esc(W.kindRu[b.kind] || b.kind) + "</span></div>" +
    '<div class="split"><div><div class="bk-head">' + coverImg(b, "cov-l") +
    "<div><h1>" + esc(b.title) + '</h1><p class="lede">' +
    esc(b.author || "автор не назван") + '</p><p class="facts">' +
    esc(facts.join(" · ")) + '</p><div class="chips">' +
    chips.map((c) => '<span class="chip">' + esc(c) + "</span>").join("") +
    "</div></div></div>" +
    (b.about
      ? '<p class="bk-about">' + esc(b.about) + "</p>"
      : '<p class="bk-about empty">Описания нет: карточка заведена из списка ' +
        "и содержание книги в ней не записано.</p>") +
    (topics ? section("Дисциплины", "", '<div class="chips">' + topics + "</div>") : "") +
    section("Что поднимает", b.skills.length
      ? plural(b.skills.length, "навык", "навыка", "навыков") : "нет разметки",
      b.skills.length
        ? '<p class="note">Глубина — насколько далеко книга заводит по ступеням ' +
          "навыка. Она поднимает изученность, а не владение: владение появляется от " +
          'сделанной работы.</p><div class="rows" style="margin-top:14px">' + rows + "</div>"
        : '<p class="empty">Ресурс ещё не размечен по навыкам — из каталога видно ' +
          "только название и автора. Размечается рабочий набор, остальное остаётся " +
          "каталогом, из которого его выбирают.</p>") +
    '</div><aside class="rail"><dl>' +
    "<div><dt>Статус</dt><dd>" + esc(W.statusRu[b.status] || b.status) + "</dd></div>" +
    "<div><dt>Ярус</dt><dd>" + esc(b.tier || "не назначен") + "</dd></div>" +
    (b.url ? '<div><dt>Где взять</dt><dd class="path"><a href="' + esc(b.url) +
      '">' + esc(b.url) + "</a></dd></div>" : "") +
    "</dl></aside></div>";
}

// Профиль: готовность к выбранной роли. Игровой слой был остановлен намеренно —
// в источнике записано «XP не повышает Skill, Achievement не повышает Skill».
// Поэтому здесь нет ни очков, ни уровней персонажа: только два числа, которые
// считаются из фактов склада, и оба честно показывают ноль, когда он ноль.
function buildPage() {
  const list = W.builds || [];
  if (!list.length) return lost("Целевой профиль в складе не заведён.");
  return list.map((b) => {
    const rows = b.rows.map((r) => {
      const pct = Math.round(100 * Math.min(r.proved / r.need, 1));
      return '<a class="' + stateCls("row", r.state) + '" href="#/skill/' +
        r.id.toLowerCase() + '"><span class="id">' + r.id + '</span>' +
        '<span class="nm">' + esc(r.ru) + "<em>вес " + r.weight + " · нужен уровень " +
        r.need + "</em></span>" + steps(r.proved, r.known, r.need) +
        '<span class="st">' + pct + "%</span></a>";
    }).join("");
    return '<div class="crumb"><a href="#/">развитие</a><span>/</span>' +
      "<span>профиль</span></div><h1>" + esc(b.title) + "</h1>" +
      '<p class="lede">' + esc(b.about) + "</p>" +
      '<div class="gauge"><div class="gau"><h4>Готовность к роли</h4>' +
      '<div class="val">' + b.mastery + "<small> %</small></div>" +
      "<p>Доля требований, закрытых доказанной работой, с учётом веса. Избыток " +
      "в одном навыке не закрывает пробел в другом: каждое требование считается " +
      "с потолком в сто процентов.</p></div>" +
      '<div class="gau"><h4>Покрытие теорией</h4>' +
      '<div class="val">' + b.theory + "<small> %</small></div>" +
      "<p>Сколько из требуемой глубины закрыто прочитанными книгами. Это другое " +
      "число, и с готовностью оно не складывается.</p></div></div>" +
      section("Путь к профилю", "", '<p class="note">В основании профиля ' +
        plural(b.base, "навык", "навыка", "навыков") + " — сами требования и всё, " +
        "что ведёт к ним по предпосылкам. Доказано работой " + b.baseProved +
        ", открыто к взятию " + b.baseOpen + ". Требования стоят на вершинах " +
        "графа, поэтому готовность остаётся нулевой дольше всего — она поднимается " +
        "последней, когда основание уже собрано.</p>") +
      section("Требования", plural(b.rows.length, "навык", "навыка", "навыков"),
        '<div class="rows">' + rows + "</div>") +
      section("Когда профиль достигнут", "", '<p class="note">' +
        (b.complete
          ? "Достигнут: каждое требование дошло до своей цели."
          : "Не достигнут. Нужны два условия сразу: взвешенная готовность в сто " +
            "процентов и каждое требование на своей цели. Среднего процента мало — " +
            "он позволяет закрыть дыру чужим избытком.") + "</p>");
  }).join("");
}

// Дерево: навыки, разложенные по слою — длине самой длинной цепочки предпосылок.
// Списки и карта отвечают «что есть» и «что открыто прямо сейчас»; ни один из них
// не показывает путь целиком — от того, что берётся с нуля, до вершины, ради
// которой всё и строилось. Слой отвечает ровно на это: сколько ступеней между
// сегодняшним днём и навыком, который пока недостижим.
function treePage() {
  const all = Object.values(S);
  const maxLayer = Math.max.apply(null, all.map((s) => s.layer));
  const rows = [];
  for (let lv = 0; lv <= maxLayer; lv++) {
    // Порядок внутри слоя: сперва несущие — те, от кого зависит больше других
    // навыков. «Границы системы» открывают восемь, «Что автоматизировать» —
    // один, и стоять они должны в этом порядке, а не в алфавитном по коду:
    // сортировка по идентификатору ставила первым ИИ просто потому, что «AI»
    // раньше «S» в латинице. При равном весе — порядок дисциплин из склада,
    // где системное мышление первое, а ИИ одиннадцатое.
    const ord = (s) => (D[s.domains[0]] || {}).order || 99;
    const here = all.filter((s) => s.layer === lv)
      .sort((a, b) => b.opens.length - a.opens.length ||
        ord(a) - ord(b) || a.id.localeCompare(b.id));
    if (!here.length) continue;
    const proved = here.filter((s) => s.state === "доказан").length;
    const open = here.filter((s) => s.state === "открыт").length;
    const cards = here.map((s) =>
      '<a class="' + stateCls("tnode", s.state) + '" href="#/skill/' +
      s.id.toLowerCase() + '"><span class="tid">' + s.id + "</span>" +
      '<span class="tnm">' + esc(s.ru) + "</span>" +
      '<span class="tdom">' + esc(s.domains.map(domRu)[0] || "") + "</span></a>").join("");
    rows.push('<div class="tlayer"><div class="thead"><span class="tlv">' +
      (lv === 0 ? "начало" : "шаг " + lv) + "</span><span class=\"tcnt\">" +
      plural(here.length, "навык", "навыка", "навыков") +
      (proved ? " · доказано " + proved : "") +
      (open ? " · открыто " + open : "") + '</span></div>' +
      '<div class="tnodes">' + cards + "</div></div>");
  }
  return '<div class="crumb"><a href="#/">развитие</a><span>/</span>' +
    "<span>дерево</span></div><h1>Дерево навыков</h1>" +
    '<p class="lede">Слой — сколько ступеней предпосылок отделяет навык от начала. ' +
    "Первый слой берётся с нуля; каждый следующий открывается только после того, " +
    "как доказаны навыки под ним. Цветом — состояние: янтарный доказан, синий " +
    "открыт к взятию, серый ждёт предпосылок.</p>" +
    '<div class="tree">' + rows.join("") + "</div>";
}

let sf = "все", bf = "недавние";
function skillsPage() {
  const all = Object.values(S);
  const counts = {
    "все": all.length,
    "доказан": all.filter((s) => s.state === "доказан").length,
    "открыт": all.filter((s) => s.state === "открыт").length,
    "закрыт": all.filter((s) => s.state === "закрыт").length,
  };
  const bar = ["все", "доказан", "открыт", "закрыт"].map((k) =>
    '<button data-sf="' + k + '"' + (sf === k ? ' class="on"' : "") + ">" + k + " · " +
    counts[k] + "</button>").join("");
  // Список идёт в порядке прохождения: сперва слой, внутри слоя — несущие,
  // дальше порядок дисциплин. Алфавит по коду ставил бы ИИ впереди системного
  // мышления, как это было на карте и в дереве.
  const ordS = (s) => (D[s.domains[0]] || {}).order || 99;
  const rows = all.filter((s) => sf === "все" || s.state === sf)
    .sort((a, b) => a.layer - b.layer || b.opens.length - a.opens.length ||
      ordS(a) - ordS(b) || a.id.localeCompare(b.id));
  return '<div class="crumb"><a href="#/">развитие</a><span>/</span>' +
    "<span>навыки</span></div><h1>Навыки</h1>" +
    '<p class="lede">Порядок — как проходить: сверху то, что берётся с нуля и ' +
    "открывает больше других. Квадраты показывают ступени: заполненный — " +
    "доказанное владение, обведённый — изученность из книг, пунктирный — цель.</p>" +
    '<div class="sect"><div class="bar">' + bar + '</div><div class="rows">' +
    capped("skills-" + sf, rows, (s) => skillRow(s), 40) + "</div></div>";
}

function booksPage() {
  const all = Object.values(B);
  const counts = {
    "недавние": all.filter((b) => b.skills.length).length,
    "набор": all.filter((b) => b.set).length,
    "на руках": all.filter((b) => b.owned).length,
    "размеченные": all.filter((b) => b.skills.length).length,
    "весь каталог": all.length,
  };
  const bar = ["недавние", "набор", "на руках", "размеченные", "весь каталог"].map((k) =>
    '<button data-bf="' + k + '"' + (bf === k ? ' class="on"' : "") + ">" + k + " · " +
    counts[k] + "</button>").join("");
  const rows = all.filter((b) =>
    bf === "набор" ? b.set : bf === "на руках" ? b.owned
      : bf === "размеченные" || bf === "недавние" ? b.skills.length > 0 : true)
    .sort((a, b) => {
      // Свежезаведённое не имеет ни отметки «на руках», ни места в наборе,
      // поэтому в обычном порядке тонет ниже сороковой строки — и пополнение
      // каталога выглядит так, будто его не было.
      if (bf === "недавние") return (b.no || 0) - (a.no || 0);
      const da = a.skills.length ? Math.max.apply(null, a.skills.map((x) => x[1])) : 0;
      const db = b.skills.length ? Math.max.apply(null, b.skills.map((x) => x[1])) : 0;
      // Порядок чтения: глубже — раньше; при равной глубине первой идёт та,
      // что уже на руках, потом та, что в наборе, и лишь потом алфавит.
      return db - da || (b.owned | 0) - (a.owned | 0) || (b.set | 0) - (a.set | 0)
        || a.title.localeCompare(b.title, "ru");
    });
  return '<div class="crumb"><a href="#/">развитие</a><span>/</span>' +
    "<span>ресурсы</span></div><h1>Ресурсы</h1>" +
    '<p class="lede">Книги, статьи и курсы. Число слева — глубина, до которой ресурс ' +
    "заводит по самому сильному своему навыку; прочерк значит, что разметки ещё нет. " +
    "Открыто на недавних — то, что заведено последним, новое сверху; «набор» — " +
    "очередь чтения, «размеченные» — всё, что работает хотя бы на один навык.</p>" +
    '<div class="sect"><div class="bar">' + bar + '</div><div class="rows">' +
    capped("books-" + bf, rows, (b) => bookRow(b), 40) + "</div></div>";
}

function lost(why) {
  return '<div class="crumb"><a href="#/">развитие</a></div>' +
    "<h1>Такой страницы нет</h1><p class=\"lede\">" + esc(why) + "</p>";
}

function route() {
  const bits = (location.hash || "#/").replace(/^#\/?/, "").split("/")
    .filter(Boolean).map(decodeURIComponent);
  if (!bits.length) return { view: "home" };
  if (bits[0] === "skill" && bits[1]) return { view: "skill", key: bits[1].toUpperCase() };
  if (bits[0] === "domain" && bits[1]) return { view: "domain", key: bits[1] };
  if (bits[0] === "reading" && bits[1]) return { view: "book", key: bits[1] };
  if (bits[0] === "map") return { view: "map" };
  if (bits[0] === "build") return { view: "build" };
  if (bits[0] === "tree") return { view: "tree" };
  if (bits[0] === "skills") return { view: "skills" };
  if (bits[0] === "books") return { view: "books" };
  return { view: "lost", key: bits.join("/") };
}

function draw() {
  const r = route();
  const body =
    r.view === "home" ? home()
      : r.view === "map" ? mapPage()
      : r.view === "domain" ? domainPage(r.key)
        : r.view === "skill" ? skillPage(r.key)
          : r.view === "book" ? bookPage(r.key)
            : r.view === "skills" ? skillsPage()
              : r.view === "tree" ? treePage()
                : r.view === "build" ? buildPage()
                : r.view === "books" ? booksPage()
                : lost("Адрес «" + r.key + "» ни на что не указывает.");
  document.getElementById("page").innerHTML = body;
  document.getElementById("nav-build").className = r.view === "build" ? "on" : "";
  document.getElementById("nav-tree").className = r.view === "tree" ? "on" : "";
  document.getElementById("nav-skills").className = r.view === "skills" ? "on" : "";
  document.getElementById("nav-books").className = r.view === "books" ? "on" : "";
}

// Поиск идёт и по навыкам, и по ресурсам: человек не помнит, к какому сорту
// относится то, что ищет, — он помнит слово.
function search(q) {
  const box = document.getElementById("pop");
  q = q.trim().toLowerCase();
  if (q.length < 2) { box.innerHTML = ""; box.style.display = "none"; return; }
  const hits = [];
  Object.values(S).forEach((s) => {
    if ((s.ru + " " + s.en + " " + s.id).toLowerCase().includes(q))
      hits.push(['<a href="#/skill/' + s.id.toLowerCase() + '"><span class="k">навык' +
        '</span><span>' + esc(s.ru) + '<span class="sub">' + s.id + " · " + s.state +
        "</span></span></a>", 0]);
  });
  Object.values(B).forEach((b) => {
    if ((b.title + " " + b.author).toLowerCase().includes(q))
      hits.push(['<a href="#/reading/' + encodeURIComponent(b.slug) +
        '"><span class="k">ресурс</span><span>' + esc(b.title) + '<span class="sub">' +
        esc(b.author || b.tier) + "</span></span></a>", 1]);
  });
  box.innerHTML = hits.length
    ? hits.slice(0, 14).map((h) => h[0]).join("") +
      (hits.length > 14 ? '<div class="none">и ещё ' + (hits.length - 14) + "</div>" : "")
    : '<div class="none">Ничего не нашлось</div>';
  box.style.display = "block";
}

document.addEventListener("click", (e) => {
  const more = e.target.closest("[data-more]");
  if (more) { expanded[more.dataset.more] = true; draw(); return; }
  const s = e.target.closest("[data-sf]");
  if (s) { sf = s.dataset.sf; draw(); return; }
  const b = e.target.closest("[data-bf]");
  if (b) { bf = b.dataset.bf; draw(); return; }
  if (!e.target.closest(".find")) {
    const box = document.getElementById("pop");
    if (box) box.style.display = "none";
  }
  if (e.target.closest(".pop a")) closeFind();
});
document.getElementById("q").addEventListener("input", (e) => search(e.target.value));
document.getElementById("theme").addEventListener("click", () => {
  const el = document.documentElement;
  const now = el.getAttribute("data-theme") ||
    (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  const next = now === "dark" ? "light" : "dark";
  el.setAttribute("data-theme", next);
  document.getElementById("theme").textContent = next === "dark" ? "☀" : "☾";
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeFind();
});

// Список находок живёт вне страницы, поэтому сам он при переходе не исчезнет:
// открытая подсказка поверх новой страницы читается как часть этой страницы.
function closeFind() {
  const box = document.getElementById("pop");
  box.style.display = "none";
  box.innerHTML = "";
  document.getElementById("q").value = "";
}

window.addEventListener("hashchange", () => {
  expanded = {};
  closeFind();
  draw();
  window.scrollTo(0, 0);
});
draw();
"""

PAGE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Развитие — склад GSWorkHUB</title>
<style>{style}</style>
</head>
<body>
<div class="top"><div class="topin">
  <a class="mark" href="#/">склад GSWorkHUB</a>
  <nav class="nav">
    <a id="nav-map" href="#/map">Карта</a>
    <a id="nav-build" href="#/build">Профиль</a>
    <a id="nav-tree" href="#/tree">Дерево</a>
    <a id="nav-skills" href="#/skills">Навыки</a>
    <a id="nav-books" href="#/books">Ресурсы</a>
  </nav>
  <span class="grow"></span>
  <div class="find">
    <input id="q" type="search" placeholder="навык, книга, автор" autocomplete="off">
    <div id="pop" class="pop" style="display:none"></div>
  </div>
  <button id="theme" class="theme" title="Светлая или тёмная">☾</button>
</div></div>
<div class="wrap">
  <div id="page"></div>
  <div class="foot">{foot}</div>
</div>
<script>window.WH = {data};</script>
<script>{script}</script>
</body>
</html>
"""


def render(data: dict, now: dt.datetime) -> str:
    t = data["totals"]
    # Подвал — дата и ничего больше. Пересчёт склада и команда сборки читателю
    # страницы не нужны: он пришёл смотреть карту, а не служебную сводку.
    foot = f"{now:%d.%m.%Y}"
    payload = dict(data)
    payload["kindRu"] = KIND_RU
    payload["statusRu"] = STATUS_RU
    payload["nodeRu"] = NODE_RU
    return PAGE.format(
        style=STYLE, foot=html.escape(foot), script=SCRIPT,
        data=json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        .replace("</", "<\\/"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--out", type=Path, help="куда положить страницу")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    settings = args.root / "config" / "attention.yml"
    config = {}
    if settings.exists():
        config = yaml.safe_load(settings.read_text(encoding="utf-8")) or {}
    gate = int((config.get("skills") or {}).get("gate", skill_map.DEFAULT_GATE))

    data = collect(args.root, gate)
    t = data["totals"]
    if not data["skills"] or not data["books"]:
        print(f"АТЛАС: ВАКУУМ — навыков {t['skills']}, ресурсов {t['catalog']}. "
              "Это провал, а не пустая страница.")
        return 1

    # Дисциплина без названия читается ключом — служебной строкой вместо слова.
    unnamed = [k for k, d in data["domains"].items() if d["ru"] == k]
    if unnamed:
        print(f"АТЛАС: дисциплины без названия в config/domains.yml: {', '.join(unnamed)}")

    page = render(data, dt.datetime.now().replace(microsecond=0))
    if args.dry_run or not args.out:
        print(f"атлас: навыков {t['skills']} (доказано {t['proved']}, открыто {t['open']}), "
              f"ресурсов {t['catalog']} (набор {t['set']}, размечено {t['marked']}), "
              f"дисциплин {len(data['domains'])}, страниц "
              f"{t['skills'] + t['catalog'] + len(data['domains']) + 3}, "
              f"вес {len(page.encode('utf-8')) // 1024} КБ")
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(page, encoding="utf-8")
    print(f"атлас: {args.out} — навыков {t['skills']} (доказано {t['proved']}), "
          f"ресурсов {t['catalog']} (набор {t['set']}), дисциплин {len(data['domains'])}, "
          f"страниц {t['skills'] + t['catalog'] + len(data['domains']) + 3}, "
          f"вес {len(page.encode('utf-8')) // 1024} КБ")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
