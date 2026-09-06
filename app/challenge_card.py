"""Challenge lifecycle cards (Pillow PNG) — distinct from Tadoku log cards."""

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

# Warmer ink base (Tadoku logs are cooler gray-purple)
BG = (26, 24, 30)
INK = (236, 237, 242)
INK_SOFT = (150, 152, 166)
HAIRLINE = (58, 54, 64)
PANEL_BG = (38, 36, 46)
PLACEHOLDER_BG = (58, 56, 68)
ACCENT_PURPLE = (150, 128, 226)
ACCENT_GREEN = (61, 214, 140)
ACCENT_RED = (240, 113, 120)
ACCENT_AMBER = (230, 192, 123)
ACCENT_BLUE = (91, 156, 250)
ACCENT_CYAN = (56, 189, 248)  # medium — distinct from kind purple/blue
ACCENT_GOLD = (234, 196, 96)  # ota prestige
ACCENT_VIOLET = (186, 140, 255)  # ota primary (brighter than log purple)

BRAND_FOOTER = "ChallengeTracker"

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

# In-card hero status (no top-right pill) — soft-glow like Cleared
HERO_STATUS = {
    "start": "Started",
    "scheduled": "Scheduled",
    "active": "Active",
    "complete": "Cleared",
    "fail": "Failed",
    "abandon": "Abandoned",
    "expired": "Expired",
    "status": "Status",
    "leaderboard": "Board",
}

TIER_LABEL = {
    "easy": "Easy",
    "medium": "Medium",
    "hard": "Hard",
    "ota_master": "Ota-Master",
}

TIER_COLOR = {
    "easy": ACCENT_GREEN,
    "medium": ACCENT_CYAN,
    "hard": ACCENT_RED,
    "ota_master": ACCENT_VIOLET,
}

# Escalating flair: higher tier = stronger wash, thicker rail, more ornaments
# wash: max tier mix at left edge; rail_tier/kind in CSS-ish px before SCALE
TIER_FLAIR: dict[str, dict] = {
    # Dialed down — one accent rail, softer wash (was dual-rail + loud mist)
    "easy": {
        "wash": 0.04,
        "wash_width": 0.28,
        "rail_tier": 0,
        "rail_kind": 4,
        "ring": 2,
        "ring_outer": 0,
        "ring_outer_color": None,
        "edge": 0.22,
        "top_rule": False,
        "corners": False,
        "tier_pill_pad": 0,
        "spark": False,
        "secondary": None,
    },
    "medium": {
        "wash": 0.06,
        "wash_width": 0.32,
        "rail_tier": 0,
        "rail_kind": 4,
        "ring": 2,
        "ring_outer": 0,
        "ring_outer_color": None,
        "edge": 0.28,
        "top_rule": False,
        "corners": False,
        "tier_pill_pad": 0,
        "spark": False,
        "secondary": None,
    },
    "hard": {
        "wash": 0.08,
        "wash_width": 0.36,
        "rail_tier": 3,
        "rail_kind": 4,
        "ring": 2,
        "ring_outer": 0,
        "ring_outer_color": None,
        "edge": 0.35,
        "top_rule": False,
        "corners": False,
        "tier_pill_pad": 1,
        "spark": False,
        "secondary": ACCENT_AMBER,
    },
    "ota_master": {
        "wash": 0.10,
        "wash_width": 0.40,
        "rail_tier": 4,
        "rail_kind": 4,
        "ring": 2,
        "ring_outer": 2,
        "ring_outer_color": "gold",
        "edge": 0.40,
        "top_rule": True,
        "corners": True,
        "tier_pill_pad": 2,
        "spark": True,
        "secondary": ACCENT_GOLD,
    },
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
    """Blend color c onto bg by t (0=bg, 1=c)."""
    return tuple(int(a + (b - a) * t) for a, b in zip(bg, c))  # type: ignore[return-value]


def _tier_flair(tier_key: str) -> dict:
    return TIER_FLAIR.get(tier_key) or TIER_FLAIR["easy"]


def _flair_secondary(flair: dict) -> tuple[int, int, int] | None:
    sec = flair.get("secondary")
    return sec if isinstance(sec, tuple) else None


def _ring_outer_color(
    flair: dict, tier_color: tuple[int, int, int]
) -> tuple[int, int, int] | None:
    key = flair.get("ring_outer_color")
    if key == "gold":
        return ACCENT_GOLD
    if key == "amber":
        return ACCENT_AMBER
    if key == "secondary":
        return _flair_secondary(flair) or tier_color
    if isinstance(key, tuple):
        return key
    return None


def _draw_tier_wash(
    draw: ImageDraw.ImageDraw,
    *,
    card_w_px: int,
    card_h_px: int,
    tier_color: tuple[int, int, int],
    strength: float = 0.10,
    width_frac: float = 0.45,
) -> None:
    """Soft left-weighted tier mist; strength/width escalate by difficulty."""
    wash_w = max(1, int(card_w_px * max(0.25, min(0.7, width_frac))))
    steps = 12
    step_w = max(1, wash_w // steps)
    peak = max(0.04, min(0.28, float(strength)))
    for i in range(steps):
        t = peak * (1.0 - i / max(steps - 1, 1))
        if t < 0.006:
            break
        col = _mix(tier_color, BG, t)
        x0 = i * step_w
        x1 = wash_w if i == steps - 1 else (i + 1) * step_w
        draw.rectangle((x0, 0, x1, card_h_px - 1), fill=col)


def _draw_corner_brackets(
    draw: ImageDraw.ImageDraw,
    outer: tuple[int, int, int, int],
    *,
    color: tuple[int, int, int],
    S: int,
    length: int | None = None,
    width: int | None = None,
) -> None:
    """Gold L-brackets on the four corners (Ota prestige)."""
    x0, y0, x1, y1 = outer
    L = length if length is not None else 18 * S
    W = width if width is not None else max(2, 2 * S)
    # inset so brackets sit inside the rounded edge
    pad = 10 * S
    x0, y0, x1, y1 = x0 + pad, y0 + pad, x1 - pad, y1 - pad
    # TL
    draw.rectangle((x0, y0, x0 + L, y0 + W), fill=color)
    draw.rectangle((x0, y0, x0 + W, y0 + L), fill=color)
    # TR
    draw.rectangle((x1 - L, y0, x1, y0 + W), fill=color)
    draw.rectangle((x1 - W, y0, x1, y0 + L), fill=color)
    # BL
    draw.rectangle((x0, y1 - W, x0 + L, y1), fill=color)
    draw.rectangle((x0, y1 - L, x0 + W, y1), fill=color)
    # BR
    draw.rectangle((x1 - L, y1 - W, x1, y1), fill=color)
    draw.rectangle((x1 - W, y1 - L, x1, y1), fill=color)


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


def parse_content_items(raw: str | None, *, limit: int = 12) -> list[str]:
    """Split stored content into one work per entry (newlines and commas)."""
    from app.services.verify import parse_content_items as _parse

    return _parse(raw, limit=limit)


def format_content_items(items: list[str] | None) -> str:
    """Store multi-work content as one title per line."""
    from app.services.verify import format_content_items as _fmt

    return _fmt("\n".join(items or [])) or ""


def _resolve_hero_style(
    kind: str,
    hero_style: str | None,
    cleared_style: str = "",
) -> str:
    """Stamp outline only for complete/abandon. Started/Scheduled/… stay soft-glow."""
    kind = (kind or "").lower().strip()
    # Normal statuses never get terminal flair
    if kind not in ("complete", "abandon"):
        return "glow"
    # Explicit override (protos / tests)
    if hero_style is not None and str(hero_style).strip() != "":
        return str(hero_style).lower().strip()
    if cleared_style and str(cleared_style).strip():
        cs = str(cleared_style).lower().strip()
        if cs not in ("d_soft_glow", "glow", ""):
            return cs
    return "stamp_outline"


def _draw_hero_status(
    draw: ImageDraw.ImageDraw,
    *,
    style: str,
    label: str,
    tier_color: tuple[int, int, int],
    tx: float,
    y: float,
    tw: float,
    S: int,
    kind: str,
    has_progress: bool,
) -> None:
    """In-card status hero. style=glow is default; extras for complete/abandon protos."""
    label = _oneline(label)
    style = (style or "glow").lower().strip()
    max_w = tw * (0.55 if has_progress else 1.0)

    def _glow_text(text: str, font, fill, x: float, yy: float) -> None:
        for dx, dy in ((-S, 0), (S, 0), (0, -S), (0, S)):
            draw.text((x + dx, yy + dy), text, font=font, fill=_mix(fill, BG, 0.45))
        draw.text((x, yy), text, font=font, fill=fill)

    if style in ("glow", "0_current_glow", "d_soft_glow", ""):
        pf = _font(26 * S, bold=True)
        lab = _truncate(draw, label, pf, max_w)
        _glow_text(lab, pf, tier_color, tx, y + 10 * S)
        return

    if style in ("1_tier_pill", "tier_pill", "pill"):
        pf = _font(22 * S, bold=True)
        lab = _truncate(draw, label.upper(), pf, max_w - 28 * S)
        pw = draw.textlength(lab, font=pf) + 28 * S
        ph = pf.size + 14 * S
        py = y + 10 * S
        draw.rounded_rectangle(
            (tx, py, tx + pw, py + ph),
            radius=ph / 2,
            fill=_mix(tier_color, BG, 0.22),
            outline=_mix(tier_color, BG, 0.55),
            width=max(1, S),
        )
        draw.text((tx + pw / 2, py + ph / 2), lab, font=pf, fill=tier_color, anchor="mm")
        return

    if style in ("2_double_rule", "double_rule"):
        pf = _font(26 * S, bold=True)
        lab = _truncate(draw, label, pf, max_w)
        _glow_text(lab, pf, tier_color, tx, y + 8 * S)
        lw = draw.textlength(lab, font=pf)
        uy = y + 8 * S + pf.size + 6 * S
        draw.line((tx, uy, tx + lw, uy), fill=tier_color, width=max(2, 2 * S))
        draw.line(
            (tx, uy + 4 * S, tx + lw * 0.55, uy + 4 * S),
            fill=_mix(tier_color, BG, 0.5),
            width=max(1, S),
        )
        return

    if style in ("3_symbol_lead", "symbol_lead", "symbol"):
        pf = _font(24 * S, bold=True)
        sym = "✓  " if kind == "complete" else ("✕  " if kind == "abandon" else "")
        lab = _truncate(draw, f"{sym}{label.upper()}", pf, max_w)
        _glow_text(lab, pf, tier_color, tx, y + 10 * S)
        return

    if style in ("4_tracked", "tracked"):
        pf = _font(20 * S, bold=True)
        spaced = " ".join(list(_oneline(label).upper()))
        lab = _truncate(draw, spaced, pf, max_w)
        draw.text((tx, y + 14 * S), lab, font=pf, fill=tier_color)
        lw = draw.textlength(lab, font=pf)
        draw.line(
            (tx, y + 14 * S + pf.size + 5 * S, tx + lw, y + 14 * S + pf.size + 5 * S),
            fill=tier_color,
            width=max(1, S),
        )
        return

    if style in ("5_stamp_outline", "stamp_outline", "stamp"):
        pf = _font(28 * S, bold=True)
        lab = _truncate(draw, label.upper(), pf, max_w)
        oc = _mix(tier_color, BG, 0.2)
        for dx in range(-2 * S, 2 * S + 1, max(1, S)):
            for dy in range(-2 * S, 2 * S + 1, max(1, S)):
                if dx == 0 and dy == 0:
                    continue
                draw.text((tx + dx, y + 8 * S + dy), lab, font=pf, fill=oc)
        draw.text((tx, y + 8 * S), lab, font=pf, fill=_mix(tier_color, INK, 0.4))
        return

    if style in ("6_spark_ends", "spark_ends", "spark"):
        pf = _font(26 * S, bold=True)
        lab = _truncate(draw, label, pf, max_w - 24 * S)
        x0 = tx + 12 * S
        _glow_text(lab, pf, tier_color, x0, y + 10 * S)
        lw = draw.textlength(lab, font=pf)
        spark = ACCENT_GOLD if kind == "complete" else tier_color
        cy = y + 10 * S + pf.size / 2
        for sx in (tx + 2 * S, x0 + lw + 8 * S):
            r = max(2, 2 * S)
            draw.ellipse((sx - r, cy - r, sx + r, cy + r), fill=spark)
            draw.line((sx - 4 * S, cy, sx + 4 * S, cy), fill=spark, width=max(1, S // 2))
            draw.line((sx, cy - 4 * S, sx, cy + 4 * S), fill=spark, width=max(1, S // 2))
        return

    if style in ("7_stack_kicker", "stack_kicker", "kicker"):
        kick = {
            "complete": "CLEAR",
            "abandon": "WALK AWAY",
            "fail": "FAILED RUN",
            "expired": "TIME UP",
        }.get(kind, "STATUS")
        draw.text(
            (tx, y + 4 * S),
            kick,
            font=_font(11 * S, bold=True),
            fill=_mix(tier_color, INK_SOFT, 0.45),
        )
        pf = _font(28 * S, bold=True)
        lab = _truncate(draw, label, pf, max_w)
        _glow_text(lab, pf, tier_color, tx, y + 22 * S)
        return

    # unknown → glow
    pf = _font(26 * S, bold=True)
    lab = _truncate(draw, label, pf, max_w)
    _glow_text(lab, pf, tier_color, tx, y + 10 * S)


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
    cleared_style: str = "",
    # None = auto: stamp_outline for complete/abandon only; glow for all other kinds
    hero_style: str | None = None,
) -> bytes:
    """
    Layout:
      [av]  NAME
            [TIER PILL] #N
            title · focus (if no poster)
      [ outcome: hero status + meta / progress ]
      optional cover on the right
    """
    S = SCALE
    badge, accent = KIND_STYLE.get(kind, ("EVENT", ACCENT_PURPLE))
    display_name = _oneline(display_name)
    challenge_title = _oneline(challenge_title)
    focus_items = parse_content_items(focus)
    # Single long string without list separators stays one item
    if not focus_items and (focus or "").strip():
        focus_items = [_oneline(focus)]
    line_primary = _oneline(line_primary)
    line_secondary = _oneline(line_secondary)
    footer = _oneline(footer)
    tier_key = (tier or "").lower()
    tier_pretty = TIER_LABEL.get(tier_key, (tier or "?").replace("_", " ").title())
    tier_color = TIER_COLOR.get(tier_key, INK_SOFT)
    flair = _tier_flair(tier_key)
    sec_color = _flair_secondary(flair)
    terminal = kind in TERMINAL_KINDS

    # Decode poster early (affects focus row + width)
    poster_probe_h = 260
    poster_w = round(poster_probe_h * 2 / 3)
    poster = (
        _poster_image(poster_bytes, poster_w * S, poster_probe_h * S, 12 * S)
        if poster_bytes
        else None
    )
    # Multi-work lists always show as lines; single focus hides when poster covers it
    show_focus = bool(focus_items) and (poster is None or len(focus_items) > 1)
    progress_ratio, progress_label = _parse_progress(line_secondary)
    if progress_ratio is not None:
        if " · " in progress_label:
            progress_disp, outcome_meta = progress_label.split(" · ", 1)
        else:
            progress_disp, outcome_meta = progress_label, ""
    else:
        progress_disp = ""
        outcome_meta = line_secondary

    # Always show in-card status hero (Started / Cleared / …) — no corner pill
    has_outcome = True
    has_progress = progress_ratio is not None
    hero_label = HERO_STATUS.get(kind, (badge or "Event").title())
    # complete: Cleared; status milestones: Halfway / Final hour / …
    if kind in ("complete", "status") and line_primary:
        hero_label = line_primary

    # Measure title + works with a scratch draw so H tracks real line counts
    scratch = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    title_font_m = _font(20 * S, bold=True)
    focus_font_m = _font(15 * S)
    # content width estimate (avatar 72 + gaps); works indent ~16px for bullet
    content_w = (WIDTH - MARGIN - MARGIN - 72 - 16) * S
    works_w = content_w - 16 * S
    title_lines = _wrap_two_lines(scratch, challenge_title, title_font_m, content_w)

    # Dynamic works block: each entry can wrap to 2 lines; height = sum of lines
    focus_header_h = 20  # unscaled px
    focus_line_h = 22 if len(focus_items) <= 8 else 20
    focus_pad_after = 10
    focus_wrapped: list[list[str]] = []
    focus_block_h = 0
    if show_focus:
        focus_block_h = focus_header_h
        for item in focus_items:
            lines = _wrap_two_lines(scratch, item, focus_font_m, works_w)
            focus_wrapped.append(lines)
            focus_block_h += focus_line_h * len(lines)
        focus_block_h += focus_pad_after

    _hs = _resolve_hero_style(kind, hero_style, cleared_style)
    _tall = _hs in (
        "1_tier_pill",
        "tier_pill",
        "pill",
        "7_stack_kicker",
        "stack_kicker",
        "kicker",
        "2_double_rule",
        "double_rule",
        "5_stamp_outline",
        "stamp_outline",
        "stamp",
    )
    if has_progress:
        outcome_h = 90 if _tall else 82
    elif outcome_meta or line_secondary or line_primary:
        outcome_h = (78 if _tall else 70) if terminal else (72 if _tall else 64)
    else:
        outcome_h = 62 if _tall else 54

    # Stack sections (unscaled). Keep draw path in sync with these constants.
    av_size = 72
    top_pad = 22
    name_row_h = 30
    tier_row_h = 26  # pill + gap into title
    title_line_h = 24
    title_after = 6
    outcome_gap = 10
    footer_h = 28

    H = top_pad + name_row_h + tier_row_h
    H += title_line_h * len(title_lines) + title_after
    H += focus_block_h
    if has_outcome:
        H += outcome_gap + outcome_h + 6
    # Always reserve brand footer
    if not footer:
        footer = BRAND_FOOTER
    elif BRAND_FOOTER.lower() not in footer.lower():
        footer = f"{footer}  ·  {BRAND_FOOTER}"
    H += footer_h
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
    _draw_tier_wash(
        draw,
        card_w_px=card_w * S,
        card_h_px=H * S,
        tier_color=tier_color,
        strength=float(flair.get("wash") or 0.1),
        width_frac=float(flair.get("wash_width") or 0.45),
    )
    # Left rail: difficulty theme only (same color language as outcome tick)
    rail_w = max(3, int(flair.get("rail_kind") or 4)) * S
    if int(flair.get("rail_tier") or 0) > 0:
        rail_w = max(rail_w, int(flair["rail_tier"]) * S + rail_w // 2)
    draw.rounded_rectangle(
        (0, 0, rail_w, H * S - 1), radius=8 * S, fill=tier_color
    )

    # Prestige / heat rule under top edge
    if flair.get("top_rule"):
        rule_c = sec_color or tier_color
        draw.rectangle(
            (rail_w, 0, card_w * S - 1, max(2, 2 * S)),
            fill=_mix(rule_c, BG, 0.65),
        )

    av = av_size * S
    avatar = _circular_avatar(avatar_bytes, av)
    ax = MARGIN * S
    ay = 22 * S
    img.paste(avatar, (ax, ay), avatar)
    # Avatar rings escalate: outer flair ring then tier ring
    outer_w = int(flair.get("ring_outer") or 0) * S
    ring_w = max(1, int(flair.get("ring") or 2) * S)
    oc = _ring_outer_color(flair, tier_color)
    if outer_w > 0 and oc is not None:
        pad = outer_w + S
        draw.ellipse(
            (ax - pad, ay - pad, ax + av + pad, ay + av + pad),
            outline=_mix(oc, BG, 0.75),
            width=outer_w,
        )
    draw.ellipse(
        (ax, ay, ax + av, ay + av), outline=tier_color, width=ring_w
    )

    cx = ax + av + 16 * S
    right = (WIDTH - MARGIN) * S
    y = 20 * S

    # Name uses full width — status lives in the outcome band, not a corner pill
    pill_font = _font(11 * S, bold=True)
    name_font = _font(24 * S, bold=True)
    name_max = max(40.0, right - cx)
    draw.text(
        (cx, y),
        _truncate(draw, display_name, name_font, name_max),
        font=name_font,
        fill=INK,
    )
    y += 34 * S

    # Tier badge + #N, then title (1–2 lines) — flair pads/outline escalate
    title_font = _font(20 * S, bold=True)
    tlabel = tier_pretty.upper()
    if tier_key == "ota_master":
        tlabel = "★  OTA-MASTER"
    extra_pad = int(flair.get("tier_pill_pad") or 0) * S
    tpad_x, tpad_y = 10 * S + extra_pad, 4 * S + max(0, extra_pad // 2)
    th = pill_font.size + tpad_y * 2
    tw = draw.textlength(tlabel, font=pill_font) + tpad_x * 2
    tier_outline = sec_color if tier_key in ("hard", "ota_master") and sec_color else _mix(
        tier_color, BG, 0.55
    )
    draw.rounded_rectangle(
        (cx, y, cx + tw, y + th),
        radius=th / 2,
        fill=_mix(tier_color, BG, 0.28 if tier_key != "easy" else 0.22),
        outline=tier_outline,
        width=max(1, (1 + (1 if tier_key in ("hard", "ota_master") else 0)) * max(1, S // 2)),
    )
    draw.text(
        (cx + tw / 2, y + th / 2),
        tlabel,
        font=pill_font,
        fill=ACCENT_GOLD if tier_key == "ota_master" else tier_color,
        anchor="mm",
    )
    # Tiny prestige sparks beside Ota pill
    if flair.get("spark"):
        spark = ACCENT_GOLD
        sy = int(y + th / 2)
        for dx, dy in ((-6 * S, 0), (tw + 6 * S, 0)):
            sx = int(cx + dx)
            draw.ellipse((sx - S, sy - S, sx + S, sy + S), fill=spark)
    if number:
        num_font = _font(13 * S, bold=True)
        num_x = cx + tw + 10 * S + (8 * S if flair.get("spark") else 0)
        draw.text(
            (num_x, y + th / 2),
            f"#{int(number)}",
            font=num_font,
            fill=ACCENT_GOLD if tier_key == "ota_master" else INK_SOFT,
            anchor="lm",
        )
    y += th + 10 * S
    for i, ln in enumerate(title_lines):
        draw.text((cx, y + i * 24 * S), ln, font=title_font, fill=INK)
    y += 24 * S * len(title_lines) + 4 * S

    # Works completed — height already reserved via focus_block_h / focus_wrapped
    if show_focus:
        ff = _font(15 * S)
        flab = _font(11 * S, bold=True)
        bullet_f = _font(13 * S, bold=True)
        header = "WORKS" if len(focus_items) > 1 else "FOCUS"
        draw.text((cx, y), header, font=flab, fill=INK_SOFT)
        y += focus_header_h * S
        for lines in focus_wrapped:
            draw.text((cx, y + 2 * S), "•", font=bullet_f, fill=accent)
            for j, ln in enumerate(lines):
                draw.text(
                    (cx + 16 * S, y + j * focus_line_h * S),
                    ln,
                    font=ff,
                    fill=INK,
                )
            y += focus_line_h * len(lines) * S
        y += focus_pad_after * S

    # Outcome: tier side-tick + hero status (soft-glow, same language as Cleared)
    if has_outcome:
        y += outcome_gap * S
        # Build soft meta under the hero (skip if it just repeats the status word)
        meta_bits: list[str] = []
        lp = (line_primary or "").strip()
        if lp and lp.lower() not in {
            hero_label.lower(),
            "cleared",
            "done",
            badge.lower(),
        }:
            meta_bits.append(lp)
        if has_progress and progress_disp:
            bit = progress_disp
            if outcome_meta:
                bit = f"{progress_disp} · {outcome_meta}"
            meta_bits.append(bit)
        elif outcome_meta:
            meta_bits.append(outcome_meta)
        elif line_secondary and not has_progress:
            meta_bits.append(line_secondary)
        meta_line = " · ".join(dict.fromkeys(meta_bits))  # stable unique

        oh_s = outcome_h * S

        tick_w = 3 * S
        draw.rectangle(
            (cx, y + 6 * S, cx + tick_w, y + oh_s - 6 * S), fill=tier_color
        )
        pad = 14 * S
        tx = cx + tick_w + pad
        tw = right - tx - 12 * S

        # complete/abandon → stamp outline; Started/Scheduled/… always plain glow
        hs = _resolve_hero_style(kind, hero_style, cleared_style)
        _draw_hero_status(
            draw,
            style=hs,
            label=hero_label,
            tier_color=tier_color,
            tx=tx,
            y=y,
            tw=tw,
            S=S,
            kind=kind,
            has_progress=has_progress,
        )

        if has_progress:
            sf = _font(15 * S, bold=True)
            draw.text(
                (right - 14 * S, y + 10 * S + 26 * S / 2),
                _truncate(draw, progress_disp, sf, tw * 0.4),
                font=sf,
                fill=INK,
                anchor="rm",
            )
            bar_x0, bar_x1 = tx, right - 14 * S
            bar_y0 = y + 48 * S
            bar_y1 = bar_y0 + 8 * S
            draw.rounded_rectangle(
                (bar_x0, bar_y0, bar_x1, bar_y1),
                radius=4 * S,
                fill=(48, 50, 62),
            )
            fill_r = max(0.0, min(1.0, float(progress_ratio or 0)))
            if fill_r > 0.02:
                fx1 = bar_x0 + (bar_x1 - bar_x0) * fill_r
                draw.rounded_rectangle(
                    (bar_x0, bar_y0, fx1, bar_y1),
                    radius=4 * S,
                    fill=tier_color,
                )
            # Foot: soft meta only (no second copy of the X / Y fraction)
            foot_bits: list[str] = []
            if lp and lp.lower() not in {
                hero_label.lower(),
                "cleared",
                "done",
                badge.lower(),
            }:
                foot_bits.append(lp)
            if outcome_meta:
                foot_bits.append(outcome_meta)
            foot = " · ".join(dict.fromkeys(foot_bits))
            if foot:
                draw.text(
                    (tx, bar_y1 + 8 * S),
                    _truncate(draw, foot, _font(13 * S), tw),
                    font=_font(13 * S),
                    fill=INK_SOFT,
                )
        elif meta_line:
            draw.text(
                (tx, y + 46 * S),
                _truncate(draw, meta_line, _font(14 * S), tw),
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
            outline=_mix(tier_color, HAIRLINE, 0.35),
            width=S,
        )

    if flair.get("corners"):
        _draw_corner_brackets(
            draw, outer, color=ACCENT_GOLD, S=S
        )
    edge_t = float(flair.get("edge") or 0.30)
    edge = _mix(sec_color or tier_color, HAIRLINE, edge_t)
    _clip_round(img, outer, radius, edge, max(S, 2 * S if tier_key == "ota_master" else S))
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
    """entries: {rank, name, score, avatar?, best_tier?}.

    best_tier = highest challenge tier the user has cleared (themes row border/ring).
    """
    S = SCALE
    accent = ACCENT_PURPLE
    W = 560
    row_h = 44
    top_h = 52
    margin = 28
    header = 72
    body = 0
    for e in entries:
        body += top_h if int(e.get("rank") or 99) <= 3 else row_h
        body += 2
    if not footer:
        footer = f"{BRAND_FOOTER}  ·  row color = best tier cleared"
    elif BRAND_FOOTER.lower() not in footer.lower():
        footer = f"{footer}  ·  {BRAND_FOOTER}"
    foot = 28
    H = header + body + foot

    img = Image.new("RGBA", (W * S, H * S), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    outer = (0, 0, W * S - 1, H * S - 1)
    radius = 22 * S
    draw.rounded_rectangle(outer, radius=radius, fill=BG)
    _draw_tier_wash(
        draw,
        card_w_px=W * S,
        card_h_px=H * S,
        tier_color=ACCENT_PURPLE,
        strength=0.08,
        width_frac=0.35,
    )
    draw.rounded_rectangle((0, 0, 8 * S, H * S - 1), radius=8 * S, fill=accent)
    draw.rectangle((4 * S, 0, 9 * S, H * S - 1), fill=accent)

    draw.text(
        (margin * S, 22 * S),
        _truncate(draw, _oneline(title), _font(22 * S, bold=True), (W - 2 * margin) * S),
        font=_font(22 * S, bold=True),
        fill=INK,
    )
    uf = _font(12 * S, bold=True)
    draw.text(
        ((W - margin) * S, 28 * S),
        "CLEARS",
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
        best = (e.get("best_tier") or "").lower().strip()
        tier_c = TIER_COLOR.get(best) if best else None
        flair = _tier_flair(best) if best else None
        sec = _flair_secondary(flair) if flair else None

        # Row plate + left tier border
        row_box = (
            (margin - 6) * S,
            y * S + 2 * S,
            (W - margin + 6) * S,
            (y + rh) * S - 2 * S,
        )
        if tier_c:
            plate = _mix(tier_c, BG, 0.10 if is_top else 0.07)
            draw.rounded_rectangle(
                row_box,
                radius=10 * S,
                fill=plate,
                outline=_mix(sec or tier_c, BG, 0.45 if best == "ota_master" else 0.35),
                width=max(1, (2 if best in ("hard", "ota_master") else 1) * S),
            )
            # Left accent bar inside row
            bx0 = row_box[0]
            draw.rounded_rectangle(
                (bx0, row_box[1], bx0 + 5 * S, row_box[3]),
                radius=3 * S,
                fill=tier_c,
            )
            if sec and best in ("medium", "hard", "ota_master"):
                draw.rectangle(
                    (bx0 + 5 * S, row_box[1], bx0 + 7 * S, row_box[3]),
                    fill=sec,
                )
        elif is_top:
            draw.rounded_rectangle(
                row_box,
                radius=10 * S,
                fill=_mix(medals.get(rank, accent), BG, 0.08),
            )

        # Rank column
        if is_top:
            r = 14 * S
            rcx = (margin + 18) * S
            draw.ellipse(
                (rcx - r, mid - r, rcx + r, mid + r),
                fill=medals.get(rank, accent),
            )
            draw.text(
                (rcx, mid), str(rank), font=_font(14 * S, bold=True), fill=BG, anchor="mm"
            )
        else:
            draw.text(
                ((margin + 18) * S, mid),
                str(rank),
                font=_font(14 * S, bold=True),
                fill=INK_SOFT,
                anchor="mm",
            )

        d = (32 if is_top else 26) * S
        av = _circular_avatar(e.get("avatar"), d)
        ax = (margin + 40) * S
        ay = int(mid - d / 2)
        img.paste(av, (int(ax), ay), av)
        ring = tier_c or HAIRLINE
        ring_w = max(S, (2 if best in ("hard", "ota_master") else 1) * S)
        if best == "ota_master":
            # gold outer + violet inner
            pad = 2 * S
            draw.ellipse(
                (ax - pad, ay - pad, ax + d + pad, ay + d + pad),
                outline=ACCENT_GOLD,
                width=2 * S,
            )
            draw.ellipse((ax, ay, ax + d, ay + d), outline=tier_c, width=ring_w)
        else:
            draw.ellipse((ax, ay, ax + d, ay + d), outline=ring, width=ring_w)

        sf = score_font_top if is_top else score_font
        score_fill = ACCENT_GOLD if best == "ota_master" else INK
        draw.text(((W - margin) * S, mid), score, font=sf, fill=score_fill, anchor="rm")
        sw = draw.textlength(score, font=sf)

        # Mini tier pill after name area
        name_x = margin + 40 + 32 + 12
        nf = _font(17 * S if is_top else 15 * S, bold=is_top)
        tier_pill_w = 0
        if best and tier_c:
            tlab = TIER_LABEL.get(best, best)[:3].upper()
            if best == "ota_master":
                tlab = "OTA"
            tf = _font(10 * S, bold=True)
            tw = draw.textlength(tlab, font=tf) + 12 * S
            th = tf.size + 6 * S
            tpx = name_x * S
            tpy = mid - th / 2
            draw.rounded_rectangle(
                (tpx, tpy, tpx + tw, tpy + th),
                radius=th / 2,
                fill=_mix(tier_c, BG, 0.28),
                outline=sec if best in ("hard", "ota_master") and sec else _mix(tier_c, BG, 0.55),
                width=max(1, S // 2),
            )
            draw.text(
                (tpx + tw / 2, mid),
                tlab,
                font=tf,
                fill=ACCENT_GOLD if best == "ota_master" else tier_c,
                anchor="mm",
            )
            tier_pill_w = tw + 8 * S

        name_x_px = name_x * S + tier_pill_w
        name_max = (W - margin) * S - name_x_px - sw - 20 * S
        draw.text(
            (name_x_px, mid),
            _truncate(draw, name, nf, name_max),
            font=nf,
            fill=INK,
            anchor="lm",
        )

        # subtle leader dots between name and score
        trunc = _truncate(draw, name, nf, name_max)
        dots_x0 = name_x_px + draw.textlength(trunc, font=nf) + 10 * S
        dots_x1 = (W - margin) * S - sw - 12 * S
        if dots_x1 - dots_x0 > 24 * S:
            dy = mid
            x = dots_x0
            dot_c = _mix(tier_c, HAIRLINE, 0.35) if tier_c else HAIRLINE
            while x < dots_x1:
                draw.ellipse((x, dy - S, x + 2 * S, dy + S), fill=dot_c)
                x += 6 * S

        y += rh + 2

    if footer:
        draw.text(
            (margin * S, (H - 20) * S),
            _truncate(draw, _oneline(footer), _font(12 * S), (W - 2 * margin) * S),
            font=_font(12 * S),
            fill=INK_SOFT,
        )

    _clip_round(img, outer, radius, _mix(ACCENT_PURPLE, HAIRLINE, 0.35), S)
    img = img.resize((W, H), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _render_event_chip(
    *,
    primary: str,
    secondary: str = "",
    accent: tuple[int, int, int] | None = None,
    tier: str | None = None,
    width: int = 720,
) -> bytes:
    """Thin under-banner chip for start/schedule (proto E). Side bar = tier theme."""
    S = SCALE
    W, H = width, 88
    img = Image.new("RGBA", (W * S, H * S), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    outer = (0, 0, W * S - 1, H * S - 1)
    draw.rounded_rectangle(outer, radius=14 * S, fill=BG)
    bar = accent or TIER_COLOR.get((tier or "").lower().strip(), ACCENT_PURPLE)
    draw.rectangle((0, 0, 5 * S, H * S), fill=bar)
    draw.text(
        (18 * S, 18 * S),
        _truncate(draw, _oneline(primary), _font(20 * S, True), (W - 40) * S),
        font=_font(20 * S, True),
        fill=INK,
    )
    if secondary:
        draw.text(
            (18 * S, 50 * S),
            _truncate(draw, _oneline(secondary), _font(14 * S), (W - 40) * S),
            font=_font(14 * S),
            fill=INK_SOFT,
        )
    _clip_round(img, outer, 14 * S, _mix(bar, HAIRLINE, 0.3), S)
    img = img.resize((W, H), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def render_event_chip(**kwargs) -> bytes:
    return await asyncio.to_thread(_render_event_chip, **kwargs)


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
    "new_challenger": 0x5B9CFA,
}

_TIER_EMBED_COLOR = {
    "easy": 0x3DD68C,
    "medium": 0x38BDF8,
    "hard": 0xF07178,
    "ota_master": 0xE4C460,  # gold prestige strip
}

_TIER_CAPTION = {
    "easy": "🟢 **Easy**",
    "medium": "🔵 **Medium**",
    "hard": "🔴 **Hard**",
    "ota_master": "★ **Ota-Master**",
}


def tier_embed_color(tier: str | None) -> int | None:
    if not tier:
        return None
    return _TIER_EMBED_COLOR.get((tier or "").lower().strip())


def tier_caption_mark(tier: str | None) -> str:
    if not tier:
        return ""
    return _TIER_CAPTION.get((tier or "").lower().strip(), "")


def files_as_image_embeds(
    files,
    *,
    kind: str | None = None,
    color: int | None = None,
    tier: str | None = None,
    colors: list[int] | None = None,
    chrome: bool = False,
):
    """One embed per file with set_image(attachment://…) so Discord shows them full-size.

    chrome=False (default): no left color strip — avoids double border with PNG rails.
    chrome=True: prefer colors[] / color / tier / kind.
    """
    import discord

    fallback: int | None = None
    if chrome:
        fallback = (
            color
            if color is not None
            else (tier_embed_color(tier) if tier else None)
            or _KIND_EMBED_COLOR.get(kind or "", 0x2F3136)
        )
    elif color is not None:
        fallback = color
    embeds: list = []
    for i, f in enumerate(files or []):
        name = getattr(f, "filename", None) or "image.png"
        c = fallback
        if colors is not None and i < len(colors) and colors[i] is not None:
            c = colors[i]
        emb = discord.Embed(color=int(c)) if c is not None else discord.Embed()
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
        "line_secondary": "24 hours · Ends Sat 28 Mar 18:00 UTC",
        "footer": "",
    },
    "scheduled": {
        "display_name": "Monty",
        "challenge_title": "Anime Sprint",
        "tier": "easy",
        "number": 2,
        "focus": "Frieren",
        "line_primary": "Queued to start",
        "line_secondary": "24 hours · Sun 29 Mar 09:00 -> Mon 30 Mar 09:00 UTC",
        "footer": "",
    },
    "active": {
        "display_name": "Monty",
        "challenge_title": "VN 30k",
        "tier": "easy",
        "number": 4,
        "focus": "Steins;Gate",
        "line_primary": "In progress",
        "line_secondary": "24 hours · Ends Sat 28 Mar 18:00 UTC",
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
        "display_name": "PatChatGC",
        "challenge_title": "12-Hour Immersion Day",
        "tier": "ota_master",
        "number": 4,
        "focus": "",
        "line_primary": "Halfway",
        "line_secondary": (
            "12 / 24 hours · 11h 59m left · honor · still running · "
            "confirm when finished"
        ),
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
                cover_q = parse_content_items(str(payload.get("focus") or ""))
                cover_q = cover_q[0] if cover_q else str(payload["focus"])
                poster = await fetch_poster(
                    cover_q, media_by_kind.get(kind, "any")
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
            {
                "rank": 1,
                "name": "Alice",
                "score": 12,
                "avatar": demo_avatar_png((214, 175, 84)),
                "best_tier": "ota_master",
            },
            {
                "rank": 2,
                "name": "Bob",
                "score": 7,
                "avatar": demo_avatar_png((183, 189, 201)),
                "best_tier": "hard",
            },
            {
                "rank": 3,
                "name": "Cara",
                "score": 5,
                "avatar": demo_avatar_png((200, 143, 92)),
                "best_tier": "medium",
            },
            {"rank": 4, "name": "Dan", "score": 3, "avatar": None, "best_tier": "easy"},
            {"rank": 5, "name": "Eve", "score": 1, "avatar": None, "best_tier": None},
        ],
        footer="5 ranked · clears",
    )
    out.append(("leaderboard", lb))
    return out
