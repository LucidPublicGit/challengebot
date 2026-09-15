"""Regenerate data/card_previews for the current card/NC design.

  PYTHONPATH=. python scripts/regen_card_previews.py
"""
from __future__ import annotations

import asyncio
import io
from pathlib import Path

from PIL import Image

from app.challenge_card import (
    demo_avatar_png,
    render_all_demo_cards,
    render_challenge_card,
)
from app.new_challenger import list_templates, render_new_challenger

OUT = Path("data/card_previews")
AV = demo_avatar_png()

STALE_PREFIXES = (
    "debug_",
    "mask_",
    "unicode_",
    "new_challenger_wired",
    "new_challenger_example_nc",
    "new_challenger_tier_ota",
    "congrats_framed_",
)


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for p in list(OUT.glob("*")):
        if p.is_file() and any(p.name.startswith(s) for s in STALE_PREFIXES):
            p.unlink()
            print("deleted", p.name)

    cards = await render_all_demo_cards(avatar=AV, fetch_covers=True)
    for kind, png in cards:
        path = OUT / f"card_{kind}.png"
        path.write_bytes(png)
        print(path.name, len(png))

    cover = Image.new("RGB", (200, 300), (40, 80, 160))
    buf = io.BytesIO()
    cover.save(buf, format="PNG")
    png = await render_challenge_card(
        kind="complete",
        display_name="Monty",
        challenge_title="VN 30k",
        tier="easy",
        number=4,
        avatar_bytes=AV,
        focus="Steins;Gate",
        line_primary="Cleared",
        line_secondary="32.1k / 30k characters · Tadoku",
        footer="",
        poster_bytes=buf.getvalue(),
    )
    (OUT / "card_with_poster.png").write_bytes(png)
    print("card_with_poster.png", len(png))

    png = await render_challenge_card(
        kind="start",
        display_name="Monty",
        challenge_title="Very Long Challenge Title That Wraps Nicely",
        tier="medium",
        number=9,
        avatar_bytes=AV,
        focus="Super Long Focus Name That Should Truncate Gracefully Somewhere",
        line_primary="Good luck",
        line_secondary="Ends Sat 28 Mar 18:00 UTC",
        footer="",
    )
    (OUT / "card_focus_long.png").write_bytes(png)
    print("card_focus_long.png", len(png))

    for tier in ("easy", "medium", "hard", "ota_master"):
        png = await render_challenge_card(
            kind="complete",
            display_name="Monty",
            challenge_title="Manga Mini-Marathon",
            tier=tier,
            number=1,
            avatar_bytes=AV,
            focus="One Piece",
            line_primary="Cleared",
            line_secondary="5 / 5 volumes · Tadoku",
            footer="",
        )
        (OUT / f"tier_preview_{tier}.png").write_bytes(png)
        print(f"tier_preview_{tier}.png", len(png))

    def _save(path: Path, img: Image.Image) -> None:
        """Atomic-ish write (avoids Windows lock on open preview files)."""
        tmp = path.with_name(path.stem + ".tmp.png")
        img.save(tmp)
        tmp.replace(path)

    def _stack_v(*imgs: Image.Image, gap: int = 12, pad: int = 16) -> Image.Image:
        """Vertical stack, center-aligned, dark mock background."""
        w = max(im.size[0] for im in imgs)
        h = sum(im.size[1] for im in imgs) + gap * (len(imgs) - 1) + pad * 2
        canvas = Image.new("RGB", (w + pad * 2, h), (32, 34, 40))
        y = pad
        for im in imgs:
            im = im.convert("RGBA")
            x = pad + (w - im.size[0]) // 2
            canvas.paste(im, (x, y), im)
            y += im.size[1] + gap
        return canvas

    templates = list_templates()
    nc_easy: bytes | None = None
    for i, tpl in enumerate(templates):
        tiers = ("easy", "ota_master") if i == 0 else ("hard",)
        for tier in tiers:
            nc = render_new_challenger(AV, template=tpl, tier=tier)
            if not nc:
                continue
            name = f"new_challenger_{tpl.stem}_{tier}.png"
            (OUT / name).write_bytes(nc)
            print(name, len(nc))
            if i == 0 and tier == "easy":
                (OUT / "new_challenger_example.png").write_bytes(nc)
                from app.challenge_card import render_unified_start_card

                unified = await render_unified_start_card(
                    display_name="Monty",
                    challenge_title="Manga Mini-Marathon",
                    tier="easy",
                    number=1,
                    avatar_bytes=AV,
                    focus="One Piece",
                    meta_line="24 hours · Ends 03/28/26 18:00",
                    include_challenger=True,
                )
                (OUT / "preview_unified_start.png").write_bytes(unified)
                (OUT / "unified_start_live.png").write_bytes(unified)
                print("preview_unified_start.png", len(unified))
                for stale in (
                    "start_chip_example.png",
                    "preview_start_pair.png",
                    "preview_challenger_plus_card.png",
                ):
                    sp = OUT / stale
                    if sp.exists():
                        sp.unlink()
                        print("deleted", stale)

    names = sorted(p.name for p in OUT.glob("*.png"))
    (OUT / "README.txt").write_text(
        "ChallengeTracker card previews (current design)\n"
        "- preview_unified_start.png — prod start (NC inside one card)\n"
        "- Cleared/Abandoned: stamp-outline; other kinds: soft-glow\n"
        "- Tier side-tick; no corner status pills\n\n"
        "Regenerate: PYTHONPATH=. python scripts/regen_card_previews.py\n\n"
        + "\n".join(names)
        + "\n",
        encoding="utf-8",
    )
    print("README.txt", len(names), "pngs")


if __name__ == "__main__":
    asyncio.run(main())
