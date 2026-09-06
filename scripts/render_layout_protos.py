"""Generate announce layout prototypes A–E for visual pick."""
from __future__ import annotations

import asyncio
import io
from pathlib import Path

from PIL import Image, ImageDraw

from app.challenge_card import (
    ACCENT_GREEN,
    ACCENT_PURPLE,
    BG,
    INK,
    INK_SOFT,
    _circular_avatar,
    _font,
    _mix,
    demo_avatar_png,
    render_challenge_card,
)
from app.new_challenger import list_templates, render_new_challenger

ROOT = Path("data/card_previews/prototypes")
AV = demo_avatar_png()
NAME = "Monty"
TITLE = "Manga Mini-Marathon"
TIER = "easy"
FOCUS = "One Piece"


def mock_discord_message(
    *,
    caption_lines: list[str],
    images: list[Image.Image],
    label: str,
    out: Path,
    width: int = 520,
) -> None:
    pad = 16
    gap = 10
    body_f = _font(15)
    label_f = _font(13, bold=True)
    line_h = 22
    text_h = 28 + len(caption_lines) * line_h
    imgs_h = 0
    scaled: list[Image.Image] = []
    max_img_w = width - 2 * pad
    for im in images:
        im = im.convert("RGBA")
        w, h = im.size
        if w > max_img_w:
            nh = max(1, int(h * max_img_w / w))
            im = im.resize((max_img_w, nh), Image.Resampling.LANCZOS)
        scaled.append(im)
        imgs_h += im.size[1] + gap
    H = pad + text_h + imgs_h + pad + 24
    canvas = Image.new("RGB", (width, max(H, 80)), (49, 51, 56))
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle((8, 8, width - 8, H - 8), radius=8, fill=(43, 45, 49))
    y = pad + 4
    draw.text((pad + 4, y), label, font=label_f, fill=(148, 155, 164))
    y += 22
    for line in caption_lines:
        draw.text((pad + 4, y), line[:90], font=body_f, fill=(220, 221, 222))
        y += line_h
    y += 6
    for im in scaled:
        # center-align image in the mock panel
        x = pad + max(0, (max_img_w - im.size[0]) // 2)
        canvas.paste(im, (x, y), im if im.mode == "RGBA" else None)
        y += im.size[1] + gap
    canvas.save(out)
    print("wrote", out, canvas.size)


def status_strip(
    *,
    primary: str,
    secondary: str,
    name: str = NAME,
    title: str = TITLE,
    avatar: bytes = AV,
) -> Image.Image:
    S = 2
    W, H = 720, 120
    img = Image.new("RGBA", (W * S, H * S), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    outer = (0, 0, W * S - 1, H * S - 1)
    draw.rounded_rectangle(outer, radius=16 * S, fill=BG)
    draw.rectangle((0, 0, 6 * S, H * S), fill=ACCENT_PURPLE)
    av = _circular_avatar(avatar, 56 * S)
    img.paste(av, (20 * S, 32 * S), av)
    draw.text((92 * S, 18 * S), primary, font=_font(22 * S, True), fill=INK)
    draw.text((92 * S, 48 * S), f"{name} · {title}", font=_font(14 * S), fill=INK_SOFT)
    draw.text((92 * S, 72 * S), secondary, font=_font(14 * S), fill=INK_SOFT)
    pill = "STATUS"
    pf = _font(11 * S, True)
    pw = draw.textlength(pill, font=pf) + 20 * S
    px = (W - 28) * S - pw
    draw.rounded_rectangle(
        (px, 20 * S, px + pw, 42 * S),
        radius=12 * S,
        fill=_mix(ACCENT_PURPLE, BG, 0.25),
    )
    draw.text((px + pw / 2, 31 * S), pill, font=pf, fill=ACCENT_PURPLE, anchor="mm")
    return img.resize((W, H), Image.Resampling.LANCZOS)


def micro_chip(
    *,
    primary: str = "Good luck",
    secondary: str = "Ends Sat 28 Mar 18:00 UTC",
) -> Image.Image:
    S = 2
    W, H = 720, 100
    img = Image.new("RGBA", (W * S, H * S), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((0, 0, W * S - 1, H * S - 1), radius=14 * S, fill=BG)
    draw.rectangle((0, 0, 5 * S, H * S), fill=ACCENT_GREEN)
    draw.text((18 * S, 22 * S), primary, font=_font(20 * S, True), fill=INK)
    draw.text((18 * S, 54 * S), secondary, font=_font(14 * S), fill=INK_SOFT)
    return img.resize((W, H), Image.Resampling.LANCZOS)


def text_thumb_mock(
    *,
    title_line: str,
    fields: list[tuple[str, str]],
    avatar: bytes = AV,
) -> Image.Image:
    S = 2
    W, H = 480, 200
    img = Image.new("RGBA", (W * S, H * S), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle(
        (0, 0, W * S - 1, H * S - 1), radius=12 * S, fill=(47, 49, 54, 255)
    )
    av = _circular_avatar(avatar, 64 * S)
    img.paste(av, ((W - 88) * S, 24 * S), av)
    draw.text((20 * S, 20 * S), title_line, font=_font(18 * S, True), fill=INK)
    y = 56 * S
    for k, v in fields:
        draw.text((20 * S, y), k.upper(), font=_font(11 * S, True), fill=INK_SOFT)
        draw.text((20 * S, y + 18 * S), v, font=_font(15 * S), fill=INK)
        y += 48 * S
    return img.resize((W, H), Image.Resampling.LANCZOS)


async def main() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    templates = list_templates()
    tpl = templates[0] if templates else None
    nc = render_new_challenger(AV, template=tpl, tier=TIER)
    if not nc:
        raise SystemExit("need new_challenger template under ./new_challenger")
    nc_im = Image.open(io.BytesIO(nc)).convert("RGBA")

    card_png = await render_challenge_card(
        kind="start",
        display_name=NAME,
        challenge_title=TITLE,
        tier=TIER,
        number=1,
        avatar_bytes=AV,
        focus=FOCUS,
        line_primary="Good luck",
        line_secondary="Ends Sat 28 Mar 18:00 UTC",
        footer="",
    )
    card_im = Image.open(io.BytesIO(card_png)).convert("RGBA")

    status_card_png = await render_challenge_card(
        kind="status",
        display_name=NAME,
        challenge_title=TITLE,
        tier=TIER,
        number=1,
        avatar_bytes=AV,
        focus=FOCUS,
        line_primary="Halfway through the window",
        line_secondary="4h 0m left · timer checkpoint",
        footer="",
    )
    status_card_im = Image.open(io.BytesIO(status_card_png)).convert("RGBA")

    complete_png = await render_challenge_card(
        kind="complete",
        display_name=NAME,
        challenge_title=TITLE,
        tier=TIER,
        number=1,
        avatar_bytes=AV,
        focus=FOCUS,
        line_primary="Cleared",
        line_secondary="5 / 5 volumes",
        footer="",
    )
    complete_im = Image.open(io.BytesIO(complete_png)).convert("RGBA")

    # A
    a = ROOT / "A_nc_plus_text"
    a.mkdir(exist_ok=True)
    nc_im.save(a / "start_image.png")
    mock_discord_message(
        label="PROTO A — start (NC + text only)",
        caption_lines=[
            "Easy #1 · @Monty started Manga Mini-Marathon · One Piece",
            "Ends Sat 28 Mar 18:00 (in 12 hours)",
        ],
        images=[nc_im],
        out=a / "mock_start.png",
    )
    mock_discord_message(
        label="PROTO A — status (text only, no image)",
        caption_lines=[
            "Easy #1 · @Monty · Halfway through the window · Manga Mini-Marathon",
            "4h 0m left · timer checkpoint",
            "Ends Sat 28 Mar 18:00 (in 4 hours)",
        ],
        images=[],
        out=a / "mock_status.png",
    )
    mock_discord_message(
        label="PROTO A — complete (calm card)",
        caption_lines=["Easy #1 · @Monty completed Manga Mini-Marathon"],
        images=[complete_im],
        out=a / "mock_complete.png",
    )

    # B
    b = ROOT / "B_status_strip"
    b.mkdir(exist_ok=True)
    strip = status_strip(
        primary="Halfway through the window",
        secondary="4h 0m left · timer checkpoint",
    )
    strip.save(b / "status_image.png")
    nc_im.save(b / "start_image.png")
    mock_discord_message(
        label="PROTO B — start (NC + text)",
        caption_lines=[
            "Easy #1 · @Monty started Manga Mini-Marathon · One Piece",
            "Ends Sat 28 Mar 18:00 (in 12 hours)",
        ],
        images=[nc_im],
        out=b / "mock_start.png",
    )
    mock_discord_message(
        label="PROTO B — status (short banner, not log card)",
        caption_lines=["Easy #1 · @Monty · checkpoint"],
        images=[strip],
        out=b / "mock_status.png",
    )
    mock_discord_message(
        label="PROTO B — complete",
        caption_lines=["Easy #1 · @Monty completed Manga Mini-Marathon"],
        images=[complete_im],
        out=b / "mock_complete.png",
    )

    # C
    c = ROOT / "C_calm_card_only"
    c.mkdir(exist_ok=True)
    card_im.save(c / "start_image.png")
    status_card_im.save(c / "status_image.png")
    complete_im.save(c / "complete_image.png")
    mock_discord_message(
        label="PROTO C — start (lifecycle card only, no NC)",
        caption_lines=[
            "Easy #1 · @Monty started Manga Mini-Marathon · One Piece",
            "Ends Sat 28 Mar 18:00 (in 12 hours)",
        ],
        images=[card_im],
        out=c / "mock_start.png",
    )
    mock_discord_message(
        label="PROTO C — status (full card)",
        caption_lines=["Easy #1 · @Monty · Halfway through the window"],
        images=[status_card_im],
        out=c / "mock_status.png",
    )
    mock_discord_message(
        label="PROTO C — complete",
        caption_lines=["Easy #1 · @Monty completed Manga Mini-Marathon"],
        images=[complete_im],
        out=c / "mock_complete.png",
    )

    # D
    d = ROOT / "D_text_thumb"
    d.mkdir(exist_ok=True)
    start_emb = text_thumb_mock(
        title_line="Monty started a challenge",
        fields=[
            ("Challenge", "Easy #1 · Manga Mini-Marathon"),
            ("Focus", "One Piece"),
            ("Ends", "Sat 28 Mar 18:00 UTC"),
        ],
    )
    status_emb = text_thumb_mock(
        title_line="Halfway through the window",
        fields=[
            ("Challenge", "Easy #1 · Manga Mini-Marathon"),
            ("Time left", "4h 0m"),
            ("Note", "timer checkpoint"),
        ],
    )
    complete_emb = text_thumb_mock(
        title_line="Challenge cleared",
        fields=[
            ("Challenge", "Easy #1 · Manga Mini-Marathon"),
            ("Result", "5 / 5 volumes"),
            ("By", "Monty"),
        ],
    )
    start_emb.save(d / "start_image.png")
    status_emb.save(d / "status_image.png")
    complete_emb.save(d / "complete_image.png")
    mock_discord_message(
        label="PROTO D — start (embed fields + thumb)",
        caption_lines=["@Monty started Easy #1"],
        images=[start_emb],
        out=d / "mock_start.png",
    )
    mock_discord_message(
        label="PROTO D — status",
        caption_lines=["checkpoint"],
        images=[status_emb],
        out=d / "mock_status.png",
    )
    mock_discord_message(
        label="PROTO D — complete",
        caption_lines=["cleared"],
        images=[complete_emb],
        out=d / "mock_complete.png",
    )

    # E
    e = ROOT / "E_nc_plus_micro"
    e.mkdir(exist_ok=True)
    chip = micro_chip()
    chip.save(e / "micro_chip.png")
    nc_im.save(e / "start_nc.png")
    mock_discord_message(
        label="PROTO E — start (NC + micro chip — two images)",
        caption_lines=[
            "Easy #1 · @Monty started Manga Mini-Marathon · One Piece",
            "Ends Sat 28 Mar 18:00 (in 12 hours)",
        ],
        images=[nc_im, chip],
        out=e / "mock_start.png",
    )
    strip2 = status_strip(
        primary="Under 1 hour left",
        secondary="52m left · final stretch",
    )
    mock_discord_message(
        label="PROTO E — status (strip)",
        caption_lines=["Easy #1 · final stretch"],
        images=[strip2],
        out=e / "mock_status.png",
    )
    mock_discord_message(
        label="PROTO E — complete",
        caption_lines=["Easy #1 · completed"],
        images=[complete_im],
        out=e / "mock_complete.png",
    )

    (ROOT / "README.md").write_text(
        """# Announce layout prototypes — pick one

Open each folder’s `mock_*.png` (Discord-ish mock). Raw assets alongside.

| Proto | Folder | Start | Status | Complete | Notes |
|-------|--------|-------|--------|----------|-------|
| **A** | `A_nc_plus_text` | NC banner + text | **Text only** | Calm card | Peak NC, quiet status. **Current prod lean.** |
| **B** | `B_status_strip` | NC + text | Short milestone banner | Calm card | Status has presence without log-card clone |
| **C** | `C_calm_card_only` | Lifecycle card only | Full status card | Calm card | No NC in channel |
| **D** | `D_text_thumb` | Embed + avatar thumb | Same pattern | Same | Max text, min art |
| **E** | `E_nc_plus_micro` | NC + thin chip | Strip | Calm card | Two images on start — heavier |

## How to choose

Reply with a letter (or mix), e.g.:
- `A` (whole family)
- `A start/status, C complete`
- `B`

Already in code (can still switch): A-like start (NC+text), A-like status (text),
no embed chrome, short-window **50%** only.

Regenerate: `python scripts/render_layout_protos.py`
""",
        encoding="utf-8",
    )
    print("INDEX", ROOT / "README.md")
    for p in sorted(ROOT.rglob("mock_*.png")):
        print(p)


if __name__ == "__main__":
    asyncio.run(main())
