"""Светлый вариант схем для чёрно-белой печати.

Тёмные плашки и тёмный фон в печати превращаются в серую заливку: текст на них
теряется, а тонер расходуется на всю страницу. Скрипт переводит схемы в светлый
вариант, сохраняя тон акцентов.

Два режима:
* `invert` — для схем на тёмном фоне: светлота каждого цвета переворачивается,
  насыщенные акценты притемняются до читаемого уровня;
* `map` — для светлых схем с отдельными тёмными плашками: меняются только они
  и светлый текст на них.

Запуск:
    /Library/Developer/CommandLineTools/usr/bin/python3 svg_lighten.py \\
        <папка-с-svg> <папка-назначения> [--mode invert|map] [--width 2400]
"""

import argparse
import colorsys
import re
import subprocess
import sys
from pathlib import Path

RSVG = "/opt/homebrew/bin/rsvg-convert"

HEX_PATTERN = re.compile(r"#([0-9a-fA-F]{6})\b")
TEXT_PATTERN = re.compile(r"<text\b[^>]*>", re.DOTALL)
SHAPE_PATTERN = re.compile(r"<(rect|circle|ellipse|path|polygon)\b[^>]*>", re.DOTALL)
STYLE_PATTERN = re.compile(r"<style\b[^>]*>.*?</style>", re.DOTALL)
CSS_RULE_PATTERN = re.compile(r"([^{}]*)(\{[^{}]*\})")

# светлые схемы: тёмная плашка -> светлая, светлый текст на ней -> тёмный
DARK_PLATES = {
    "#16324F": "#E3EBF4",
    "#234968": "#DCE6F2",
    "#102E45": "#E3EBF4",
    "#153951": "#E3EBF4",
}
LIGHT_INK = {
    "white": "#16324F",
    "#FFFFFF": "#16324F",
    "#D8E6F3": "#41586F",
    "#CAD3DF": "#41586F",
}
KEEP = {"#102A43"}  # цвет тени: осветление сделало бы её грязной


def to_hls(value):
    red, green, blue = (int(value[i : i + 2], 16) / 255 for i in (0, 2, 4))
    return colorsys.rgb_to_hls(red, green, blue)


def to_hex(hue, lightness, saturation):
    red, green, blue = colorsys.hls_to_rgb(hue, lightness, saturation)
    return f"#{round(red * 255):02X}{round(green * 255):02X}{round(blue * 255):02X}"


def as_surface(value):
    """Фон и панели: тёмное становится светлым, иерархия слоёв сохраняется."""
    hue, lightness, saturation = to_hls(value)
    if lightness >= 0.55:
        return None
    if lightness < 0.12:
        lightness = 0.975
    elif lightness < 0.25:
        lightness = 0.935
    else:
        lightness = 0.88
    return to_hex(hue, lightness, min(saturation, 0.16))


def as_ink(value):
    """Текст и штрихи: светлое становится тёмным, тёмное остаётся тёмным."""
    hue, lightness, saturation = to_hls(value)
    if lightness <= 0.45:
        return None
    lightness = 0.30 if saturation > 0.35 else 0.18
    return to_hex(hue, lightness, min(saturation, 0.75))


def recolor(fragment, transform):
    def replace(match):
        color = f"#{match.group(1).upper()}"
        if color in KEEP:
            return match.group(0)
        return transform(match.group(1)) or match.group(0)

    fragment = HEX_PATTERN.sub(replace, fragment)
    light = transform("FFFFFF")
    if light:
        fragment = re.sub(r'"white"', f'"{light}"', fragment, flags=re.IGNORECASE)
        fragment = re.sub(r'"#fff"', f'"{light}"', fragment, flags=re.IGNORECASE)
    return fragment


def text_classes(svg):
    """Классы, которыми размечен текст: их цвет — чернила, а не заливка."""
    names = set()
    for tag in TEXT_PATTERN.findall(svg):
        found = re.search(r'class="([^"]+)"', tag)
        if found:
            names.update(found.group(1).split())
    return names


def recolor_style(block, ink_classes):
    """Правило красит текст, если размечает текст или задаёт шрифт."""

    def replace(match):
        selector, body = match.group(1), match.group(2)
        used = {name.strip(" .") for name in selector.split(",")}
        is_ink = "font" in body or bool(used & ink_classes)
        return selector + recolor(body, as_ink if is_ink else as_surface)

    return CSS_RULE_PATTERN.sub(replace, block)


def invert(svg):
    """Схему на тёмном фоне переводит в светлый вариант для печати."""

    def fix_surface(match):
        tag = match.group(0)
        if match.group(1) in {"rect", "ellipse"}:
            return recolor(tag, as_surface)
        return recolor(tag, as_ink)

    ink = text_classes(svg)
    svg = STYLE_PATTERN.sub(lambda m: recolor_style(m.group(0), ink), svg)
    svg = SHAPE_PATTERN.sub(fix_surface, svg)
    svg = TEXT_PATTERN.sub(lambda m: recolor(m.group(0), as_ink), svg)
    return svg


def remap(svg):
    """Светлеют только заливки фигур; в тексте, наоборот, светлое темнеет."""

    def fix_shape(match):
        tag = match.group(0)
        for dark, light in DARK_PLATES.items():
            tag = re.sub(
                rf'fill="{re.escape(dark)}"', f'fill="{light}"', tag, flags=re.IGNORECASE
            )
        return tag

    def fix_text(match):
        tag = match.group(0)
        for light, ink in LIGHT_INK.items():
            tag = re.sub(
                rf'fill="{re.escape(light)}"', f'fill="{ink}"', tag, flags=re.IGNORECASE
            )
        return tag

    svg = SHAPE_PATTERN.sub(fix_shape, svg)
    return TEXT_PATTERN.sub(fix_text, svg)


def convert(source, target, mode, width):
    target.parent.mkdir(parents=True, exist_ok=True)
    svg = source.read_text(encoding="utf-8")
    svg = invert(svg) if mode == "invert" else remap(svg)
    target.write_text(svg, encoding="utf-8")
    png = target.with_suffix(".png")
    subprocess.run([RSVG, "-w", str(width), "-o", str(png), str(target)], check=True)
    return png


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source")
    parser.add_argument("target")
    parser.add_argument("--mode", choices=["invert", "map"], default="map")
    parser.add_argument("--width", type=int, default=2400)
    args = parser.parse_args()

    source_dir, target_dir = Path(args.source), Path(args.target)
    files = sorted(source_dir.rglob("*.svg"))
    if not files:
        print(f"нет схем в {source_dir}", file=sys.stderr)
        return 1
    for svg in files:
        png = convert(
            svg, target_dir / svg.relative_to(source_dir), args.mode, args.width
        )
        print(png.relative_to(target_dir))
    print(f"осветлено схем: {len(files)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
