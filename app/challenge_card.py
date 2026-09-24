"""Challenge lifecycle cards (Pillow PNG) — distinct from Tadoku log cards."""

from __future__ import annotations

import asyncio
import io
import re
from typing import Optional

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

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
    "userinfo": ("PROFILE", ACCENT_PURPLE),
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
    "userinfo": "Profile",
    "leaderboard": "Board",
}

TIER_LABEL = {
    "easy": "Easy",
    "medium": "Medium",
    "hard": "Hard",
    "ota_master": "Ota-Master",
    "special": "Special",
}

TIER_COLOR = {
    "easy": ACCENT_GREEN,
    "medium": ACCENT_CYAN,
    "hard": ACCENT_RED,
    "ota_master": ACCENT_VIOLET,
    "special": ACCENT_AMBER,
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
    "special": {
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
        "secondary": ACCENT_AMBER,
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

# Allow 12,688 / 50,000 (commas) — bare [\d.]+ matched 688/50 and broke bars
_PROGRESS_RE = re.compile(
    r"(?P<a>\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)\s*(?P<ak>k)?"
    r"\s*/\s*"
    r"(?P<b>\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)\s*(?P<bk>k)?",
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


# Color emoji → .notdef tofu with Noto/DejaVu (no CBDT). Drop for card text.
_EMOJI_RE = re.compile(
    "["
    "\U0001F1E0-\U0001F1FF"  # flags
    "\U0001F300-\U0001F5FF"
    "\U0001F600-\U0001F64F"
    "\U0001F680-\U0001F6FF"
    "\U0001F700-\U0001F77F"
    "\U0001F780-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FAFF"
    "\U00002702-\U000027B0"
    "\U00002600-\U000026FF"
    "\U0000FE00-\U0000FE0F"  # variation selectors
    "\U0000200D"  # ZWJ
    "]+",
)


def _oneline(text: str) -> str:
    text = _EMOJI_RE.sub("", text or "")
    return " ".join(text.split())


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


def _iter_cleared_tiers(tiers) -> list[str]:
    seen = {(t or "").lower().strip() for t in (tiers or [])}
    return [t for t in TIER_LABEL if t in seen]


def _hanko_stamp(tier: str, S: int, *, compact: bool = False) -> Image.Image:
    """Tilted 完 chop in tier color (double-frame square)."""
    color = ACCENT_GOLD if tier == "ota_master" else TIER_COLOR.get(tier, ACCENT_GREEN)
    side = (20 if compact else 32) * S
    stroke = max(1, S if compact else 2 * S)
    pad = 3 * S
    im = Image.new("RGBA", (side + pad * 2, side + pad * 2), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    x0, y0 = pad, pad
    x1, y1 = pad + side - 1, pad + side - 1
    d.rectangle((x0, y0, x1, y1), outline=color, width=stroke)
    if not compact:
        inset = stroke + max(1, S // 2)
        d.rectangle(
            (x0 + inset, y0 + inset, x1 - inset, y1 - inset),
            outline=color,
            width=max(1, S),
        )
    gf = _font(max(10, int(side * 0.64)), bold=True)
    d.text(((x0 + x1) / 2, (y0 + y1) / 2), "完", font=gf, fill=color, anchor="mm")
    return im.rotate(-10, resample=Image.Resampling.BICUBIC, expand=True)


def _complete_badge_size(
    draw: ImageDraw.ImageDraw, tier: str, S: int, *, compact: bool
) -> tuple[float, float]:
    w, h = _hanko_stamp(tier, S, compact=compact).size
    return float(w), float(h)


def _complete_badges_width(
    draw: ImageDraw.ImageDraw,
    tiers,
    S: int,
    *,
    compact: bool = False,
    gap: int | None = None,
) -> float:
    keys = _iter_cleared_tiers(tiers)
    if not keys:
        return 0.0
    g = (2 if compact else 4) * S if gap is None else gap * S
    w = sum(_complete_badge_size(draw, t, S, compact=compact)[0] for t in keys)
    return w + g * (len(keys) - 1)


def _draw_complete_badges(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    x: float,
    y_mid: float,
    tiers,
    S: int,
    *,
    compact: bool = False,
) -> float:
    """Hanko 完 stamps, one per swept tier."""
    _ = draw
    keys = _iter_cleared_tiers(tiers)
    if not keys:
        return 0.0
    g = (2 if compact else 4) * S
    cx = x
    for t in keys:
        stamp = _hanko_stamp(t, S, compact=compact)
        tw, th = stamp.size
        img.alpha_composite(stamp, (int(round(cx)), int(round(y_mid - th / 2))))
        cx += tw + g
    return cx - g - x


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


_TIME_LEFT_RE = re.compile(
    r"(?<![\w/])(\d+h\s+\d+m left|\d+m left)(?![\w/])",
    re.IGNORECASE,
)


def _peel_time_left(text: str) -> tuple[str, str]:
    """Pull '3h 12m left' out of a meta string so it can sit in its own slot."""
    text = (text or "").strip()
    if not text:
        return "", ""
    m = _TIME_LEFT_RE.search(text)
    if not m:
        return "", text
    left = m.group(1).strip()
    rest = (text[: m.start()] + text[m.end() :]).strip(" ·•|-")
    rest = re.sub(r"\s*·\s*·\s*", " · ", rest).strip(" ·")
    return left, rest


def _parse_progress(text: str) -> tuple[Optional[float], str]:
    """If secondary has 'X / Y', return (ratio 0..1+, 'frac[ unit] · meta')."""
    text = _oneline(text)
    m = _PROGRESS_RE.search(text)
    if not m:
        return None, text
    a = float(m.group("a").replace(",", "")) * (1000 if m.group("ak") else 1)
    b = float(m.group("b").replace(",", "")) * (1000 if m.group("bk") else 1)
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
    time_left: str = "",
    cleared_tiers: list | None = None,
    name_tier: str | None = None,
) -> bytes:
    """
    Layout:
      [av]  NAME
            [TIER PILL] #N
            title · focus (if no poster)
      [ outcome: hero + progress bar; time left is its own right-side slot ]
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

    # Dedicated time-left slot (explicit arg wins; else peel from meta)
    time_left = _oneline(time_left or "")
    if not time_left:
        peeled, outcome_meta = _peel_time_left(outcome_meta)
        time_left = peeled
    else:
        _, outcome_meta = _peel_time_left(outcome_meta)

    # Always show in-card status hero (Started / Cleared / …) — no corner pill
    has_outcome = True
    has_progress = progress_ratio is not None
    has_time_left = bool(time_left)
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
        # Extra foot row when time left sits opposite the progress fraction
        outcome_h = (96 if has_time_left else 90) if _tall else (88 if has_time_left else 82)
    elif has_time_left or outcome_meta or line_secondary or line_primary:
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
    badges_w = _complete_badges_width(draw, cleared_tiers, S)
    name_max = max(40.0, right - cx - (badges_w + 10 * S if badges_w else 0))
    nk = (name_tier or "").lower().strip()
    name_fill = (
        ACCENT_GOLD if nk == "ota_master" else TIER_COLOR[nk]
    ) if nk in TIER_COLOR else INK
    draw.text(
        (cx, y),
        _truncate(draw, display_name, name_font, name_max),
        font=name_font,
        fill=name_fill,
    )
    if badges_w:
        _draw_complete_badges(
            img, draw, right - badges_w, y + name_font.size * 0.55, cleared_tiers, S
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
            # Progress amount lives top-right; don't also cram it into meta
            if outcome_meta:
                meta_bits.append(outcome_meta)
        elif outcome_meta:
            meta_bits.append(outcome_meta)
        elif line_secondary and not has_progress and not has_time_left:
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
            # Reserve right column for progress amount and/or time left
            has_progress=has_progress or has_time_left,
        )

        # Top-right: goal progress amount (exclusive slot)
        if has_progress and progress_disp:
            sf = _font(15 * S, bold=True)
            draw.text(
                (right - 14 * S, y + 10 * S + 26 * S / 2),
                _truncate(draw, progress_disp, sf, tw * 0.4),
                font=sf,
                fill=INK,
                anchor="rm",
            )
        elif has_time_left:
            # No progress bar → time left shares the top-right hero column
            sf = _font(15 * S, bold=True)
            draw.text(
                (right - 14 * S, y + 10 * S + 26 * S / 2),
                _truncate(draw, time_left, sf, tw * 0.42),
                font=sf,
                fill=INK_SOFT,
                anchor="rm",
            )

        if has_progress:
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
            # Foot: left = soft meta; right = time left (never stacked on progress amt)
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
            foot_y = bar_y1 + 8 * S
            if has_time_left:
                tl_f = _font(13 * S, bold=True)
                tl_w = draw.textlength(time_left, font=tl_f)
                draw.text(
                    (right - 14 * S, foot_y),
                    time_left,
                    font=tl_f,
                    fill=INK_SOFT,
                    anchor="rm",
                )
                foot_max = max(40.0, tw - tl_w - 16 * S)
            else:
                foot_max = tw
            if foot:
                draw.text(
                    (tx, foot_y),
                    _truncate(draw, foot, _font(13 * S), foot_max),
                    font=_font(13 * S),
                    fill=INK_SOFT,
                )
        elif meta_line:
            # Leave room on the right if time left already claimed top-right
            meta_max = tw * (0.55 if has_time_left else 1.0)
            draw.text(
                (tx, y + 46 * S),
                _truncate(draw, meta_line, _font(14 * S), meta_max),
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
    """entries: {rank, name, clears|score, points?, fails?, avatar?, best_tier?}.

    best_tier = highest fully swept prestige tier (themes row; None → UNRANKED).
    Right side: PTS · CLR · F (points bold = sort key).
    """
    S = SCALE
    accent = ACCENT_PURPLE
    W = 580
    row_h = 44
    top_h = 52
    margin = 28
    header = 72
    # Fixed right stat columns (right edges, logical px) — PTS · CLR · F
    col_f = W - margin          # fails right edge
    col_c = col_f - 44          # clears right edge
    col_p = col_c - 52          # points right edge
    stats_left = col_p - 36     # name must stop before points
    body = 0
    for e in entries:
        body += top_h if int(e.get("rank") or 99) <= 3 else row_h
        body += 2
    if not footer:
        footer = f"{BRAND_FOOTER}  ·  sorted by points"
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
        _truncate(draw, _oneline(title), _font(22 * S, bold=True), (stats_left - margin) * S),
        font=_font(22 * S, bold=True),
        fill=INK,
    )
    uf = _font(11 * S, bold=True)
    for label, x in (("PTS", col_p), ("CLR", col_c), ("F", col_f)):
        draw.text((x * S, 28 * S), label, font=uf, fill=INK_SOFT, anchor="rt")
    draw.line(
        (margin * S, (header - 10) * S, (W - margin) * S, (header - 10) * S),
        fill=HAIRLINE,
        width=S,
    )

    medals = {1: (214, 175, 84), 2: (183, 189, 201), 3: (200, 143, 92)}
    y = header
    pts_font_top = _font(18 * S, bold=True)
    pts_font = _font(16 * S, bold=True)
    sec_font = _font(13 * S)
    fail_font = _font(13 * S)
    for e in entries:
        rank = int(e.get("rank") or 0)
        is_top = 1 <= rank <= 3
        rh = top_h if is_top else row_h
        mid = (y + rh / 2) * S
        name = _oneline(str(e.get("name") or "?"))
        clears = e.get("clears")
        if clears is None:
            clears = e.get("score") or 0
        points = e.get("points")
        if points is None:
            points = e.get("score") or 0
        fails = e.get("fails") or 0
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

        # Stats: points (bold) · clears · fails
        pf = pts_font_top if is_top else pts_font
        pts_fill = ACCENT_GOLD if best == "ota_master" else INK
        draw.text((col_p * S, mid), str(int(points)), font=pf, fill=pts_fill, anchor="rm")
        draw.text(
            (col_c * S, mid),
            str(int(clears)),
            font=sec_font,
            fill=INK_SOFT,
            anchor="rm",
        )
        fail_fill = ACCENT_RED if int(fails) > 0 else INK_SOFT
        draw.text(
            (col_f * S, mid),
            str(int(fails)),
            font=fail_font,
            fill=fail_fill,
            anchor="rm",
        )

        # Mini tier pill after name area (UNR until a full tier is swept)
        name_x = margin + 40 + 32 + 12
        nf = _font(17 * S if is_top else 15 * S, bold=is_top)
        if best and tier_c:
            tlab = "OTA" if best == "ota_master" else TIER_LABEL.get(best, best)[:3].upper()
            pill_fill = _mix(tier_c, BG, 0.28)
            pill_outline = (
                sec if best in ("hard", "ota_master") and sec else _mix(tier_c, BG, 0.55)
            )
            pill_ink = ACCENT_GOLD if best == "ota_master" else tier_c
        else:
            tlab = "UNR"
            pill_fill = _mix(INK_SOFT, BG, 0.18)
            pill_outline = HAIRLINE
            pill_ink = INK_SOFT
        tf = _font(10 * S, bold=True)
        tw = draw.textlength(tlab, font=tf) + 12 * S
        th = tf.size + 6 * S
        tpx = name_x * S
        tpy = mid - th / 2
        draw.rounded_rectangle(
            (tpx, tpy, tpx + tw, tpy + th),
            radius=th / 2,
            fill=pill_fill,
            outline=pill_outline,
            width=max(1, S // 2),
        )
        draw.text(
            (tpx + tw / 2, mid),
            tlab,
            font=tf,
            fill=pill_ink,
            anchor="mm",
        )
        tier_pill_w = tw + 8 * S

        name_x_px = name_x * S + tier_pill_w
        swept = e.get("cleared_tiers")
        bw = _complete_badges_width(draw, swept, S, compact=True)
        gap = 6 * S if bw else 0
        name_max = stats_left * S - name_x_px - 8 * S - bw - gap
        draw.text(
            (name_x_px, mid),
            _truncate(draw, name, nf, name_max),
            font=nf,
            fill=INK,
            anchor="lm",
        )
        if bw:
            _draw_complete_badges(
                img, draw, stats_left * S - bw, mid, swept, S, compact=True
            )

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


def _render_userinfo(
    *,
    display_name: str,
    avatar: Optional[bytes] = None,
    points: int = 0,
    clears: int = 0,
    rank: int | None = None,
    ranked: int = 0,
    best_tier: str | None = None,
    by_tier: dict | None = None,
    fails: int = 0,
    active: list | None = None,
    completed: list | None = None,
    tadoku_linked: bool = False,
    footer: str = "",
    cleared_tiers: list | None = None,
) -> bytes:
    """Profile card: points/clears/rank + open + completed lists.

    active/completed entries: {title, tier, number?, status?}
    by_tier: {easy/medium/hard/ota_master: count}
    """
    S = SCALE
    accent = ACCENT_PURPLE
    W = 560
    margin = 28
    best = (best_tier or "").lower().strip()
    best_c = TIER_COLOR.get(best, accent) if best else accent
    flair = _tier_flair(best) if best else None
    sec_color = _flair_secondary(flair) if flair else None
    active = list(active or [])
    completed = list(completed or [])
    by_tier = by_tier or {}

    # Measure body height
    header = 118
    stats_h = 64
    tier_h = 36 if any(by_tier.get(t) for t in TIER_LABEL) else 0
    row_h = 34
    active_hdr = 28 if active else 0
    list_hdr = 28 if completed else 0
    empty_h = 28 if not completed else 0
    body = (
        stats_h
        + tier_h
        + active_hdr
        + len(active) * row_h
        + list_hdr
        + (len(completed) * row_h if completed else empty_h)
    )
    if not footer:
        footer = BRAND_FOOTER
    elif BRAND_FOOTER.lower() not in footer.lower():
        footer = f"{footer}  ·  {BRAND_FOOTER}"
    foot = 28
    H = header + body + foot

    img = Image.new("RGBA", (W * S, H * S), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    outer = (0, 0, W * S - 1, H * S - 1)
    radius = 22 * S
    draw.rounded_rectangle(outer, radius=radius, fill=BG)
    # Same tier chrome language as completion cards
    wash_s = float(flair.get("wash") or 0.1) if flair else 0.08
    wash_w = float(flair.get("wash_width") or 0.38) if flair else 0.35
    _draw_tier_wash(
        draw,
        card_w_px=W * S,
        card_h_px=H * S,
        tier_color=best_c,
        strength=wash_s,
        width_frac=wash_w,
    )
    rail_w = max(3, int((flair or {}).get("rail_kind") or 4)) * S
    if flair and int(flair.get("rail_tier") or 0) > 0:
        rail_w = max(rail_w, int(flair["rail_tier"]) * S + rail_w // 2)
    draw.rounded_rectangle((0, 0, rail_w, H * S - 1), radius=8 * S, fill=best_c)
    if flair and flair.get("top_rule"):
        rule_c = sec_color or best_c
        draw.rectangle(
            (rail_w, 0, W * S - 1, max(2, 2 * S)),
            fill=_mix(rule_c, BG, 0.65),
        )

    # Avatar + name — rings match completion (gold outer + tier inner for ota)
    d = 56 * S
    av = _circular_avatar(avatar, d)
    ax, ay = margin * S, 22 * S
    img.paste(av, (int(ax), int(ay)), av)
    if flair:
        outer_w = int(flair.get("ring_outer") or 0) * S
        ring_w = max(1, int(flair.get("ring") or 2) * S)
        oc = _ring_outer_color(flair, best_c)
        if outer_w > 0 and oc is not None:
            pad = outer_w + S
            draw.ellipse(
                (ax - pad, ay - pad, ax + d + pad, ay + d + pad),
                outline=_mix(oc, BG, 0.75),
                width=outer_w,
            )
        draw.ellipse((ax, ay, ax + d, ay + d), outline=best_c, width=ring_w)
    else:
        draw.ellipse((ax, ay, ax + d, ay + d), outline=HAIRLINE, width=2 * S)

    name_x = margin + 56 + 14
    nf = _font(22 * S, bold=True)
    badges_w = _complete_badges_width(draw, cleared_tiers, S)
    name_max = (W - name_x - margin) * S - (badges_w + 10 * S if badges_w else 0)
    draw.text(
        (name_x * S, 28 * S),
        _truncate(draw, _oneline(display_name), nf, name_max),
        font=nf,
        fill=INK,
    )
    if badges_w:
        _draw_complete_badges(
            img,
            draw,
            (W - margin) * S - badges_w,
            28 * S + nf.size * 0.55,
            cleared_tiers,
            S,
        )
    # Swept-tier pill under name (UNRANKED until a full tier is cleared)
    pill_font = _font(11 * S, bold=True)
    if best:
        tlabel = TIER_LABEL.get(best, best).upper()
        if best == "ota_master":
            tlabel = "★  OTA-MASTER"
        extra_pad = int((flair or {}).get("tier_pill_pad") or 0) * S
        pill_fill = _mix(best_c, BG, 0.28 if best != "easy" else 0.22)
        pill_outline = (
            sec_color
            if best in ("hard", "ota_master") and sec_color
            else _mix(best_c, BG, 0.55)
        )
        pill_ink = ACCENT_GOLD if best == "ota_master" else best_c
        outline_w = (1 + (1 if best in ("hard", "ota_master") else 0)) * max(
            1, S // 2
        )
    else:
        tlabel = "UNRANKED"
        extra_pad = 0
        pill_fill = _mix(INK_SOFT, BG, 0.18)
        pill_outline = HAIRLINE
        pill_ink = INK_SOFT
        outline_w = max(1, S // 2)
    tpad_x = 10 * S + extra_pad
    tpad_y = 4 * S + max(0, extra_pad // 2)
    th = pill_font.size + tpad_y * 2
    tw = draw.textlength(tlabel, font=pill_font) + tpad_x * 2
    tpx = name_x * S
    tpy = 60 * S  # below name (was overlapping at 54)
    draw.rounded_rectangle(
        (tpx, tpy, tpx + tw, tpy + th),
        radius=th / 2,
        fill=pill_fill,
        outline=pill_outline,
        width=max(1, outline_w),
    )
    draw.text(
        (tpx + tw / 2, tpy + th / 2),
        tlabel,
        font=pill_font,
        fill=pill_ink,
        anchor="mm",
    )
    if best and flair and flair.get("spark"):
        sy = int(tpy + th / 2)
        for dx in (-6 * S, tw + 6 * S):
            sx = int(tpx + dx)
            draw.ellipse((sx - S, sy - S, sx + S, sy + S), fill=ACCENT_GOLD)
    tad = "Tadoku linked" if tadoku_linked else "Tadoku not linked"
    draw.text(
        (name_x * S, 90 * S),
        tad,
        font=_font(12 * S),
        fill=ACCENT_GREEN if tadoku_linked else INK_SOFT,
    )

    y = header
    draw.line(
        (margin * S, (y - 8) * S, (W - margin) * S, (y - 8) * S),
        fill=HAIRLINE,
        width=S,
    )

    # Stat chips: Points / Clears / Rank / Fails
    rank_s = f"#{rank}" if rank else "—"
    if rank and ranked:
        rank_s = f"#{rank}/{ranked}"
    chips = [
        ("POINTS", str(int(points)), ACCENT_GOLD if best == "ota_master" else INK),
        ("CLEARS", str(int(clears)), INK),
        ("RANK", rank_s, INK),
        ("FAILS", str(int(fails)), ACCENT_RED if fails else INK_SOFT),
    ]
    chip_w = (W - 2 * margin - 9 * 3) / 4
    chip_h = stats_h - 10  # air under chips before next section
    for i, (lab, val, fill) in enumerate(chips):
        cx = margin + i * (chip_w + 9)
        box = (cx * S, y * S, (cx + chip_w) * S, (y + chip_h) * S)
        chip_outline = None
        if best == "ota_master" and i == 0:
            chip_outline = ACCENT_GOLD
        elif best in ("hard", "ota_master") and sec_color and i == 0:
            chip_outline = sec_color
        draw.rounded_rectangle(
            box,
            radius=10 * S,
            fill=PANEL_BG,
            outline=chip_outline,
            width=max(1, S // 2) if chip_outline else 0,
        )
        mid_x = (cx + chip_w / 2) * S
        lab_f = _font(10 * S, bold=True)
        val_f = _font(18 * S, bold=True)
        # Fixed insets so value isn't flush with chip bottom
        pad_y = 11
        draw.text(
            (mid_x, (y + pad_y) * S),
            lab,
            font=lab_f,
            fill=INK_SOFT,
            anchor="mt",
        )
        draw.text(
            (mid_x, (y + chip_h - pad_y) * S),
            _truncate(draw, val, val_f, chip_w * S - 12 * S),
            font=val_f,
            fill=fill,
            anchor="mb",  # bottom of glyph sits pad_y above chip edge
        )
    y += stats_h

    # Tier breakdown pills (ota/hard match completion pill chrome)
    if tier_h:
        px = margin * S
        mid = (y + tier_h / 2 - 4) * S
        for tkey, tlab in TIER_LABEL.items():
            n = int(by_tier.get(tkey) or 0)
            if n <= 0:
                continue
            tc = TIER_COLOR[tkey]
            tf_flair = _tier_flair(tkey)
            tf_sec = _flair_secondary(tf_flair)
            short = "OTA" if tkey == "ota_master" else tlab[:3].upper()
            txt = f"{short} {n}"
            tf = _font(11 * S, bold=True)
            tw = draw.textlength(txt, font=tf) + 16 * S
            th = 22 * S
            outline = (
                tf_sec
                if tkey in ("hard", "ota_master") and tf_sec
                else _mix(tc, BG, 0.55)
            )
            draw.rounded_rectangle(
                (px, mid - th / 2, px + tw, mid + th / 2),
                radius=th / 2,
                fill=_mix(tc, BG, 0.28),
                outline=outline,
                width=max(
                    1,
                    (1 + (1 if tkey in ("hard", "ota_master") else 0)) * max(1, S // 2),
                ),
            )
            draw.text(
                (px + tw / 2, mid),
                txt,
                font=tf,
                fill=ACCENT_GOLD if tkey == "ota_master" else tc,
                anchor="mm",
            )
            px += tw + 8 * S
        y += tier_h

    def _draw_rows(entries: list, *, title_fill=INK, show_progress: bool = False) -> None:
        nonlocal y
        for e in entries:
            tier = (e.get("tier") or "").lower().strip()
            tc = TIER_COLOR.get(tier, HAIRLINE)
            row_flair = _tier_flair(tier) if tier in TIER_COLOR else None
            row_sec = _flair_secondary(row_flair) if row_flair else None
            title = _oneline(str(e.get("title") or "?"))
            num = e.get("number")
            if num is not None and str(num).strip() != "":
                title = f"#{num} {title}"
            st = (e.get("status") or "").lower().strip()
            if st == "scheduled":
                title = f"{title} · sched"
            mid = (y + row_h / 2) * S
            draw.rounded_rectangle(
                ((margin - 2) * S, y * S + 4 * S, (margin + 3) * S, (y + row_h) * S - 4 * S),
                radius=2 * S,
                fill=tc,
            )
            tlab = "OTA" if tier == "ota_master" else TIER_LABEL.get(tier, tier or "?")[:3].upper()
            tf = _font(10 * S, bold=True)
            tw = draw.textlength(tlab, font=tf) + 10 * S
            th = 18 * S
            tpx = (margin + 10) * S
            outline = (
                row_sec
                if tier in ("hard", "ota_master") and row_sec
                else None
            )
            draw.rounded_rectangle(
                (tpx, mid - th / 2, tpx + tw, mid + th / 2),
                radius=th / 2,
                fill=_mix(tc, BG, 0.28),
                outline=outline,
                width=max(1, S // 2) if outline else 0,
            )
            draw.text(
                (tpx + tw / 2, mid),
                tlab,
                font=tf,
                fill=ACCENT_GOLD if tier == "ota_master" else tc,
                anchor="mm",
            )
            tx = margin + 10 + tw / S + 8
            # Optional % on the right (active rows only when we have a poll cache)
            pct_s = ""
            pct_w = 0.0
            if show_progress:
                pct = e.get("progress_pct")
                if pct is not None:
                    try:
                        pct_s = f"{int(round(float(pct)))}%"
                    except (TypeError, ValueError):
                        pct_s = ""
                if pct_s:
                    pf = _font(12 * S, bold=True)
                    pct_w = draw.textlength(pct_s, font=pf) + 4 * S
                    draw.text(
                        ((W - margin) * S, mid),
                        pct_s,
                        font=pf,
                        fill=title_fill,
                        anchor="rm",
                    )
            max_title = (W - margin - tx) * S - pct_w - (8 * S if pct_s else 0)
            draw.text(
                (tx * S, mid),
                _truncate(draw, title, _font(13 * S), max_title),
                font=_font(13 * S),
                fill=title_fill,
                anchor="lm",
            )
            y += row_h

    if active:
        draw.text(
            (margin * S, y * S),
            "ACTIVE",
            font=_font(11 * S, bold=True),
            fill=INK_SOFT,
        )
        y += active_hdr
        _draw_rows(active, title_fill=ACCENT_GREEN, show_progress=True)

    # Completed list
    if completed:
        draw.text(
            (margin * S, y * S),
            "COMPLETED",
            font=_font(11 * S, bold=True),
            fill=INK_SOFT,
        )
        y += list_hdr
        _draw_rows(completed)
    else:
        draw.text(
            (margin * S, y * S + 4 * S),
            "No clears yet",
            font=_font(14 * S),
            fill=INK_SOFT,
        )

    if footer:
        draw.text(
            (margin * S, (H - 20) * S),
            _truncate(draw, _oneline(footer), _font(12 * S), (W - 2 * margin) * S),
            font=_font(12 * S),
            fill=INK_SOFT,
        )

    if flair and flair.get("corners"):
        _draw_corner_brackets(draw, outer, color=ACCENT_GOLD, S=S)
    edge_t = float((flair or {}).get("edge") or 0.30)
    edge = _mix(sec_color or best_c, HAIRLINE, edge_t) if best else _mix(accent, HAIRLINE, 0.35)
    _clip_round(
        img,
        outer,
        radius,
        edge,
        max(S, 2 * S if best == "ota_master" else S),
    )
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


def _stroke_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    text: str,
    font: ImageFont.ImageFont,
    fill,
    *,
    stroke_fill=(0, 0, 0, 230),
    stroke_width: int = 3,
) -> None:
    try:
        draw.text(
            xy,
            text,
            font=font,
            fill=fill,
            stroke_width=stroke_width,
            stroke_fill=stroke_fill,
        )
    except TypeError:
        x, y = xy
        for dx, dy in (
            (-stroke_width, 0),
            (stroke_width, 0),
            (0, -stroke_width),
            (0, stroke_width),
            (-2, -2),
            (2, -2),
            (-2, 2),
            (2, 2),
        ):
            draw.text((x + dx, y + dy), text, font=font, fill=stroke_fill)
        draw.text(xy, text, font=font, fill=fill)


def _bottom_scrim(
    im: Image.Image, *,
    strength: int = 245,
    start_frac: float = 0.40,
) -> Image.Image:
    """Darken lower portion so white name/meta pop."""
    w, h = im.size
    g = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    gd = ImageDraw.Draw(g)
    y0 = int(h * start_frac)
    span = max(1.0, h - y0)
    for i in range(y0, h):
        t = (i - y0) / span
        a = int(strength * (t**0.85))
        gd.line((0, i, w, i), fill=(6, 4, 10, min(240, a)))
    return Image.alpha_composite(im.convert("RGBA"), g)


def _render_unified_start(
    *,
    display_name: str,
    challenge_title: str,
    tier: str,
    number: int,
    avatar_bytes: Optional[bytes],
    focus: str,
    meta_line: str = "",
    schedule_label: str | None = None,
    include_challenger: bool = True,
    poster_bytes: Optional[bytes] = None,
    name_tier: str | None = None,
    cleared_tiers: list | None = None,
) -> bytes:
    """One-image start/scheduled card (Gbar_E_glow).

    NC banner full-bleed; name + challenge tier/meta on the art.
    name_tier = highest fully swept prestige tier (colors the display name).
    tier = challenge being started (colors the tier label under the name).
    poster_bytes ignored (no series underlay).
    """
    from app.new_challenger import render_new_challenger

    _ = poster_bytes  # API compat; series alpha dropped
    S = SCALE
    W = WIDTH * S
    display_name = _oneline(display_name)
    challenge_title = _oneline(challenge_title)
    focus = _oneline(focus)
    meta_line = _oneline(meta_line)
    schedule_label = _oneline(schedule_label) if schedule_label else None
    tier_key = (tier or "").lower().strip()
    tier_color = TIER_COLOR.get(tier_key, ACCENT_PURPLE)
    tier_fill = ACCENT_GOLD if tier_key == "ota_master" else tier_color
    if tier_key == "ota_master":
        tlabel = "★ OTA-MASTER"
    else:
        tlabel = TIER_LABEL.get(tier_key, (tier or "?").replace("_", " ").title()).upper()

    # Name themed by best clear (not the challenge just started)
    nk = (name_tier or "").lower().strip()
    if nk in TIER_COLOR:
        name_fill = ACCENT_GOLD if nk == "ota_master" else TIER_COLOR[nk]
        name_glow = name_fill
    else:
        name_fill = INK
        name_glow = ACCENT_PURPLE

    bits: list[str] = []
    if number:
        bits.append(f"#{int(number)}")
    if challenge_title:
        bits.append(challenge_title)
    if focus:
        bits.append(focus)
    if schedule_label:
        bits.append(schedule_label)
    elif meta_line:
        bits.append(meta_line)
    sub = "  ·  ".join(bits)

    nc_im: Image.Image | None = None
    if include_challenger:
        try:
            nc_png = render_new_challenger(avatar_bytes, tier=None)
        except Exception:  # noqa: BLE001
            nc_png = None
        if nc_png:
            nc_im = Image.open(io.BytesIO(nc_png)).convert("RGBA")
            if nc_im.size[0] != W:
                nh = max(1, int(nc_im.size[1] * W / max(nc_im.size[0], 1)))
                nc_im = nc_im.resize((W, nh), Image.Resampling.LANCZOS)

    if nc_im is not None:
        canvas = nc_im
    else:
        # Go-live / NC off: dark plate only
        canvas = Image.new("RGBA", (W, 220 * S), (8, 6, 10, 255))
        if avatar_bytes:
            av = _circular_avatar(avatar_bytes, 72 * S)
            canvas.paste(av, (24 * S, 40 * S), av)

    # Scrim on the art so overlaid text reads
    canvas = _bottom_scrim(canvas, strength=250, start_frac=0.40)
    w, h = canvas.size

    name_f = _font(40 * S, bold=True)
    # On-art, slightly lower than original E (was h-100)
    name_y = h - 88 * S
    glow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(glow).text(
        (22 * S, name_y), display_name, font=name_f, fill=(*name_glow, 200)
    )
    glow = glow.filter(ImageFilter.GaussianBlur(max(4, 8 * S // 2)))
    canvas = Image.alpha_composite(canvas, glow)
    draw = ImageDraw.Draw(canvas)
    _stroke_text(
        draw,
        (22 * S, name_y),
        display_name,
        name_f,
        name_fill,
        stroke_width=max(2, S),
    )
    if _iter_cleared_tiers(cleared_tiers):
        _draw_complete_badges(
            canvas, draw, 22 * S, name_y - 22 * S, cleared_tiers, S
        )

    tf = _font(14 * S, bold=True)
    tw = draw.textlength(tlabel, font=tf)
    tx = 22 * S
    ty = h - 40 * S
    draw.text((tx, ty), tlabel, font=tf, fill=tier_fill)
    # Thin underline (thick bar read as "blocks" next to the name row)
    draw.rectangle(
        (tx, ty + tf.size + 2 * S, tx + tw, ty + tf.size + 4 * S),
        fill=tier_fill,
    )
    if sub:
        # Regular weight — bold CJK fallback can tofu on some hosts
        sf = _font(14 * S, bold=False)
        draw.text(
            (tx + tw + 14 * S, ty + 2 * S),
            _truncate(draw, sub, sf, W - tx - tw - 36 * S),
            font=sf,
            fill=(210, 212, 220),
        )

    out = canvas.resize((WIDTH, max(1, h // S)), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue()


async def render_event_chip(**kwargs) -> bytes:
    return await asyncio.to_thread(_render_event_chip, **kwargs)


async def render_challenge_card(**kwargs) -> bytes:
    return await asyncio.to_thread(_render_challenge, **kwargs)


async def render_unified_start_card(**kwargs) -> bytes:
    return await asyncio.to_thread(_render_unified_start, **kwargs)


async def render_leaderboard_card(**kwargs) -> bytes:
    return await asyncio.to_thread(_render_leaderboard, **kwargs)


async def render_userinfo_card(**kwargs) -> bytes:
    return await asyncio.to_thread(_render_userinfo, **kwargs)


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
    "userinfo": 0x9680E2,
    "leaderboard": 0x9680E2,
    "new_challenger": 0x5B9CFA,
}

_TIER_EMBED_COLOR = {
    "easy": 0x3DD68C,
    "medium": 0x38BDF8,
    "hard": 0xF07178,
    "ota_master": 0xE4C460,  # yellow/gold
    "special": 0x9B6DFF,  # purple
}

_TIER_CAPTION = {
    "easy": "🟢 **Easy**",
    "medium": "🔵 **Medium**",
    "hard": "🔴 **Hard**",
    "ota_master": "★ **Ota-Master**",
    "special": "◆ **Special**",
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
    """Optional embeds that set_image(attachment://…) for each file.

    Default (chrome=False, no color): return [] and let callers send plain
    image files. Discord still inlines PNGs; Embed+set_image wraps them in
    a nested box (visible in-channel, gone when you open the image).

    chrome=True (or color/colors set): one embed per file with a color strip.
    """
    import discord

    colored = color is not None or (
        colors is not None and any(c is not None for c in colors)
    )
    if not chrome and not colored:
        return []

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
        "cleared_tiers": ["easy"],
    },
    "scheduled": {
        "display_name": "Monty",
        "challenge_title": "Shows Sprint",
        "tier": "easy",
        "number": 2,
        "focus": "Frieren",
        "line_primary": "Queued to start",
        "line_secondary": "24 hours · Sun 29 Mar 09:00 -> Mon 30 Mar 09:00 UTC",
        "footer": "",
        "cleared_tiers": ["easy"],
    },
    "active": {
        "display_name": "Monty",
        "challenge_title": "VN 30k",
        "tier": "easy",
        "number": 4,
        "focus": "Steins;Gate",
        "line_primary": "In progress",
        "line_secondary": "12.5k / 30k characters",
        "time_left": "4h 12m left",
        "footer": "",
        "cleared_tiers": ["easy"],
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
        "cleared_tiers": ["easy"],
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
        "cleared_tiers": ["easy", "medium"],
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
        "cleared_tiers": ["easy"],
    },
    "expired": {
        "display_name": "Monty",
        "challenge_title": "Shows 6h",
        "tier": "medium",
        "number": 2,
        "focus": "Dungeon Meshi",
        "line_primary": "Time's up",
        "line_secondary": "Window closed",
        "footer": "",
        "cleared_tiers": ["easy", "medium"],
    },
    "status": {
        "display_name": "PatChatGC",
        "challenge_title": "12-Hour Immersion (24 Hours)",
        "tier": "ota_master",
        "number": 4,
        "focus": "",
        "line_primary": "Halfway",
        "line_secondary": (
            "12 / 24 hours · honor · still running · confirm when finished"
        ),
        "time_left": "11h 59m left",
        "footer": "",
        "cleared_tiers": ["easy", "medium", "hard", "ota_master"],
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
        "scheduled": "shows",
        "active": "vn",
        "complete": "vn",
        "fail": "manga",
        "abandon": "youtube",
        "expired": "shows",
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
    ui = await render_userinfo_card(
        display_name=display_name or "Monty",
        avatar=av,
        points=16,
        clears=6,
        rank=1,
        ranked=8,
        best_tier="ota_master",
        by_tier={"easy": 2, "medium": 1, "hard": 2, "ota_master": 1},
        fails=1,
        tadoku_linked=True,
        cleared_tiers=["easy"],
        active=[
            {
                "title": "VN 50k",
                "tier": "medium",
                "number": 3,
                "status": "active",
            },
            {
                "title": "Shows Sprint",
                "tier": "easy",
                "number": 2,
                "status": "scheduled",
            },
        ],
        completed=[
            {
                "title": "Manga Mini-Marathon",
                "tier": "easy",
                "number": 1,
            },
            {
                "title": "VN 30k",
                "tier": "easy",
                "number": 4,
            },
            {
                "title": "Shows Sprint",
                "tier": "medium",
                "number": 2,
            },
            {
                "title": "Hard Mode Reading",
                "tier": "hard",
                "number": 1,
            },
            {
                "title": "Immersion (24 Hours)",
                "tier": "hard",
                "number": 3,
            },
            {
                "title": "12-Hour Immersion (24 Hours)",
                "tier": "ota_master",
                "number": 1,
            },
        ],
    )
    out.append(("userinfo", ui))
    lb = await render_leaderboard_card(
        title="Challenge board",
        entries=[
            {
                "rank": 1,
                "name": "Alice",
                "clears": 12,
                "points": 28,
                "fails": 1,
                "avatar": demo_avatar_png((214, 175, 84)),
                "best_tier": "ota_master",
                "cleared_tiers": ["easy", "medium", "hard", "ota_master"],
            },
            {
                "rank": 2,
                "name": "Bob",
                "clears": 7,
                "points": 18,
                "fails": 0,
                "avatar": demo_avatar_png((183, 189, 201)),
                "best_tier": "hard",
                "cleared_tiers": ["easy", "medium", "hard"],
            },
            {
                "rank": 3,
                "name": "Cara",
                "clears": 5,
                "points": 9,
                "fails": 2,
                "avatar": demo_avatar_png((200, 143, 92)),
                "best_tier": "medium",
                "cleared_tiers": ["easy", "medium"],
            },
            {
                "rank": 4,
                "name": "Dan",
                "clears": 3,
                "points": 3,
                "fails": 0,
                "avatar": None,
                "best_tier": "easy",
                "cleared_tiers": ["easy"],
            },
            {
                "rank": 5,
                "name": "Eve",
                "clears": 1,
                "points": 1,
                "fails": 4,
                "avatar": None,
                "best_tier": None,
            },
        ],
        footer="5 ranked · by points",
    )
    out.append(("leaderboard", lb))
    return out
