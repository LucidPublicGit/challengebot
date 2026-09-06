"""Dark challenge cards styled after TadokuBot (Pillow PNG)."""

from __future__ import annotations

import asyncio
import io
import re
from typing import Optional

from PIL import Image, ImageChops, ImageDraw, ImageFont

SCALE = 2
WIDTH = 720
MARGIN = 28
POSTER_GAP = 20
POSTER_MARGIN = 20

BG = (30, 31, 38)
INK = (236, 237, 242)
INK_SOFT = (150, 152, 166)
HAIRLINE = (56, 58, 68)
PANEL_BG = (40, 42, 51)
PLACEHOLDER_BG = (58, 60, 72)
ACCENT_PURPLE = (150, 128, 226)
ACCENT_GREEN = (61, 214, 140)
ACCENT_RED = (240, 113, 120)
ACCENT_AMBER = (230, 192, 123)
ACCENT_BLUE = (91, 156, 250)

KIND_STYLE = {
    "start": ("STARTED", ACCENT_PURPLE),
    "scheduled": ("SCHEDULED", ACCENT_BLUE),
    "active": ("ACTIVE", ACCENT_PURPLE),
    "complete": ("DONE", ACCENT_GREEN),
    "fail": ("FAILED", ACCENT_RED),
    "abandon": ("QUIT", ACCENT_AMBER),
    "expired": ("EXPIRED", ACCENT_RED),
    "status": ("STATUS", ACCENT_PURPLE),
    "leaderboard": ("BOARD", ACCENT_PURPLE),
}

TIER_LABEL = {
    "easy": "Easy",
    "medium": "Medium",
    "hard": "Hard",
    "ota_master": "Ota-Master",
}

TIER_COLOR = {
    "easy": ACCENT_GREEN,
    "medium": ACCENT_BLUE,
    "hard": ACCENT_RED,
    "ota_master": ACCENT_PURPLE,
}

# Outcome is the hero on these kinds
TERMINAL_KINDS = frozenset({"complete", "fail", "abandon", "expired"})

# CJK-capable first (immersion titles often Japanese). TTC = (path, index).
_FONT_FILES: dict[bool, list[str | tuple[str, int]]] = {
    False: [
        ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 0),
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "C:/Windows/Fonts/msgothic.ttc",
        "C:/Windows/Fonts/meiryo.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:/Windows/Fonts/segoeui.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "DejaVuSans.ttf",
        "arial.ttf",
    ],
    True: [
        ("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", 0),
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
        "C:/Windows/Fonts/msgothic.ttc",
        "C:/Windows/Fonts/meiryob.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "C:/Windows/Fonts/segoeuib.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "DejaVuSans-Bold.ttf",
        "arialbd.ttf",
    ],
}

_PROGRESS_RE = re.compile(
    r"(?P<a>[\d.]+)\s*(?P<ak>k)?\s*/\s*(?P<b>[\d.]+)\s*(?P<bk>k)?",
    re.IGNORECASE,
)


def _font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    for entry in _FONT_FILES[bold]:
        try:
            if isinstance(entry, tuple):
                path, idx = entry
                return ImageFont.truetype(path, size, index=idx)
            return ImageFont.truetype(entry, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _oneline(text: str) -> str:
    return " ".join((text or "").split())


def _truncate(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_w: float) -> str:
    text = _oneline(text)
    max_w = max(8.0, float(max_w))
    if draw.textlength(text, font=font) <= max_w:
        return text
    ell = "..."
    lo, hi = 0, len(text)
    best = ell
    while lo <= hi:
        mid = (lo + hi) // 2
        cand = text[:mid] + ell
        if draw.textlength(cand, font=font) <= max_w:
            best = cand
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def _mix(c: tuple[int, int, int], bg: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(int(a + (b - a) * t) for a, b in zip(bg, c))  # type: ignore[return-value]


def _parse_progress(text: str) -> tuple[Optional[float], str]:
    """If secondary has 'X / Y', return (ratio 0..1+, 'frac[ unit] · meta')."""
    text = _oneline(text)
    m = _PROGRESS_RE.search(text)
    if not m:
        return None, text
    a = float(m.group("a")) * (1000 if m.group("ak") else 1)
    b = float(m.group("b")) * (1000 if m.group("bk") else 1)
    if b <= 0:
        return None, text
    ratio = a / b
    frac = m.group(0).strip()
    before = text[: m.start()].strip(" ·•|-")
    after = text[m.end() :].strip(" ·•|-")
    unit = ""
    meta_bits: list[str] = []
    if before:
        meta_bits.append(before)
    if after:
        # First short token after fraction is usually the unit (volumes, characters)
        tok, _, rest = after.partition(" · ")
        if tok and " " not in tok and len(tok) <= 16:
            unit = tok
            if rest:
                meta_bits.append(rest)
        else:
            meta_bits.append(after)
    label = f"{frac} {unit}".strip() if unit else frac
    if meta_bits:
        label = f"{label} · {' · '.join(meta_bits)}"
    return ratio, label


def _poster_image(poster_bytes: bytes, w: int, h: int, radius: int) -> Optional[Image.Image]:
    try:
        source = Image.open(io.BytesIO(poster_bytes)).convert("RGB")
    except Exception:  # noqa: BLE001
        return None
    sw, sh = source.size
    scale = max(w / max(sw, 1), h / max(sh, 1))
    nw, nh = max(1, round(sw * scale)), max(1, round(sh * scale))
    source = source.resize((nw, nh), Image.Resampling.LANCZOS)
    left, top = (nw - w) // 2, (nh - h) // 2
    source = source.crop((left, top, left + w, top + h))
    over = 4
    mask = Image.new("L", (w * over, h * over), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, w * over - 1, h * over - 1), radius=radius * over, fill=255
    )
    mask = mask.resize((w, h), Image.Resampling.LANCZOS)
    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    out.paste(source, (0, 0), mask)
    return out


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


def _wrap_two_lines(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_w: float
) -> list[str]:
    """Up to 2 lines; second line truncated if needed."""
    text = _oneline(text)
    if draw.textlength(text, font=font) <= max_w:
        return [text]
    words = text.split(" ")
    line1: list[str] = []
    for w in words:
        trial = " ".join(line1 + [w])
        if line1 and draw.textlength(trial, font=font) > max_w:
            break
        line1.append(w)
    if not line1:
        return [_truncate(draw, text, font, max_w)]
    rest = " ".join(words[len(line1) :]).strip()
    if not rest:
        return [" ".join(line1)]
    return [" ".join(line1), _truncate(draw, rest, font, max_w)]


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
    poster_bytes: Optional[bytes] = None,
) -> bytes:
    """
    Layout:
      [av]  NAME                 [STATUS PILL]
            [TIER PILL] #N
            title · focus (if no poster)
      [ outcome block — primary + optional progress + secondary ]
      optional cover on the right
    """
    S = SCALE
    badge, accent = KIND_STYLE.get(kind, ("EVENT", ACCENT_PURPLE))
    display_name = _oneline(display_name)
    challenge_title = _oneline(challenge_title)
    focus = _oneline(focus)
    line_primary = _oneline(line_primary)
    line_secondary = _oneline(line_secondary)
    footer = _oneline(footer)
    tier_key = (tier or "").lower()
    tier_pretty = TIER_LABEL.get(tier_key, (tier or "?").replace("_", " ").title())
    tier_color = TIER_COLOR.get(tier_key, INK_SOFT)
    terminal = kind in TERMINAL_KINDS

    # Decode poster early (affects focus row + width)
    poster_probe_h = 260
    poster_w = round(poster_probe_h * 2 / 3)
    poster = (
        _poster_image(poster_bytes, poster_w * S, poster_probe_h * S, 12 * S)
        if poster_bytes
        else None
    )
    show_focus = bool(focus) and poster is None
    progress_ratio, progress_label = _parse_progress(line_secondary)
    if progress_ratio is not None:
        if " · " in progress_label:
            progress_disp, outcome_meta = progress_label.split(" · ", 1)
        else:
            progress_disp, outcome_meta = progress_label, ""
    else:
        progress_disp = ""
        outcome_meta = line_secondary

    has_outcome = bool(line_primary or line_secondary)
    has_progress = progress_ratio is not None

    # Measure title lines with a scratch draw (for height)
    scratch = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    title_font_m = _font(20 * S, bold=True)
    # content width estimate (avatar 72 + gaps)
    content_w = (WIDTH - MARGIN - MARGIN - 72 - 16) * S
    title_lines = _wrap_two_lines(scratch, challenge_title, title_font_m, content_w)

    av_size = 72
    H = 22 + 30  # pad + name row
    H += 26  # tier badge row
    H += 24 * len(title_lines) + 6
    if show_focus:
        H += 22
    if has_outcome:
        H += 10
        if has_progress:
            H += 82
        elif outcome_meta or line_secondary:
            H += 70 if terminal else 64
        else:
            H += 54
    H += 28 if footer else 18
    H = max(H, 200)

    if poster is not None:
        poster_h = H - 2 * POSTER_MARGIN
        poster_w = max(100, round(poster_h * 2 / 3))
        poster = _poster_image(poster_bytes, poster_w * S, poster_h * S, 12 * S)
        poster_panel = (POSTER_GAP + poster_w + POSTER_MARGIN - MARGIN) if poster else 0
        if not poster:
            poster_h = 0
    else:
        poster_panel = 0
        poster_h = 0

    card_w = WIDTH + poster_panel
    img = Image.new("RGBA", (card_w * S, H * S), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    outer = (0, 0, card_w * S - 1, H * S - 1)
    radius = 22 * S
    draw.rounded_rectangle(outer, radius=radius, fill=BG)
    draw.rounded_rectangle((0, 0, 8 * S, H * S - 1), radius=8 * S, fill=accent)
    draw.rectangle((4 * S, 0, 9 * S, H * S - 1), fill=accent)

    av = av_size * S
    avatar = _circular_avatar(avatar_bytes, av)
    ax = MARGIN * S
    ay = 22 * S
    img.paste(avatar, (ax, ay), avatar)
    draw.ellipse((ax, ay, ax + av, ay + av), outline=accent, width=2 * S)

    cx = ax + av + 16 * S
    right = (WIDTH - MARGIN) * S
    y = 20 * S

    # Status pill (right) + name
    pill_font = _font(11 * S, bold=True)
    pill_pad_x, pill_pad_y = 12 * S, 5 * S
    pill_h = pill_font.size + pill_pad_y * 2
    pill_w = draw.textlength(badge, font=pill_font) + pill_pad_x * 2
    pill_x = right - pill_w
    pill_y = y + 4 * S
    draw.rounded_rectangle(
        (pill_x, pill_y, pill_x + pill_w, pill_y + pill_h),
        radius=pill_h / 2,
        fill=_mix(accent, BG, 0.22),
    )
    draw.text(
        (pill_x + pill_w / 2, pill_y + pill_h / 2),
        badge,
        font=pill_font,
        fill=accent,
        anchor="mm",
    )

    name_font = _font(24 * S, bold=True)
    name_max = max(40.0, pill_x - cx - 12 * S)
    draw.text(
        (cx, y),
        _truncate(draw, display_name, name_font, name_max),
        font=name_font,
        fill=INK,
    )
    y += 34 * S

    # Tier badge + #N, then title (1–2 lines)
    title_font = _font(20 * S, bold=True)
    tlabel = tier_pretty.upper()
    tpad_x, tpad_y = 10 * S, 4 * S
    th = pill_font.size + tpad_y * 2
    tw = draw.textlength(tlabel, font=pill_font) + tpad_x * 2
    draw.rounded_rectangle(
        (cx, y, cx + tw, y + th),
        radius=th / 2,
        fill=_mix(tier_color, BG, 0.22),
        outline=_mix(tier_color, BG, 0.55),
        width=max(1, S // 2),
    )
    draw.text(
        (cx + tw / 2, y + th / 2),
        tlabel,
        font=pill_font,
        fill=tier_color,
        anchor="mm",
    )
    if number:
        num_font = _font(13 * S, bold=True)
        draw.text(
            (cx + tw + 10 * S, y + th / 2),
            f"#{int(number)}",
            font=num_font,
            fill=INK_SOFT,
            anchor="lm",
        )
    y += th + 10 * S
    for i, ln in enumerate(title_lines):
        draw.text((cx, y + i * 24 * S), ln, font=title_font, fill=INK)
    y += 24 * S * len(title_lines) + 4 * S

    # Focus only when no poster (cover art is the focus)
    if show_focus:
        ff = _font(14 * S)
        flab = _font(11 * S, bold=True)
        lab = "FOCUS  "
        draw.text((cx, y), lab, font=flab, fill=INK_SOFT)
        lx = cx + draw.textlength(lab, font=flab)
        draw.text(
            (lx, y),
            _truncate(draw, focus, ff, right - lx),
            font=ff,
            fill=INK,
        )
        y += 22 * S

    # Outcome block — single accented panel
    if has_outcome:
        y += 8 * S
        if has_progress:
            oh_s = 82 * S
        elif outcome_meta or (line_secondary and progress_ratio is None):
            oh_s = (70 if terminal else 64) * S
        else:
            oh_s = 54 * S

        box = (cx, y, right, y + oh_s)
        draw.rounded_rectangle(box, radius=12 * S, fill=PANEL_BG, outline=HAIRLINE, width=S)
        # Single accent bar on outcome only
        draw.rounded_rectangle(
            (cx, y, cx + 5 * S, y + oh_s), radius=3 * S, fill=accent
        )
        pad = 16 * S
        tx = cx + pad
        tw = right - tx - 12 * S

        primary_size = 22 if terminal else 17
        pf = _font(primary_size * S, bold=True)
        primary_y = y + (14 * S if has_progress or outcome_meta else (oh_s - primary_size * S) / 2)
        if has_progress:
            # Primary left, fraction right
            draw.text((tx, y + 12 * S), _truncate(draw, line_primary or badge, pf, tw * 0.55), font=pf, fill=INK)
            sf = _font(15 * S, bold=True)
            draw.text(
                (right - 14 * S, y + 12 * S + primary_size * S / 2),
                _truncate(draw, progress_disp, sf, tw * 0.4),
                font=sf,
                fill=INK,
                anchor="rm",
            )
            # Progress bar
            bar_x0 = tx
            bar_x1 = right - 14 * S
            bar_y0 = y + 44 * S
            bar_y1 = bar_y0 + 8 * S
            draw.rounded_rectangle(
                (bar_x0, bar_y0, bar_x1, bar_y1), radius=4 * S, fill=(48, 50, 62)
            )
            fill_r = max(0.0, min(1.0, float(progress_ratio)))
            if fill_r > 0.02:
                fx1 = bar_x0 + (bar_x1 - bar_x0) * fill_r
                bar_color = accent if fill_r >= 1.0 or kind == "complete" else (
                    ACCENT_AMBER if kind == "fail" else accent
                )
                draw.rounded_rectangle(
                    (bar_x0, bar_y0, fx1, bar_y1), radius=4 * S, fill=bar_color
                )
            if outcome_meta:
                draw.text(
                    (tx, bar_y1 + 8 * S),
                    _truncate(draw, outcome_meta, _font(13 * S), tw),
                    font=_font(13 * S),
                    fill=INK_SOFT,
                )
        else:
            if line_primary:
                if terminal and not outcome_meta:
                    draw.text(
                        (tx, y + oh_s / 2),
                        _truncate(draw, line_primary, pf, tw),
                        font=pf,
                        fill=INK,
                        anchor="lm",
                    )
                else:
                    draw.text(
                        (tx, y + 12 * S),
                        _truncate(draw, line_primary, pf, tw),
                        font=pf,
                        fill=INK,
                    )
            meta = outcome_meta or (line_secondary if progress_ratio is None else "")
            if meta:
                draw.text(
                    (tx, y + 12 * S + primary_size * S + 8 * S),
                    _truncate(draw, meta, _font(14 * S), tw),
                    font=_font(14 * S),
                    fill=INK_SOFT,
                )
        y += oh_s + 6 * S

    if footer:
        draw.text(
            (cx, (H - 22) * S),
            _truncate(draw, footer, _font(11 * S), right - cx),
            font=_font(11 * S),
            fill=INK_SOFT,
        )

    if poster is not None:
        px0 = (WIDTH - MARGIN + POSTER_GAP) * S
        py0 = POSTER_MARGIN * S
        img.paste(poster, (int(px0), int(py0)), poster)
        draw.rounded_rectangle(
            (px0, py0, px0 + poster_w * S - 1, py0 + poster_h * S - 1),
            radius=12 * S,
            outline=HAIRLINE,
            width=S,
        )

    _clip_round(img, outer, radius, HAIRLINE, S)
    img = img.resize((card_w, H), Image.Resampling.LANCZOS)
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
    W = 560
    row_h = 40
    top_h = 48
    margin = 28
    header = 72
    body = 0
    for e in entries:
        body += top_h if int(e.get("rank") or 99) <= 3 else row_h
        body += 2
    foot = 28 if footer else 16
    H = header + body + foot

    img = Image.new("RGBA", (W * S, H * S), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    outer = (0, 0, W * S - 1, H * S - 1)
    radius = 22 * S
    draw.rounded_rectangle(outer, radius=radius, fill=BG)
    draw.rounded_rectangle((0, 0, 8 * S, H * S - 1), radius=8 * S, fill=accent)
    draw.rectangle((4 * S, 0, 9 * S, H * S - 1), fill=accent)

    draw.text(
        (margin * S, 22 * S),
        _truncate(draw, _oneline(title), _font(22 * S, bold=True), (W - 2 * margin) * S),
        font=_font(22 * S, bold=True),
        fill=INK,
    )
    # unit hint
    unit = "clears"
    uf = _font(12 * S, bold=True)
    draw.text(
        ((W - margin) * S, 28 * S),
        unit.upper(),
        font=uf,
        fill=INK_SOFT,
        anchor="rt",
    )
    draw.line(
        (margin * S, (header - 10) * S, (W - margin) * S, (header - 10) * S),
        fill=HAIRLINE,
        width=S,
    )

    medals = {1: (214, 175, 84), 2: (183, 189, 201), 3: (200, 143, 92)}
    y = header
    score_font_top = _font(18 * S, bold=True)
    score_font = _font(16 * S, bold=True)
    for e in entries:
        rank = int(e.get("rank") or 0)
        is_top = 1 <= rank <= 3
        rh = top_h if is_top else row_h
        mid = (y + rh / 2) * S
        name = _oneline(str(e.get("name") or "?"))
        score = str(e.get("score") or "0")

        # Rank column
        if is_top:
            r = 14 * S
            rcx = (margin + 16) * S
            draw.ellipse((rcx - r, mid - r, rcx + r, mid + r), fill=medals.get(rank, accent))
            draw.text((rcx, mid), str(rank), font=_font(14 * S, bold=True), fill=BG, anchor="mm")
        else:
            draw.text(
                ((margin + 16) * S, mid),
                str(rank),
                font=_font(14 * S, bold=True),
                fill=INK_SOFT,
                anchor="mm",
            )

        d = (32 if is_top else 26) * S
        av = _circular_avatar(e.get("avatar"), d)
        ax = (margin + 36) * S
        ay = int(mid - d / 2)
        img.paste(av, (int(ax), ay), av)
        draw.ellipse((ax, ay, ax + d, ay + d), outline=HAIRLINE, width=S)

        sf = score_font_top if is_top else score_font
        draw.text(((W - margin) * S, mid), score, font=sf, fill=INK, anchor="rm")
        sw = draw.textlength(score, font=sf)

        name_x = margin + 36 + 32 + 10
        nf = _font(17 * S if is_top else 15 * S, bold=is_top)
        name_max = (W - margin) * S - name_x * S - sw - 20 * S
        draw.text(
            (name_x * S, mid),
            _truncate(draw, name, nf, name_max),
            font=nf,
            fill=INK,
            anchor="lm",
        )

        # subtle leader dots between name and score
        dots_x0 = name_x * S + draw.textlength(_truncate(draw, name, nf, name_max), font=nf) + 10 * S
        dots_x1 = (W - margin) * S - sw - 12 * S
        if dots_x1 - dots_x0 > 24 * S:
            dy = mid
            x = dots_x0
            while x < dots_x1:
                draw.ellipse((x, dy - S, x + 2 * S, dy + S), fill=HAIRLINE)
                x += 6 * S

        y += rh + 2

    if footer:
        draw.text(
            (margin * S, (H - 20) * S),
            _truncate(draw, _oneline(footer), _font(12 * S), (W - 2 * margin) * S),
            font=_font(12 * S),
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


# Embed chrome so attached images render inline (not as click-to-open files)
_KIND_EMBED_COLOR = {
    "start": 0x9680E2,
    "scheduled": 0x5B9CFA,
    "active": 0x9680E2,
    "complete": 0x3DD68C,
    "fail": 0xF07178,
    "abandon": 0xE6C07B,
    "expired": 0xF07178,
    "status": 0x9680E2,
    "leaderboard": 0x9680E2,
}


def files_as_image_embeds(
    files,
    *,
    kind: str | None = None,
    color: int | None = None,
):
    """One embed per file with set_image(attachment://…) so Discord shows them full-size."""
    import discord

    c = color if color is not None else _KIND_EMBED_COLOR.get(kind or "", 0x2F3136)
    embeds: list = []
    for f in files or []:
        name = getattr(f, "filename", None) or "image.png"
        emb = discord.Embed(color=c)
        emb.set_image(url=f"attachment://{name}")
        embeds.append(emb)
    return embeds


# Demo payloads for tests + /challenge testcards
DEMO_CHALLENGE_SAMPLES: dict[str, dict] = {
    "start": {
        "display_name": "Monty",
        "challenge_title": "Manga Mini-Marathon",
        "tier": "easy",
        "number": 1,
        "focus": "One Piece",
        "line_primary": "Good luck",
        "line_secondary": "Ends Sat 28 Mar 18:00 UTC",
        "footer": "",
    },
    "scheduled": {
        "display_name": "Monty",
        "challenge_title": "Anime Sprint",
        "tier": "easy",
        "number": 2,
        "focus": "Frieren",
        "line_primary": "Queued to start",
        "line_secondary": "Sun 29 Mar 09:00 -> Mon 30 Mar 09:00 UTC",
        "footer": "",
    },
    "active": {
        "display_name": "Monty",
        "challenge_title": "VN 30k",
        "tier": "easy",
        "number": 4,
        "focus": "Steins;Gate",
        "line_primary": "In progress",
        "line_secondary": "Ends Sat 28 Mar 18:00 UTC",
        "footer": "",
    },
    "complete": {
        "display_name": "Monty",
        "challenge_title": "VN 30k",
        "tier": "easy",
        "number": 4,
        "focus": "Steins;Gate",
        "line_primary": "Cleared",
        "line_secondary": "32.1k / 30k characters · Tadoku",
        "footer": "",
    },
    "fail": {
        "display_name": "Monty",
        "challenge_title": "Manga 10",
        "tier": "medium",
        "number": 1,
        "focus": "Berserk",
        "line_primary": "Goal not met",
        "line_secondary": "6.2 / 10 volumes",
        "footer": "",
    },
    "abandon": {
        "display_name": "Monty",
        "challenge_title": "YouTube Deep Dive",
        "tier": "easy",
        "number": 3,
        "focus": "Comprehensible Japanese",
        "line_primary": "Abandoned",
        "line_secondary": "",
        "footer": "",
    },
    "expired": {
        "display_name": "Monty",
        "challenge_title": "Anime 15",
        "tier": "medium",
        "number": 2,
        "focus": "Dungeon Meshi",
        "line_primary": "Time's up",
        "line_secondary": "Window closed",
        "footer": "",
    },
    "status": {
        "display_name": "Monty",
        "challenge_title": "8-Hour Immersion Day",
        "tier": "hard",
        "number": 5,
        "focus": "",
        "line_primary": "Honor challenge",
        "line_secondary": "Confirm when finished",
        "footer": "",
    },
}


def demo_avatar_png(color: tuple[int, int, int] = (120, 100, 200)) -> bytes:
    """Simple placeholder avatar for demos/tests."""
    img = Image.new("RGB", (256, 256), (40, 42, 51))
    d = ImageDraw.Draw(img)
    d.ellipse((16, 16, 240, 240), fill=color)
    d.ellipse((90, 70, 166, 146), fill=(236, 237, 242))
    d.ellipse((70, 150, 186, 250), fill=(236, 237, 242))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def render_all_demo_cards(
    *,
    display_name: str | None = None,
    avatar: Optional[bytes] = None,
    fetch_covers: bool = True,
) -> list[tuple[str, bytes]]:
    """Every card format as (kind, png_bytes). Leaderboard last."""
    from app.poster import fetch_poster

    av = avatar if avatar is not None else demo_avatar_png()
    out: list[tuple[str, bytes]] = []
    media_by_kind = {
        "start": "manga",
        "scheduled": "anime",
        "active": "vn",
        "complete": "vn",
        "fail": "manga",
        "abandon": "youtube",
        "expired": "anime",
        "status": "any",
    }
    for kind, sample in DEMO_CHALLENGE_SAMPLES.items():
        payload = dict(sample)
        if display_name:
            payload["display_name"] = display_name
        poster = None
        if fetch_covers and payload.get("focus"):
            try:
                poster = await fetch_poster(
                    str(payload["focus"]), media_by_kind.get(kind, "any")
                )
            except Exception:  # noqa: BLE001
                poster = None
        png = await render_challenge_card(
            kind=kind, avatar_bytes=av, poster_bytes=poster, **payload
        )
        out.append((kind, png))
    lb = await render_leaderboard_card(
        title="Challenge clears",
        entries=[
            {"rank": 1, "name": "Alice", "score": 12, "avatar": demo_avatar_png((214, 175, 84))},
            {"rank": 2, "name": "Bob", "score": 7, "avatar": demo_avatar_png((183, 189, 201))},
            {"rank": 3, "name": "Cara", "score": 3, "avatar": demo_avatar_png((200, 143, 92))},
            {"rank": 4, "name": "Dan", "score": 1, "avatar": None},
            {"rank": 5, "name": "Eve", "score": 1, "avatar": None},
        ],
        footer="5 ranked · clears",
    )
    out.append(("leaderboard", lb))
    return out
