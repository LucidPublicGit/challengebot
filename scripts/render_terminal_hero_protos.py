"""CLEARED / ABANDONED hero styles — real card renderer (correct SCALE).

Writes data/card_previews/terminal_heroes/

  PYTHONPATH=. python scripts/render_terminal_hero_protos.py
"""
from __future__ import annotations

import asyncio
import io
from pathlib import Path

from PIL import Image, ImageDraw

from app.challenge_card import _font, demo_avatar_png, render_challenge_card

OUT = Path("data/card_previews/terminal_heroes")
AV = demo_avatar_png()

STYLES = [
    ("glow", "Current soft-glow (baseline)"),
    ("1_tier_pill", "Hero inside a soft tier pill"),
    ("2_double_rule", "Glow word + double tier underline"),
    ("3_symbol_lead", "Check / X + uppercase hero"),
    ("4_tracked", "Letter-spaced uppercase stamp"),
    ("5_stamp_outline", "Hollow outline stamp"),
    ("6_spark_ends", "Glow + end sparks"),
    ("7_stack_kicker", "Small kicker + big hero"),
]

CLEAR_FIXTURES = [
    ("easy", "5 / 5 volumes · Tadoku"),
    ("medium", "10 / 10 volumes · Tadoku"),
    ("hard", "20 / 20 episodes"),
    ("ota_master", "200k / 200k characters"),
]
ABANDON_FIXTURES = [
    ("easy", ""),
    ("medium", ""),
    ("hard", ""),
    ("ota_master", ""),
]


def _sheet(rows: list[tuple[str, Image.Image]], path: Path) -> None:
    gap, pad, lab_h = 12, 14, 28
    w = max(im.size[0] for _, im in rows)
    h = sum(im.size[1] + lab_h + gap for _, im in rows) + pad
    canvas = Image.new("RGB", (w + 2 * pad, h + pad), (32, 34, 40))
    d = ImageDraw.Draw(canvas)
    y = pad
    for lab, im in rows:
        d.text((pad, y), lab[:110], font=_font(13), fill=(180, 184, 192))
        y += lab_h
        canvas.paste(im, (pad, y), im if im.mode == "RGBA" else None)
        y += im.size[1] + gap
    canvas.save(path)
    print("wrote", path.name, canvas.size)


async def _card(kind: str, tier: str, detail: str, style: str) -> Image.Image:
    if kind == "complete":
        png = await render_challenge_card(
            kind="complete",
            display_name="Challenger",
            challenge_title="Manga Mini-Marathon",
            tier=tier,
            number=1,
            avatar_bytes=AV,
            focus="One Piece",
            line_primary="Cleared",
            line_secondary=detail or "5 / 5 volumes · Tadoku",
            footer="",
            hero_style=style,
        )
    else:
        png = await render_challenge_card(
            kind="abandon",
            display_name="Challenger",
            challenge_title="YouTube Deep Dive",
            tier=tier,
            number=3,
            avatar_bytes=AV,
            focus="Comprehensible Japanese",
            line_primary="Abandoned",
            line_secondary=detail,
            footer="",
            hero_style=style,
        )
    return Image.open(io.BytesIO(png)).convert("RGBA")


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for p in list(OUT.iterdir()):
        if p.is_file():
            p.unlink()
        elif p.is_dir():
            for c in p.iterdir():
                c.unlink()
            p.rmdir()

    compare_clear: list[tuple[str, Image.Image]] = []
    compare_aban: list[tuple[str, Image.Image]] = []

    for sid, title in STYLES:
        folder = OUT / sid
        folder.mkdir(exist_ok=True)
        rows: list[tuple[str, Image.Image]] = []
        for tier, detail in CLEAR_FIXTURES:
            im = await _card("complete", tier, detail, sid)
            im.save(folder / f"complete_{tier}.png")
            rows.append((f"{sid} · cleared · {tier}", im))
            if tier == "easy":
                compare_clear.append((f"{sid} — {title}", im))
        for tier, detail in ABANDON_FIXTURES:
            im = await _card("abandon", tier, detail, sid)
            im.save(folder / f"abandon_{tier}.png")
            rows.append((f"{sid} · abandoned · {tier}", im))
            if tier == "medium":
                compare_aban.append((f"{sid} — {title}", im))
        _sheet(rows, OUT / f"sheet_{sid}.png")

    _sheet(compare_clear, OUT / "sheet_compare_cleared_easy.png")
    _sheet(compare_aban, OUT / "sheet_compare_abandoned_medium.png")

    (OUT / "README.md").write_text(
        "# CLEARED / ABANDONED hero styles (correct type size)\n\n"
        "Rendered through the real card pipeline (`SCALE=2`) — same size as production.\n\n"
        "| Id | Idea |\n|----|------|\n"
        + "\n".join(f"| **{a}** | {b} |" for a, b in STYLES)
        + "\n\n"
        "**Compare:** `sheet_compare_cleared_easy.png` · "
        "`sheet_compare_abandoned_medium.png`\n\n"
        "Reply with an id (e.g. `3_symbol_lead`).\n"
        "Regenerate: `PYTHONPATH=. python scripts/render_terminal_hero_protos.py`\n",
        encoding="utf-8",
    )
    print("INDEX", OUT / "README.md")


if __name__ == "__main__":
    asyncio.run(main())
