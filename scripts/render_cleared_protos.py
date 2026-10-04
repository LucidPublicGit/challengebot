"""Completion card — Cleared text style options (no DONE pill).

Writes data/card_previews/prototypes/cleared_styles/
"""
from __future__ import annotations

import asyncio
import io
from pathlib import Path

from PIL import Image, ImageDraw

from app.challenge_card import _font, demo_avatar_png, render_challenge_card

OUT = Path("data/card_previews/prototypes/cleared_styles")
AV = demo_avatar_png()

STYLES = [
    ("D_soft_glow", "Large Cleared in tier color + progress bar"),
    ("B_check_tier", "Check + CLEARED uppercase, tier ink"),
    ("C_gold_underline", "Bright Cleared + tier underline"),
    ("D_soft_glow", "Tier Cleared with soft glow edges"),
    ("E_label_stack", "Small CLEARED label, big progress numbers"),
]

FIXTURES = [
    ("easy", "5 / 5 volumes · Tadoku"),
    ("medium", "10 / 10 volumes · Tadoku"),
    ("hard", "20 / 20 episodes"),
    ("ota_master", "200k / 200k characters"),
]


def _sheet(rows: list[tuple[str, Image.Image]], path: Path) -> None:
    gap, pad = 10, 14
    lab_h = 26
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
    print("wrote", path, canvas.size)


async def one(tier: str, detail: str, style: str) -> Image.Image:
    png = await render_challenge_card(
        kind="complete",
        display_name="Challenger",
        challenge_title="Manga Mini-Marathon",
        tier=tier,
        number=1,
        avatar_bytes=AV,
        focus="One Piece",
        line_primary="Cleared",
        line_secondary=detail,
        footer="",
        cleared_style=style,
    )
    return Image.open(io.BytesIO(png)).convert("RGBA")


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    all_compare: list[tuple[str, Image.Image]] = []

    for sid, title in STYLES:
        folder = OUT / sid
        folder.mkdir(exist_ok=True)
        rows: list[tuple[str, Image.Image]] = []
        for tier, detail in FIXTURES:
            im = await one(tier, detail, sid)
            im.save(folder / f"{tier}.png")
            rows.append((f"{sid} · {tier}", im))
            if tier == "easy":
                all_compare.append((f"{sid} — {title}", im))
        _sheet(rows, OUT / f"sheet_{sid}.png")

    _sheet(all_compare, OUT / "sheet_all_styles_easy.png")

    (OUT / "README.md").write_text(
        "# Cleared text styles (complete card)\n\n"
        "- **DONE pill removed** on complete cards\n"
        "- Side tick / rails stay **tier-themed**\n\n"
        "| Id | Look |\n|----|------|\n"
        + "\n".join(f"| **{a}** | {b} |" for a, b in STYLES)
        + "\n\nCompare all on easy: `sheet_all_styles_easy.png`\n\n"
        "Reply with a style id (e.g. `B_check_tier`).\n"
        "Default in code until you pick: `D_soft_glow`.\n\n"
        "Regenerate: `PYTHONPATH=. python scripts/render_cleared_protos.py`\n",
        encoding="utf-8",
    )
    print("INDEX", OUT / "README.md")


if __name__ == "__main__":
    asyncio.run(main())
