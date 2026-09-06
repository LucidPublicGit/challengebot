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
    render_event_chip,
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
                nc_easy = nc
                (OUT / "new_challenger_example.png").write_bytes(nc)
                w = Image.open(io.BytesIO(nc)).size[0]
                chip = await render_event_chip(
                    primary="Good luck",
                    secondary="Ends Sat 28 Mar 18:00 UTC",
                    tier="easy",
                    width=w,
                )
                (OUT / "start_chip_example.png").write_bytes(chip)
                print("start_chip_example.png", len(chip))

                # Full start pair as Discord shows it (layout E: NC + chip)
                nc_im = Image.open(io.BytesIO(nc)).convert("RGBA")
                chip_im = Image.open(io.BytesIO(chip)).convert("RGBA")
                pair_chip = _stack_v(nc_im, chip_im)
                pair_chip.save(OUT / "preview_start_pair.png")
                print("preview_start_pair.png", pair_chip.size)

                # Same pair but bottom = full Started card (design reference)
                start_png = await render_challenge_card(
                    kind="start",
                    display_name="Monty",
                    challenge_title="Manga Mini-Marathon",
                    tier="easy",
                    number=1,
                    avatar_bytes=AV,
                    focus="One Piece",
                    line_primary="Good luck",
                    line_secondary="Ends Sat 28 Mar 18:00 UTC",
                    footer="",
                )
                start_im = Image.open(io.BytesIO(start_png)).convert("RGBA")
                # Match widths: scale start card to NC width
                if start_im.size[0] != nc_im.size[0]:
                    nh = max(
                        1, int(start_im.size[1] * nc_im.size[0] / start_im.size[0])
                    )
                    start_im = start_im.resize(
                        (nc_im.size[0], nh), Image.Resampling.LANCZOS
                    )
                pair_card = _stack_v(nc_im, start_im)
                pair_card.save(OUT / "preview_challenger_plus_card.png")
                print("preview_challenger_plus_card.png", pair_card.size)

    names = sorted(p.name for p in OUT.glob("*.png"))
    (OUT / "README.txt").write_text(
        "ChallengeTracker card previews (current design)\n"
        "- preview_start_pair.png — layout E as posted (NC + tier chip)\n"
        "- preview_challenger_plus_card.png — NC + full Started card\n"
        "- Cards: in-card hero status (Started/Cleared/…), no corner pill\n"
        "- Tier side-tick; Cleared/status soft-glow; short status text-only in channel\n\n"
        "Regenerate: PYTHONPATH=. python scripts/regen_card_previews.py\n\n"
        + "\n".join(names)
        + "\n",
        encoding="utf-8",
    )
    print("README.txt", len(names), "pngs")


if __name__ == "__main__":
    asyncio.run(main())
