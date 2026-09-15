"""Random "new challenger" banner with the user's avatar in the cutout hole.

Templates live in NEW_CHALLENGER_DIR (default ./new_challenger). JPEG exports often
bake transparency as a gray checkerboard; we detect that region and composite the
avatar behind the art.
"""

from __future__ import annotations

import io
import logging
import os
import random
from collections import deque
from pathlib import Path
from typing import Optional

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

logger = logging.getLogger(__name__)

_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp"}


def new_challenger_root() -> Path:
    raw = (os.environ.get("NEW_CHALLENGER_DIR") or "./new_challenger").strip()
    return Path(raw)


def list_templates() -> list[Path]:
    root = new_challenger_root()
    if not root.is_dir():
        return []
    return [
        p
        for p in sorted(root.iterdir())
        if p.is_file() and p.suffix.lower() in _IMAGE_EXT
    ]


def pick_template(rng: random.Random | None = None) -> Path | None:
    paths = list_templates()
    if not paths:
        return None
    return (rng or random).choice(paths)


def _hole_mask(im: Image.Image) -> tuple[Image.Image, tuple[int, int, int, int]]:
    """Return (L mask 255=hole, bbox). Pure Pillow — no numpy."""
    rgb = im.convert("RGB")
    w, h = rgb.size
    px = rgb.load()
    # Candidate map: gray + busy neighborhood (checkerboard hole)
    cand = [[False] * w for _ in range(h)]
    # Precompute luminance
    lum = [[0.0] * w for _ in range(h)]
    for y in range(h):
        for x in range(w):
            r, g, b = px[x, y]
            lum[y][x] = (r + g + b) / 3.0

    y0b, y1b = int(h * 0.12), int(h * 0.88)
    x0b = int(w * 0.42)
    for y in range(y0b, y1b):
        for x in range(x0b, w):
            r, g, b = px[x, y]
            chroma = abs(r - g) + abs(g - b) + abs(r - b)
            if chroma >= 40:
                continue
            L = lum[y][x]
            if not (140 < L < 240):
                continue
            # local range 3x3
            lo, hi = L, L
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    ny, nx = y + dy, x + dx
                    if 0 <= ny < h and 0 <= nx < w:
                        v = lum[ny][nx]
                        if v < lo:
                            lo = v
                        if v > hi:
                            hi = v
            if hi - lo <= 25:
                continue
            if L > 245 and hi - lo < 15:
                continue
            cand[y][x] = True

    # Largest 4-connected component
    visited = [[False] * w for _ in range(h)]
    best: list[tuple[int, int]] = []
    for y in range(h):
        for x in range(w):
            if not cand[y][x] or visited[y][x]:
                continue
            q = deque([(y, x)])
            visited[y][x] = True
            comp: list[tuple[int, int]] = []
            while q:
                cy, cx = q.popleft()
                comp.append((cx, cy))
                for ny, nx in (
                    (cy - 1, cx),
                    (cy + 1, cx),
                    (cy, cx - 1),
                    (cy, cx + 1),
                ):
                    if (
                        0 <= ny < h
                        and 0 <= nx < w
                        and cand[ny][nx]
                        and not visited[ny][nx]
                    ):
                        visited[ny][nx] = True
                        q.append((ny, nx))
            if len(comp) > len(best):
                best = comp
    if not best:
        raise ValueError("no hole region found in template")

    mask = Image.new("L", (w, h), 0)
    mp = mask.load()
    for x, y in best:
        mp[x, y] = 255
    # Close gaps — checkerboard detect often leaves a hollow ring (smooth gray center)
    mask = mask.filter(ImageFilter.MaxFilter(15))
    mask = mask.filter(ImageFilter.MinFilter(11))
    mask = mask.filter(ImageFilter.MaxFilter(9))
    bbox = mask.getbbox()
    if not bbox:
        raise ValueError("empty hole bbox")
    # Fill enclosed voids (center of hole missed by busy-neighborhood test)
    cx = (bbox[0] + bbox[2]) // 2
    cy = (bbox[1] + bbox[3]) // 2
    if mask.getpixel((cx, cy)) < 128:
        ImageDraw.floodfill(mask, (cx, cy), 255)
    # Soft ellipse union so avatar always has a solid disk to fill
    ell = Image.new("L", (w, h), 0)
    pad = max(4, min(bbox[2] - bbox[0], bbox[3] - bbox[1]) // 12)
    ImageDraw.Draw(ell).ellipse(
        (bbox[0] + pad, bbox[1] + pad, bbox[2] - pad, bbox[3] - pad),
        fill=255,
    )
    mask = ImageChops.lighter(mask, ell)
    bbox = mask.getbbox() or bbox
    return mask, bbox


def _avatar_image(
    avatar_bytes: Optional[bytes],
    size: int,
    *,
    circular: bool = True,
) -> Image.Image:
    src: Image.Image | None
    if avatar_bytes:
        try:
            src = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")
        except Exception:  # noqa: BLE001
            src = None
    else:
        src = None
    if src is None:
        src = Image.new("RGBA", (size, size), (60, 64, 80, 255))
        d = ImageDraw.Draw(src)
        d.ellipse((4, 4, size - 5, size - 5), fill=(120, 128, 160, 255))
    else:
        # Cover-fit square (no letterbox) so hole never shows empty corners
        w, h = src.size
        side0 = max(w, h, 1)
        canvas = Image.new("RGBA", (side0, side0), (40, 42, 48, 255))
        canvas.paste(src, ((side0 - w) // 2, (side0 - h) // 2), src)
        src = canvas.resize((size, size), Image.Resampling.LANCZOS)
    if not circular:
        return src
    circ = Image.new("L", (size, size), 0)
    ImageDraw.Draw(circ).ellipse((0, 0, size - 1, size - 1), fill=255)
    # Soft edge — hard circle looks blocky against jagged hole art
    circ = circ.filter(ImageFilter.GaussianBlur(max(1, size // 64)))
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(src, (0, 0), circ)
    return out


def _crop_white_letterbox(im: Image.Image, thr: int = 242) -> Image.Image:
    """Strip near-white letterbox bars (templates are often square-padded rectangles)."""
    rgb = im.convert("RGB")
    w, h = rgb.size
    px = rgb.load()

    def mostly_white_row(y: int) -> bool:
        step = max(1, w // 64)
        n = total = 0
        for x in range(0, w, step):
            r, g, b = px[x, y]
            total += 1
            if r >= thr and g >= thr and b >= thr:
                n += 1
        return total > 0 and n / total >= 0.90

    def mostly_white_col(x: int) -> bool:
        step = max(1, h // 64)
        n = total = 0
        for y in range(0, h, step):
            r, g, b = px[x, y]
            total += 1
            if r >= thr and g >= thr and b >= thr:
                n += 1
        return total > 0 and n / total >= 0.90

    y0 = 0
    while y0 < h - 1 and mostly_white_row(y0):
        y0 += 1
    y1 = h - 1
    while y1 > y0 and mostly_white_row(y1):
        y1 -= 1
    x0 = 0
    while x0 < w - 1 and mostly_white_col(x0):
        x0 += 1
    x1 = w - 1
    while x1 > x0 and mostly_white_col(x1):
        x1 -= 1
    # pad 1px so edges aren't harsh
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(w - 1, x1), min(h - 1, y1)
    if x1 - x0 < w * 0.4 or y1 - y0 < h * 0.3:
        return im  # safety: don't nuke the image
    return im.crop((x0, y0, x1 + 1, y1 + 1))


def _append_tier_strip(canvas: Image.Image, tier: str) -> Image.Image:
    """Dark tier bar under the cropped banner (not over letterbox)."""
    try:
        from app.challenge_card import ACCENT_GOLD, TIER_COLOR, TIER_LABEL
    except Exception:  # noqa: BLE001
        return canvas

    tier_key = (tier or "").lower().strip()
    tc = TIER_COLOR.get(tier_key, (150, 128, 226))
    if tier_key == "ota_master":
        tc = ACCENT_GOLD
    w, h = canvas.size
    bar_h = max(22, h // 14)
    out = Image.new("RGBA", (w, h + bar_h), (12, 10, 14, 255))
    out.paste(canvas, (0, 0), canvas if canvas.mode == "RGBA" else None)
    draw = ImageDraw.Draw(out)
    draw.rectangle((0, h, w, h + bar_h), fill=(12, 10, 14, 255))
    draw.rectangle((0, h, w, h + 3), fill=(*tc, 255))
    label = TIER_LABEL.get(tier_key, tier_key.replace("_", " ").title())
    if tier_key == "ota_master":
        label = "★ OTA-MASTER"
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/segoeui.ttf", max(12, bar_h - 8))
    except OSError:
        try:
            font = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                max(11, bar_h - 8),
            )
        except OSError:
            font = ImageFont.load_default()
    # center tier label
    tw = draw.textlength(label.upper(), font=font)
    draw.text(
        ((w - tw) / 2, h + (bar_h - font.size) / 2),
        label.upper(),
        fill=(*tc, 255),
        font=font,
    )
    return out


def render_new_challenger(
    avatar_bytes: Optional[bytes] = None,
    *,
    template: Path | None = None,
    rng: random.Random | None = None,
    tier: str | None = None,
) -> bytes | None:
    """Composite avatar into a random (or given) template; return PNG bytes.

    Crops baked-in white letterbox so the banner stays a true rectangle.
    Optional tier bar is appended under the art (centered label).
    """
    path = template or pick_template(rng)
    if path is None:
        logger.debug("new_challenger: no templates in %s", new_challenger_root())
        return None
    try:
        base_rgb = Image.open(path).convert("RGB")
        hole, bbox = _hole_mask(base_rgb)
    except Exception as exc:  # noqa: BLE001
        logger.warning("new_challenger template failed (%s): %s", path, exc)
        return None

    x0, y0, x1, y1 = bbox
    bw, bh = max(1, x1 - x0), max(1, y1 - y0)
    # Fill the whole hole bbox (not a smaller circle) so checkerboard never shows
    side = max(bw, bh, 64)
    # Slight overscale so jagged hole edges still sample avatar pixels
    side = max(side, int(max(bw, bh) * 1.12))
    av = _avatar_image(avatar_bytes, side, circular=False)

    # Soft hole mask — binary Max/Min edges read as blocky pixels
    hole_bin = hole.point(lambda p: 255 if p > 128 else 0)
    feather = max(2, min(bw, bh) // 40)
    hole_soft = hole_bin.filter(ImageFilter.GaussianBlur(feather))

    # Kill template checkerboard inside hole first (solid underfill)
    under = Image.new("RGBA", base_rgb.size, (0, 0, 0, 0))
    # average-ish dark from avatar center
    sample = av.resize((1, 1), Image.Resampling.BOX).getpixel((0, 0))
    fill_rgb = sample[:3] if isinstance(sample, tuple) else (40, 42, 48)
    under_solid = Image.new("RGBA", base_rgb.size, (*fill_rgb, 255))
    under.paste(under_solid, (0, 0), hole_soft)

    ax = x0 + (bw - side) // 2
    ay = y0 + (bh - side) // 2
    av_layer = Image.new("RGBA", base_rgb.size, (0, 0, 0, 0))
    av_layer.paste(av, (ax, ay))
    # Soft hole alpha only — square avatar fill, no circle crop leftovers
    av_layer.putalpha(hole_soft)

    canvas = Image.new("RGBA", base_rgb.size, (0, 0, 0, 255))
    canvas.paste(base_rgb, (0, 0))
    canvas = Image.alpha_composite(canvas, under)
    canvas = Image.alpha_composite(canvas, av_layer)

    # Template art on top everywhere except the (soft) hole
    overlay = base_rgb.convert("RGBA")
    inv = ImageChops.invert(hole_soft)
    overlay.putalpha(inv)
    canvas = Image.alpha_composite(canvas, overlay)

    # Drop square white pads — keep native rectangle
    canvas = _crop_white_letterbox(canvas)

    if tier:
        try:
            canvas = _append_tier_strip(canvas, tier)
        except Exception as exc:  # noqa: BLE001
            logger.debug("new_challenger tier strip skipped: %s", exc)

    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    return buf.getvalue()


def as_discord_new_challenger(png: bytes, filename: str = "new-challenger.png"):
    import discord

    return discord.File(io.BytesIO(png), filename=filename)
