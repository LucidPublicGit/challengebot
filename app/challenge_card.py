"""Dark challenge cards styled after TadokuBot (Pillow PNG)."""

from __future__ import annotations

import asyncio
import io
from typing import Optional

from PIL import Image, ImageChops, ImageDraw, ImageFont

SCALE = 2
WIDTH = 920
MARGIN = 36

BG = (30, 31, 38)
INK = (236, 237, 242)
INK_SOFT = (150, 152, 166)
HAIRLINE = (56, 58, 68)
PANEL_BG = (40, 42, 51)
CALLOUT_BG = (44, 46, 56)
PLACEHOLDER_BG = (58, 60, 72)
ACCENT_PURPLE = (150, 128, 226)
ACCENT_GREEN = (61, 214, 140)
ACCENT_RED = (240, 113, 120)
ACCENT_AMBER = (230, 192, 123)
ACCENT_BLUE = (91, 156, 250)

KIND_STYLE = {
    "start": ("CHALLENGE STARTED", ACCENT_PURPLE),
    "scheduled": ("SCHEDULED", ACCENT_BLUE),
    "active": ("ACTIVE CHALLENGE", ACCENT_PURPLE),
    "complete": ("COMPLETED", ACCENT_GREEN),
    "fail": ("FAILED CHECK", ACCENT_RED),
    "abandon": ("ABANDONED", ACCENT_AMBER),
    "expired": ("TIME'S UP", ACCENT_RED),
    "status": ("STATUS", ACCENT_PURPLE),
    "leaderboard": ("LEADERBOARD", ACCENT_PURPLE),
}

_FONT_FILES = {
    False: [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:/Windows/Fonts/segoeui.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "DejaVuSans.ttf",
        "arial.ttf",
    ],
    True: [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "C:/Windows/Fonts/segoeuib.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "DejaVuSans-Bold.ttf",
        "arialbd.ttf",
    ],
}


def _font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    for path in _FONT_FILES[bold]:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _oneline(text: str) -> str:
    return " ".join((text or "").split())


def _truncate(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_w: float) -> str:
    text = _oneline(text)
    if draw.textlength(text, font=font) <= max_w:
        return text
    ell = "…"
    while text and draw.textlength(text + ell, font=font) > max_w:
        text = text[:-1]
    return text + ell


def _circular_avatar(avatar_bytes: Optional[bytes], size: int) -> Image.Image:
    over = size * 4
    mask = Image.new("L", (over, over), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, over - 1, over - 1), fill=255)
    mask = mask.resize((size, size), Image.Resampling.LANCZOS)

    source: Optional[Image.Image] = None
    if avatar_bytes:
        try:
            source = Image.open(io.BytesIO(avatar_bytes)).convert("RGB")
        except Exception:  # noqa: BLE001
            source = None
    if source is None:
        source = Image.new("RGB", (size, size), PLACEHOLDER_BG)
    else:
        w, h = source.size
        side = min(w, h)
        source = source.crop(
            ((w - side) // 2, (h - side) // 2, (w - side) // 2 + side, (h - side) // 2 + side)
        ).resize((size, size), Image.Resampling.LANCZOS)

    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(source, (0, 0), mask)
    return out


def _clip_round(img: Image.Image, box, radius: int, outline, width: int) -> None:
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(box, radius=radius, fill=255)
    img.putalpha(ImageChops.multiply(img.getchannel("A"), mask))
    ImageDraw.Draw(img).rounded_rectangle(box, radius=radius, outline=outline, width=width)


def _draw_stat(draw: ImageDraw.ImageDraw, box, label: str, value: str) -> None:
    x0, y0, x1, y1 = box
    draw.rounded_rectangle(box, radius=12 * SCALE, fill=PANEL_BG, outline=HAIRLINE, width=SCALE)
    draw.text(
        (x0 + 14 * SCALE, y0 + 14 * SCALE),
        label.upper(),
        font=_font(12 * SCALE, bold=True),
        fill=INK_SOFT,
    )
    draw.text(
        (x0 + 14 * SCALE, y0 + 40 * SCALE),
        value,
        font=_font(22 * SCALE, bold=True),
        fill=INK,
    )


def _render_challenge(
    *,
    kind: str,
    display_name: str,
    challenge_title: str,
    tier: str,
    number: int,
    avatar_bytes: Optional[bytes],
    focus: str,
    line_primary: str,
    line_secondary: str,
    footer: str,
) -> bytes:
    S = SCALE
    badge, accent = KIND_STYLE.get(kind, ("CHALLENGE", ACCENT_PURPLE))
    display_name = _oneline(display_name)
    challenge_title = _oneline(challenge_title)
    focus = _oneline(focus)
    line_primary = _oneline(line_primary)
    line_secondary = _oneline(line_secondary)
    footer = _oneline(footer)

    H = 360 if (line_primary or line_secondary or focus) else 300
    img = Image.new("RGBA", (WIDTH * S, H * S), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    outer = (0, 0, WIDTH * S - 1, H * S - 1)
    radius = 26 * S
    draw.rounded_rectangle(outer, radius=radius, fill=BG)
    draw.rounded_rectangle((0, 0, 10 * S, H * S - 1), radius=10 * S, fill=accent)
    draw.rectangle((6 * S, 0, 12 * S, H * S - 1), fill=accent)

    av = 168 * S
    avatar = _circular_avatar(avatar_bytes, av)
    ax, ay = MARGIN * S, (H * S - av) // 2
    img.paste(avatar, (ax, ay), avatar)
    draw.ellipse((ax, ay, ax + av, ay + av), outline=HAIRLINE, width=S)

    cx = ax + av + MARGIN * S
    right = (WIDTH - MARGIN) * S

    draw.text(
        (cx, 28 * S),
        badge,
        font=_font(13 * S, bold=True),
        fill=accent,
    )
    draw.text(
        (cx, 52 * S),
        _truncate(draw, display_name, _font(34 * S, bold=True), right - cx),
        font=_font(34 * S, bold=True),
        fill=INK,
    )
    sub = f"{tier} #{number} · {challenge_title}"
    draw.text(
        (cx, 98 * S),
        _truncate(draw, sub, _font(18 * S), right - cx),
        font=_font(18 * S),
        fill=INK_SOFT,
    )

    # Stat chips
    chips = [
        ("Tier", tier.replace("_", " ")),
        ("#", str(number)),
        ("Status", badge.title() if len(badge) < 18 else kind.title()),
    ]
    if focus:
        chips.append(("Focus", focus[:28] + ("…" if len(focus) > 28 else "")))
    row_y = 140 * S
    panel_h = 78 * S
    gap = 12 * S
    n = min(len(chips), 4)
    panel_w = (right - cx - (n - 1) * gap) // n
    for i, (lab, val) in enumerate(chips[:n]):
        x0 = cx + i * (panel_w + gap)
        _draw_stat(draw, (x0, row_y, x0 + panel_w, row_y + panel_h), lab, val)

    if line_primary or line_secondary:
        call_y = row_y + panel_h + 16 * S
        call_h = 88 * S
        call_w = right - cx + 1
        layer = Image.new("RGBA", (call_w, call_h + 1), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        local = (0, 0, call_w - 1, call_h)
        ld.rounded_rectangle(local, radius=12 * S, fill=CALLOUT_BG)
        ld.rounded_rectangle((0, 0, 6 * S, call_h), radius=3 * S, fill=accent)
        _clip_round(layer, local, 12 * S, HAIRLINE, S)
        img.alpha_composite(layer, (cx, call_y))
        tx = cx + 20 * S
        tw = right - tx - 14 * S
        if line_primary:
            draw.text(
                (tx, call_y + 18 * S),
                _truncate(draw, line_primary, _font(22 * S, bold=True), tw),
                font=_font(22 * S, bold=True),
                fill=INK,
            )
        if line_secondary:
            draw.text(
                (tx, call_y + 50 * S),
                _truncate(draw, line_secondary, _font(18 * S), tw),
                font=_font(18 * S),
                fill=INK_SOFT,
            )

    if footer:
        draw.text(
            (cx, (H - 28) * S),
            _truncate(draw, footer, _font(14 * S), right - cx),
            font=_font(14 * S),
            fill=INK_SOFT,
        )

    _clip_round(img, outer, radius, HAIRLINE, S)
    img = img.resize((WIDTH, H), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _render_leaderboard(
    *,
    title: str,
    entries: list[dict],
    footer: str = "",
) -> bytes:
    """entries: {rank, name, score, avatar?}"""
    S = SCALE
    accent = ACCENT_PURPLE
    W = 760
    row_h = 44
    top_h = 56
    margin = 36
    header = 90
    n = max(len(entries), 1)
    # first 3 taller
    body = 0
    for e in entries:
        body += top_h if int(e.get("rank") or 99) <= 3 else row_h
        body += 4
    H = header + body + 40 + (24 if footer else 0)

    img = Image.new("RGBA", (W * S, H * S), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    outer = (0, 0, W * S - 1, H * S - 1)
    radius = 26 * S
    draw.rounded_rectangle(outer, radius=radius, fill=BG)
    draw.rounded_rectangle((0, 0, 10 * S, H * S - 1), radius=10 * S, fill=accent)
    draw.rectangle((6 * S, 0, 12 * S, H * S - 1), fill=accent)

    draw.text(
        (margin * S, margin * S),
        _truncate(draw, _oneline(title), _font(28 * S, bold=True), (W - 2 * margin) * S),
        font=_font(28 * S, bold=True),
        fill=INK,
    )
    draw.line(
        (margin * S, (header - 12) * S, (W - margin) * S, (header - 12) * S),
        fill=HAIRLINE,
        width=S,
    )

    medals = {1: (214, 175, 84), 2: (183, 189, 201), 3: (200, 143, 92)}
    y = header
    for e in entries:
        rank = int(e.get("rank") or 0)
        is_top = rank <= 3
        rh = top_h if is_top else row_h
        mid = (y + rh / 2) * S
        name = _oneline(str(e.get("name") or "?"))
        score = str(e.get("score") or "0")

        if is_top:
            r = 18 * S
            cx = (margin + 22) * S
            draw.ellipse((cx - r, mid - r, cx + r, mid + r), fill=medals.get(rank, accent))
            draw.text((cx, mid), str(rank), font=_font(18 * S, bold=True), fill=BG, anchor="mm")
        else:
            draw.text(
                ((margin + 36) * S, mid),
                f"#{rank}",
                font=_font(16 * S, bold=True),
                fill=INK_SOFT,
                anchor="rm",
            )

        d = (40 if is_top else 30) * S
        av = _circular_avatar(e.get("avatar"), d)
        ax = (margin + 52) * S
        ay = int(mid - d / 2)
        img.paste(av, (int(ax), ay), av)
        draw.ellipse((ax, ay, ax + d, ay + d), outline=HAIRLINE, width=S)

        name_x = margin + 52 + 40 + 12
        draw.text(
            ((W - margin) * S, mid),
            score,
            font=_font(20 * S if is_top else 18 * S, bold=True),
            fill=INK,
            anchor="rm",
        )
        sw = draw.textlength(score, font=_font(20 * S if is_top else 18 * S, bold=True))
        draw.text(
            (name_x * S, mid),
            _truncate(
                draw,
                name,
                _font(20 * S if is_top else 17 * S, bold=is_top),
                (W - margin) * S - name_x * S - sw - 16 * S,
            ),
            font=_font(20 * S if is_top else 17 * S, bold=is_top),
            fill=INK,
            anchor="lm",
        )
        y += rh + 4

    if footer:
        draw.text(
            (margin * S, (H - 28) * S),
            _truncate(draw, _oneline(footer), _font(14 * S), (W - 2 * margin) * S),
            font=_font(14 * S),
            fill=INK_SOFT,
        )

    _clip_round(img, outer, radius, HAIRLINE, S)
    img = img.resize((W, H), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def render_challenge_card(**kwargs) -> bytes:
    return await asyncio.to_thread(_render_challenge, **kwargs)


async def render_leaderboard_card(**kwargs) -> bytes:
    return await asyncio.to_thread(_render_leaderboard, **kwargs)


async def avatar_bytes(user) -> Optional[bytes]:
    if user is None:
        return None
    try:
        return await user.display_avatar.read()
    except Exception:  # noqa: BLE001
        return None


def as_discord_file(png: bytes, filename: str = "challenge-card.png"):
    import discord

    return discord.File(io.BytesIO(png), filename=filename)
