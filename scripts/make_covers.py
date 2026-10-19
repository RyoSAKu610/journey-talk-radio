"""Generate podcast cover art (3000x3000) per language and the web app icons.

The images are static and committed under docs/; re-run only when the design or the
language list changes:

    python scripts/make_covers.py --font /path/to/cjk-font.ttc

The font must cover Japanese, Chinese, Korean and Cyrillic (e.g. WenQuanYi Zen Hei or
Noto Sans CJK).
"""
from __future__ import annotations

import argparse
from pathlib import Path

import yaml
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
SIZE = 3000

# slug: (native name, background, accent)
LANGUAGES = {
    "de": ("Deutsch", "#23315c", "#f2c94c"),
    "es": ("Español", "#9a3412", "#fde68a"),
    "ru": ("Русский", "#1e40af", "#fca5a5"),
    "zh": ("中文", "#991b1b", "#fcd34d"),
    "ko": ("한국어", "#115e59", "#a7f3d0"),
}
MAIN = ("Journey Talk", "#111827", "#8aa8ff")


def font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(path), size)


def centered(draw: ImageDraw.ImageDraw, y: int, text: str, face: ImageFont.FreeTypeFont, fill: str, width: int = SIZE) -> None:
    box = draw.textbbox((0, 0), text, font=face)
    draw.text(((width - (box[2] - box[0])) / 2 - box[0], y), text, font=face, fill=fill)


def fit(text: str, path: Path, start: int, max_width: int) -> ImageFont.FreeTypeFont:
    size = start
    while size > 80:
        face = font(path, size)
        box = face.getbbox(text)
        if box[2] - box[0] <= max_width:
            return face
        size -= 20
    return font(path, size)


def waves(draw: ImageDraw.ImageDraw, accent: str, top: int, size: int = SIZE, scale: float = 1.0) -> None:
    """Sound-wave bars: the show's visual signature."""
    heights = [0.25, 0.5, 0.8, 1.0, 0.65, 0.9, 0.45, 0.75, 0.3]
    bar, gap = int(size / 40 * scale), int(size / 60 * scale)
    total = len(heights) * bar + (len(heights) - 1) * gap
    x = (size - total) // 2
    for h in heights:
        height = int(size * 0.14 * scale * h)
        y = top + (int(size * 0.14 * scale) - height) // 2
        draw.rounded_rectangle([x, y, x + bar, y + height], radius=bar // 2, fill=accent)
        x += bar + gap


def cover(path: Path, native: str, japanese: str, background: str, accent: str, out: Path) -> None:
    image = Image.new("RGB", (SIZE, SIZE), background)
    draw = ImageDraw.Draw(image)
    draw.ellipse([SIZE * 0.55, -SIZE * 0.25, SIZE * 1.35, SIZE * 0.55], fill=shade(background, 1.18))
    draw.ellipse([-SIZE * 0.35, SIZE * 0.62, SIZE * 0.45, SIZE * 1.42], fill=shade(background, 0.82))
    centered(draw, int(SIZE * 0.12), "JOURNEY TALK", font(path, 170), accent)
    waves(draw, accent, int(SIZE * 0.24))
    centered(draw, int(SIZE * 0.43), native, fit(native, path, 520, int(SIZE * 0.86)), "#ffffff")
    if japanese:
        centered(draw, int(SIZE * 0.66), japanese, font(path, 240), "#ffffff")
    centered(draw, int(SIZE * 0.82), "ニュースで毎日、ことばの旅へ", font(path, 150), accent)
    out.parent.mkdir(parents=True, exist_ok=True)
    image.save(out, optimize=True)


def shade(hex_color: str, factor: float) -> str:
    rgb = [int(hex_color[i : i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{max(0, min(255, int(c * factor))):02x}" for c in rgb)


def icon(path: Path, size: int, out: Path, maskable: bool = False) -> None:
    background, accent = MAIN[1], MAIN[2]
    image = Image.new("RGB", (size, size), background)
    draw = ImageDraw.Draw(image)
    # Maskable icons may be cropped to a circle: keep the mark inside the central 80%.
    scale = 0.62 if maskable else 0.8
    inner = int(size * scale)
    offset = (size - inner) // 2
    mark = Image.new("RGB", (inner, inner), background)
    mark_draw = ImageDraw.Draw(mark)
    waves(mark_draw, accent, int(inner * 0.06), inner, scale=2.0)
    centered(mark_draw, int(inner * 0.40), "JT", font(path, int(inner * 0.42)), "#ffffff", inner)
    image.paste(mark, (offset, offset))
    out.parent.mkdir(parents=True, exist_ok=True)
    image.save(out, optimize=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--font", type=Path, required=True)
    parser.add_argument("--docs-dir", type=Path, default=ROOT / "docs")
    args = parser.parse_args()

    cfg = yaml.safe_load((ROOT / "cloud_languages.yaml").read_text(encoding="utf-8"))
    for lang in cfg["languages"]:
        native, background, accent = LANGUAGES[lang["slug"]]
        cover(args.font, native, lang["japanese_name"], background, accent, args.docs_dir / "covers" / f"{lang['slug']}.png")
    cover(args.font, MAIN[0], "5言語", MAIN[1], MAIN[2], args.docs_dir / "covers" / "journey-talk.png")
    icon(args.font, 192, args.docs_dir / "icons" / "icon-192.png")
    icon(args.font, 512, args.docs_dir / "icons" / "icon-512.png")
    icon(args.font, 512, args.docs_dir / "icons" / "icon-maskable-512.png", maskable=True)
    icon(args.font, 180, args.docs_dir / "icons" / "apple-touch-icon.png")
    print(f"[covers] {args.docs_dir / 'covers'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
