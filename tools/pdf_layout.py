"""Вёрстка PDF из собранного DOCX.

DOCX остаётся единственным носителем полного текста документа, PDF собирается
из него — поэтому расхождение между файлами невозможно.

Что вёрстка добавляет сверх конвертации:
* светлая обложка, оглавление с номерами страниц, рамка и колонтитулы страницы;
* врезки документа становятся карточками с рубрикой;
* каждая схема занимает отдельную альбомную страницу с подписью.

Используется сборками клиентских документов в `deliverables/` и
`tools/guides/primer/`.
"""

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from weasyprint import HTML

CSS_TEMPLATE = """
/* левое поле шире остальных — под кольца переплёта */
@page {
  size: A4;
  margin: 14mm 12mm 13mm 24mm;
  border-top: 0.6pt solid #d3d8de;
  border-bottom: 0.6pt solid #d3d8de;
  padding: 3mm 0;
  @top-left {
    content: "__RUNNING_HEAD__";
    font-family: "Helvetica Neue", Helvetica, Arial, sans-serif;
    font-size: 7pt;
    letter-spacing: 0.03em;
    color: #9aa0a8;
    margin-bottom: 2.5mm;
  }
  @top-right {
    content: string(chapter);
    font-family: "Helvetica Neue", Helvetica, Arial, sans-serif;
    font-size: 7pt;
    font-weight: 700;
    color: #4a515b;
    margin-bottom: 2.5mm;
  }
  @bottom-right {
    content: counter(page);
    font-family: "Helvetica Neue", Helvetica, Arial, sans-serif;
    font-size: 8.5pt;
    font-weight: 700;
    color: #6c737d;
    margin-top: 2.5mm;
  }
}
@page cover { margin: 0; border: 0; padding: 0; @top-left { content: none; } @top-right { content: none; } @bottom-right { content: none; } }
@page plate {
  size: A4 landscape;
  margin: 11mm 10mm 10mm 24mm;
  border-top: 0.6pt solid #d3d8de;
  border-bottom: 0.6pt solid #d3d8de;
  padding: 2.5mm 0;
  @top-left { content: none; }
  @top-right { content: string(chapter); font-family: "Helvetica Neue", Helvetica, Arial, sans-serif; font-size: 7pt; font-weight: 700; color: #4a515b; margin-bottom: 2mm; }
}

html { font-family: Georgia, "Times New Roman", serif; font-size: 10.5pt; color: #16181d; }
body { line-height: 1.5; hyphens: auto; }
p { margin: 0 0 7pt; text-align: justify; }
strong { font-weight: 700; }

.cover { page: cover; height: 297mm; display: flex; flex-direction: column; color: #16181d; background: #ffffff; padding: 24mm 18mm 20mm 24mm; box-sizing: border-box; border-top: 4mm solid #12161c; }
.cover .kicker { font-family: "Helvetica Neue", Helvetica, Arial, sans-serif; font-size: 8.5pt; letter-spacing: 0.18em; color: #8a6d2f; margin-bottom: 22mm; }
.cover h1 { font-family: "Helvetica Neue", Helvetica, Arial, sans-serif; font-size: 33pt; line-height: 1.12; font-weight: 700; margin: 0 0 7mm; color: #12161c; border: 0; padding: 0; }
.cover .subtitle { font-size: 12pt; line-height: 1.45; color: #4a515b; max-width: 132mm; margin-bottom: 14mm; text-align: left; }
.cover .lead { border-left: 3pt solid #c8a15a; padding-left: 6mm; font-size: 11pt; color: #23272e; max-width: 138mm; text-align: left; }
.cover .facts { margin-top: 14mm; display: flex; }
.cover .facts span { font-family: "Helvetica Neue", Helvetica, Arial, sans-serif; margin-right: 16mm; font-size: 9pt; color: #6c737d; letter-spacing: 0.06em; }
.cover .facts b { display: block; font-size: 20pt; color: #12161c; letter-spacing: 0; }
.cover .spacer { flex: 1; }
.cover .meta { font-family: "Helvetica Neue", Helvetica, Arial, sans-serif; font-size: 9.5pt; color: #3a4048; border-top: 0.5pt solid #d5d9df; padding-top: 4mm; }
.cover .meta em { display: block; font-style: normal; color: #8a8f98; font-size: 8.5pt; margin-top: 2mm; }

h1 { string-set: chapter content(); font-family: "Helvetica Neue", Helvetica, Arial, sans-serif; font-size: 18pt; line-height: 1.2; margin: 0 0 4.5mm; padding-bottom: 2.5mm; border-bottom: 1.5pt solid #12161c; break-before: page; break-after: avoid; }
h2 { font-family: "Helvetica Neue", Helvetica, Arial, sans-serif; font-size: 12pt; margin: 6mm 0 2.5mm; break-after: avoid; }
h3 { font-family: "Helvetica Neue", Helvetica, Arial, sans-serif; font-size: 10.5pt; margin: 4.5mm 0 1.5mm; color: #2b3038; break-after: avoid; }

.toc { break-after: page; }
.toc h1 { break-before: avoid; }
.toc ol { list-style: none; padding: 0; margin: 0; }
.toc li { border-bottom: 0.5pt dotted #c9ced6; padding: 2.4mm 0; font-family: "Helvetica Neue", Helvetica, Arial, sans-serif; font-size: 10pt; }
.toc a { color: #16181d; text-decoration: none; display: flex; }
.toc a span:not(.num) { flex: 1; }
.toc a::after { content: target-counter(attr(href), page); color: #8a8f98; }
.toc .num { color: #8a8f98; margin-right: 3mm; }

.callout { background: #f4f6f8; border-left: 3pt solid #c8a15a; padding: 3mm 4mm; margin: 3mm 0 4.5mm; break-inside: avoid; }
.callout .callout-title { font-family: "Helvetica Neue", Helvetica, Arial, sans-serif; font-size: 8pt; letter-spacing: 0.14em; color: #8a6d2f; margin-bottom: 2mm; }
.callout p { margin: 0 0 4pt; }
.callout p:last-child { margin-bottom: 0; }

ul, ol { margin: 0 0 7pt; padding-left: 6mm; }
li { margin-bottom: 2pt; }

table { width: 100%; border-collapse: collapse; margin: 4mm 0 6mm; font-size: 9pt; break-inside: avoid; font-family: "Helvetica Neue", Helvetica, Arial, sans-serif; }
th, td { border-bottom: 0.5pt solid #d5d9df; padding: 2.2mm 2.5mm; text-align: left; vertical-align: top; }
th { background: #f0f2f5; font-weight: 700; }

/* таблица шире пяти колонок не читается в портрете — уходит на разворот */
.wide { page: plate; break-before: page; break-after: page; }
.wide table { font-size: 7.5pt; break-inside: auto; }
.wide th, .wide td { padding: 1.6mm 1.8mm; }
.wide tr { break-inside: avoid; }
.wide .wide-title { font-family: "Helvetica Neue", Helvetica, Arial, sans-serif; font-size: 10pt; font-weight: 700; margin-bottom: 3mm; }

/* высота = лист 210 мм минус поля 11 и 10, отбивка рамки 2×2.5 и сама рамка */
.plate { page: plate; break-before: page; break-after: page; break-inside: avoid; margin: 0; text-align: center; display: flex; flex-direction: column; justify-content: center; height: 180mm; }
.plate img { max-width: 100%; max-height: 160mm; object-fit: contain; }
.plate .figure-title { font-family: "Helvetica Neue", Helvetica, Arial, sans-serif; font-size: 11pt; font-weight: 700; margin: 4mm 0 1.2mm; }
.plate .figure-note { font-size: 9pt; line-height: 1.4; color: #565c66; max-width: 205mm; margin: 0 auto; text-align: center; }
.plate + h1 { break-before: avoid; }
"""

FIGURE_PATTERN = re.compile(
    r"<p><img\s+src=\"(?P<src>[^\"]+)\"[^>]*?/></p>\s*"
    r"<p><(?P<tag>strong|em)>(?P<title>Рисунок.*?)</(?P=tag)></p>"
    r"(?:\s*<p><em>(?P<note>.*?)</em></p>)?",
    re.DOTALL,
)
CALLOUT_PATTERN = re.compile(r"<blockquote>(?P<inner>.*?)</blockquote>", re.DOTALL)
HEADING_PATTERN = re.compile(r"<h1 id=\"([^\"]+)\"[^>]*>(.*?)</h1>", re.DOTALL)


@dataclass
class Cover:
    """Содержимое обложки и колонтитула."""

    title: str
    subtitle: str
    kicker: str
    version: str
    audience: str
    lead: str
    running_head: str
    facts: list = field(default_factory=list)

    def html(self):
        facts = "".join(f"<span><b>{n}</b>{label}</span>" for n, label in self.facts)
        return (
            '<section class="cover">'
            f'<div class="kicker">{self.kicker}</div>'
            f"<h1>{self.title}</h1>"
            f'<div class="subtitle">{self.subtitle}</div>'
            f'<div class="lead">{self.lead}</div>'
            f'<div class="facts">{facts}</div>'
            '<div class="spacer"></div>'
            f'<div class="meta">{self.version}<em>{self.audience}</em></div>'
            "</section>"
        )


def convert(docx, build_dir):
    build_dir.mkdir(parents=True, exist_ok=True)
    html = build_dir / "body.html"
    subprocess.run(
        [
            "pandoc",
            "-f",
            "docx",
            "-t",
            "html5",
            f"--extract-media={build_dir}",
            str(docx),
            "-o",
            str(html),
        ],
        check=True,
    )
    return html.read_text(encoding="utf-8")


def plates(body):
    def replace(match):
        note = re.sub(r"\s+", " ", match.group("note") or "").strip()
        note_html = f'<div class="figure-note">{note}</div>' if note else ""
        title = re.sub(r"\s+", " ", match.group("title")).strip()
        return (
            '<figure class="plate">'
            f'<img src="{match.group("src")}" />'
            f'<div class="figure-title">{title}</div>'
            f"{note_html}"
            "</figure>"
        )

    return FIGURE_PATTERN.sub(replace, body)


def callouts(body):
    def replace(match):
        inner = match.group("inner").strip()
        head = re.match(r"<p><strong>(?P<title>[^<]+?)<br\s*/>\s*", inner)
        if head:
            rest = re.sub(r"</strong>", "", inner[head.end() :], count=1)
            return (
                '<div class="callout">'
                f'<div class="callout-title">{head.group("title").strip()}</div>'
                f"<p>{rest}"
                "</div>"
            )
        return f'<div class="callout">{inner}</div>'

    return CALLOUT_PATTERN.sub(replace, body)


def wide_tables(body, min_columns=6):
    """Таблицы шире пяти колонок переносит на альбомный разворот."""

    def replace(match):
        table = match.group(0)
        columns = len(re.findall(r"<th\b", table.split("</tr>")[0]))
        if columns < min_columns:
            return table
        return f'<div class="wide">{table}</div>'

    return re.sub(r"<table\b.*?</table>", replace, body, flags=re.DOTALL)


def table_of_contents(body):
    rows = []
    for anchor, raw in HEADING_PATTERN.findall(body):
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", raw)).strip()
        number = ""
        match = re.match(r"^(\d+)\.\s*(.+)$", text)
        if match:
            number, text = match.group(1), match.group(2)
        label = f'<span class="num">{number}.</span>' if number else ""
        rows.append(f'<li><a href="#{anchor}">{label}<span>{text}</span></a></li>')
    return (
        '<section class="toc"><h1>Содержание</h1><ol>'
        + "".join(rows)
        + "</ol></section>"
    )


def build(docx, pdf, cover, build_dir=None, toc=True):
    """Собрать PDF из DOCX. Возвращает путь готового файла."""
    docx, pdf = Path(docx), Path(pdf)
    build_dir = Path(build_dir) if build_dir else pdf.parent / "build"
    body = convert(docx, build_dir)
    body = body[body.index("<h1") :]
    body = plates(body)
    body = wide_tables(body)
    body = callouts(body)
    css = CSS_TEMPLATE.replace("__RUNNING_HEAD__", cover.running_head)
    document = (
        "<!doctype html><html lang='ru'><head><meta charset='utf-8'>"
        f"<title>{cover.title}</title><style>{css}</style></head><body>"
        + cover.html()
        + (table_of_contents(body) if toc else "")
        + body
        + "</body></html>"
    )
    (build_dir / "document.html").write_text(document, encoding="utf-8")
    HTML(string=document, base_url=str(build_dir)).write_pdf(pdf)
    return pdf
