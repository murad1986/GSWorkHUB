#!/usr/bin/env python3
"""Движок схем: описание в YAML → файл .excalidraw.

    python3 build.py schemes/sipoc.yaml
    python3 build.py schemes/*.yaml

Новая диаграмма — это новый файл описания, а не новый код. Код меняется только
тогда, когда нужен новый вид раскладки; виды перечислены в layouts.py.

Устройство описания:

    title:     заголовок схемы
    subtitle:  список строк под заголовком
    legend:    список пар [вид, подпись] — образцы цветов
    blocks:    список блоков сверху вниз, у каждого type из layouts.BLOCKS

Движок делает два прохода: сначала спрашивает у каждого блока его естественную
ширину и берёт наибольшую — так лист получается по содержимому, а не наоборот;
потом укладывает блоки по вертикали, зная итоговую ширину.
"""

import sys
from pathlib import Path

import yaml
from excalidraw_kit import header, report, save, sheet
from layouts import BLOCKS, PAD

HEAD_GAP = 40
BLOCK_GAP = 44


def load(path: Path) -> dict:
    spec = yaml.safe_load(path.read_text(encoding="utf-8"))
    for i, block in enumerate(spec.get("blocks", [])):
        block.setdefault("id", f"b{i}-{block['type']}")
    return spec


def build(spec: dict) -> list[dict]:
    blocks = spec.get("blocks", [])
    for block in blocks:
        if block["type"] not in BLOCKS:
            raise SystemExit(
                f"неизвестный вид блока: {block['type']}; "
                f"есть такие: {', '.join(sorted(BLOCKS))}"
            )

    # проход 1 — ширина листа берётся по самому широкому блоку
    natural = [BLOCKS[b["type"]][0](b) for b in blocks]
    width = max(natural + [spec.get("min_width", 1200)]) + PAD * 2

    # шапка и легенда — те же блоки, только объявляются в корне
    head = header(PAD, 56, spec["title"], spec.get("subtitle", []),
                  width=width - PAD * 2)
    y = 56 + 34 + 26 + len(spec.get("subtitle", [])) * 19 + HEAD_GAP

    parts: list[dict] = []
    if spec.get("legend"):
        legend_spec = {
            "id": "legend",
            "type": "legend",
            "items": spec["legend"],
            "step": spec.get("legend_step", 272),
            "item_width": spec.get("legend_width", 254),
        }
        chunk, h = BLOCKS["legend"][1](legend_spec, PAD, y, width)
        parts.extend(chunk)
        y += h + HEAD_GAP

    # проход 2 — укладка сверху вниз
    for block in blocks:
        y += block.get("gap_before", 0)
        chunk, h = BLOCKS[block["type"]][1](block, PAD, y, width)
        parts.extend(chunk)
        y += h + block.get("gap_after", BLOCK_GAP)

    height = y - BLOCK_GAP + PAD
    return [sheet(width, height)] + parts + head


def main(argv: list[str]) -> None:
    if len(argv) < 2:
        raise SystemExit("нужно: python3 build.py <описание.yaml> [ещё.yaml ...]")
    for name in argv[1:]:
        path = Path(name)
        spec = load(path)
        out = Path(spec.get("out") or f"out/{path.stem}.excalidraw")
        scene = save(out, build(spec))
        report(out, scene)


if __name__ == "__main__":
    main(sys.argv)
