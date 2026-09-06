"""Outcome-panel color prototypes — keep tier theme, quiet the nested box.

Writes data/card_previews/prototypes/panel_*/ 
"""
from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageDraw

from app.challenge_card import (
    ACCENT_AMBER,
    ACCENT_GREEN,
    ACCENT_PURPLE,
    ACCENT_RED,
    BG,
    HAIRLINE,
    INK,
    INK_SOFT,
    KIND_STYLE,
    PANEL_BG,
    SCALE,
    TIER_COLOR,
    TIER_LABEL,
    WIDTH,
    _circular_avatar,
    _clip_round,
    _font,
    _mix,
    _oneline,
    _tier_flair,
    _truncate,
    demo_avatar_png,
)

OUT = Path("data/card_previews/prototypes/panel_styles")
AV = demo_avatar_png()
S = SCALE

# (id, title, blurb)
STYLES = [
    (
        "0_current",
        "Current",
        "Kind-tinted fill + kind left bar + outline (nested colored card)",
    ),
    (
        "1_flat_rules",
        "Flat rules",
        "No box — hairline divider only; tier stays on rail/pill/avatar",
    ),
    (
        "2_tier_panel",
        "Tier panel",
        "Nested box uses difficulty color (not kind purple/green)",
    ),
    (
        "3_ghost",
        "Ghost panel",
        "Same BG as card, no fill tint — only thin hairline + tiny tier tick",
    ),
    (
        "4_underline",
        "Underline",
        "No box — primary text with tier underline; meta soft below",
    ),
    (
        "5_side_tick",
        "Side tick only",
        "No fill/outline — single tier-colored tick + plain text",
    ),
]


def _card(
    *,
    style: str,
    kind: str,
    tier: str,
    primary: str,
    secondary: str,
    title: str = "Manga Mini-Marathon",
    name: str = "Monty",
    number: int = 1,
) -> Image.Image:
    badge, kind_accent = KIND_STYLE.get(kind, ("EVENT", ACCENT_PURPLE))
    tier_key = tier.lower()
    tier_color = TIER_COLOR.get(tier_key, INK_SOFT)
    flair = _tier_flair(tier_key)
    W = WIDTH
    H = 220
    img = Image.new("RGBA", (W * S, H * S), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    outer = (0, 0, W * S - 1, H * S - 1)
    draw.rounded_rectangle(outer, radius=22 * S, fill=BG)

    # soft tier wash (shared)
    wash_w = int(W * S * float(flair.get("wash_width") or 0.3))
    peak = float(flair.get("wash") or 0.05)
    for i in range(10):
        t = peak * (1 - i / 9)
        col = _mix(tier_color, BG, t)
        x0 = i * (wash_w // 10)
        x1 = wash_w if i == 9 else (i + 1) * (wash_w // 10)
        draw.rectangle((x0, 0, x1, H * S), fill=col)

    # single kind rail (production lean)
    rail = 4 * S
    draw.rounded_rectangle((0, 0, rail, H * S), radius=6 * S, fill=kind_accent)

    # avatar + tier ring
    av = 64 * S
    avatar = _circular_avatar(AV, av)
    ax, ay = 22 * S, 22 * S
    img.paste(avatar, (ax, ay), avatar)
    draw.ellipse((ax, ay, ax + av, ay + av), outline=tier_color, width=2 * S)

    cx = ax + av + 16 * S
    right = (W - 24) * S
    y = 20 * S

    # kind pill
    pf = _font(11 * S, True)
    pw = draw.textlength(badge, font=pf) + 24 * S
    ph = pf.size + 10 * S
    px = right - pw
    draw.rounded_rectangle(
        (px, y + 4 * S, px + pw, y + 4 * S + ph),
        radius=ph / 2,
        fill=_mix(kind_accent, BG, 0.22),
        outline=_mix(kind_accent, BG, 0.45),
        width=max(1, S // 2),
    )
    draw.text(
        (px + pw / 2, y + 4 * S + ph / 2),
        badge,
        font=pf,
        fill=kind_accent,
        anchor="mm",
    )
    draw.text(
        (cx, y),
        _truncate(draw, name, _font(22 * S, True), px - cx - 12 * S),
        font=_font(22 * S, True),
        fill=INK,
    )
    y += 36 * S

    # tier pill
    tlab = TIER_LABEL.get(tier_key, tier).upper()
    tw = draw.textlength(tlab, font=pf) + 20 * S
    th = pf.size + 8 * S
    draw.rounded_rectangle(
        (cx, y, cx + tw, y + th),
        radius=th / 2,
        fill=_mix(tier_color, BG, 0.22),
        outline=_mix(tier_color, BG, 0.5),
        width=max(1, S // 2),
    )
    draw.text((cx + tw / 2, y + th / 2), tlab, font=pf, fill=tier_color, anchor="mm")
    draw.text(
        (cx + tw + 10 * S, y + th / 2),
        f"#{number}",
        font=_font(13 * S, True),
        fill=INK_SOFT,
        anchor="lm",
    )
    y += th + 10 * S
    draw.text((cx, y), title, font=_font(18 * S, True), fill=INK)
    y += 32 * S

    # --- outcome region (the thing we're prototyping) ---
    primary = _oneline(primary)
    secondary = _oneline(secondary)
    oh = 72 * S
    box = (cx, y, right, y + oh)
    pfont = _font(18 * S, True)
    sfont = _font(14 * S)

    if style == "0_current":
        fill = _mix(kind_accent, PANEL_BG, 0.10)
        line = _mix(kind_accent, HAIRLINE, 0.25)
        draw.rounded_rectangle(box, radius=12 * S, fill=fill, outline=line, width=S)
        draw.rounded_rectangle(
            (cx, y, cx + 5 * S, y + oh), radius=3 * S, fill=kind_accent
        )
        draw.text((cx + 16 * S, y + 14 * S), primary, font=pfont, fill=INK)
        draw.text((cx + 16 * S, y + 42 * S), secondary, font=sfont, fill=INK_SOFT)

    elif style == "1_flat_rules":
        draw.line((cx, y, right, y), fill=HAIRLINE, width=S)
        draw.text((cx, y + 14 * S), primary, font=pfont, fill=INK)
        draw.text((cx, y + 42 * S), secondary, font=sfont, fill=INK_SOFT)
        draw.line((cx, y + oh, right, y + oh), fill=HAIRLINE, width=max(1, S // 2))

    elif style == "2_tier_panel":
        fill = _mix(tier_color, BG, 0.08)
        line = _mix(tier_color, HAIRLINE, 0.35)
        draw.rounded_rectangle(box, radius=12 * S, fill=fill, outline=line, width=S)
        draw.rounded_rectangle(
            (cx, y, cx + 5 * S, y + oh), radius=3 * S, fill=tier_color
        )
        draw.text((cx + 16 * S, y + 14 * S), primary, font=pfont, fill=INK)
        draw.text((cx + 16 * S, y + 42 * S), secondary, font=sfont, fill=INK_SOFT)

    elif style == "3_ghost":
        draw.rounded_rectangle(
            box, radius=12 * S, fill=BG, outline=HAIRLINE, width=max(1, S // 2)
        )
        draw.rectangle((cx, y + 10 * S, cx + 3 * S, y + oh - 10 * S), fill=tier_color)
        draw.text((cx + 14 * S, y + 14 * S), primary, font=pfont, fill=INK)
        draw.text((cx + 14 * S, y + 42 * S), secondary, font=sfont, fill=INK_SOFT)

    elif style == "4_underline":
        draw.text((cx, y + 10 * S), primary, font=pfont, fill=INK)
        twp = draw.textlength(primary, font=pfont)
        uy = y + 10 * S + pfont.size + 4 * S
        draw.line((cx, uy, cx + twp, uy), fill=tier_color, width=2 * S)
        draw.text((cx, uy + 10 * S), secondary, font=sfont, fill=INK_SOFT)

    elif style == "5_side_tick":
        draw.rectangle((cx, y + 8 * S, cx + 3 * S, y + oh - 8 * S), fill=tier_color)
        draw.text((cx + 14 * S, y + 14 * S), primary, font=pfont, fill=INK)
        draw.text((cx + 14 * S, y + 42 * S), secondary, font=sfont, fill=INK_SOFT)

    else:
        draw.text((cx, y), primary, font=pfont, fill=INK)

    draw.text(
        (cx, (H - 22) * S),
        "ChallengeTracker",
        font=_font(11 * S),
        fill=INK_SOFT,
    )
    edge = _mix(tier_color, HAIRLINE, 0.28)
    _clip_round(img, outer, 22 * S, edge, S)
    return img.resize((W, H), Image.Resampling.LANCZOS)


def _label_strip(text: str, w: int = 720) -> Image.Image:
    h = 36
    im = Image.new("RGB", (w, h), (40, 42, 48))
    d = ImageDraw.Draw(im)
    d.text((12, 8), text[:110], font=_font(14), fill=(180, 184, 192))
    return im


def _stack(parts: list[Image.Image], gap: int = 10) -> Image.Image:
    w = max(p.size[0] for p in parts)
    h = sum(p.size[1] for p in parts) + gap * (len(parts) - 1) + 20
    canvas = Image.new("RGB", (w + 24, h + 24), (32, 34, 40))
    y = 12
    for p in parts:
        if p.mode == "RGBA":
            canvas.paste(p, (12, y), p)
        else:
            canvas.paste(p, (12, y))
        y += p.size[1] + gap
    return canvas


# fixtures: kind × tier pairs that show color clash clearly
SAMPLES = [
    ("start", "easy", "Good luck", "Ends Sat 28 Mar 18:00 UTC"),
    ("complete", "easy", "Cleared", "5 / 5 volumes · Tadoku"),
    ("fail", "medium", "Goal not met", "6.2 / 10 volumes"),
    ("complete", "hard", "Cleared", "20 / 20 episodes"),
    ("start", "ota_master", "Good luck", "Ends Sun 29 Mar 12:00 UTC"),
]


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    index_rows = []
    for sid, title, blurb in STYLES:
        folder = OUT / sid
        folder.mkdir(exist_ok=True)
        parts: list[Image.Image] = [_label_strip(f"{sid} — {title}: {blurb}")]
        for kind, tier, primary, secondary in SAMPLES:
            im = _card(
                style=sid,
                kind=kind,
                tier=tier,
                primary=primary,
                secondary=secondary,
            )
            name = f"{kind}_{tier}.png"
            im.save(folder / name)
            parts.append(_label_strip(f"{kind} · {tier}"))
            parts.append(im)
        sheet = _stack(parts)
        sheet_path = OUT / f"sheet_{sid}.png"
        sheet.save(sheet_path)
        print("wrote", sheet_path)
        index_rows.append(f"| **{sid}** | {title} | {blurb} | `sheet_{sid}.png` |")

    (OUT / "README.md").write_text(
        "# Outcome panel color prototypes\n\n"
        "Problem: nested “card in card” picks up **kind** accent (purple/green/red) "
        "on top of **tier** theme → muddy overlays.\n\n"
        "Tier theming kept on: left wash, avatar ring, tier pill, outer edge.\n\n"
        "| Id | Name | Idea | Sheet |\n|----|------|------|-------|\n"
        + "\n".join(index_rows)
        + "\n\nEach sheet shows: start/easy, complete/easy, fail/medium, "
        "complete/hard, start/ota_master.\n\n"
        "Reply with a style id (e.g. `3_ghost` or `1_flat_rules`).\n"
        "Regenerate: `PYTHONPATH=. python scripts/render_panel_protos.py`\n",
        encoding="utf-8",
    )
    print("INDEX", OUT / "README.md")


if __name__ == "__main__":
    main()
