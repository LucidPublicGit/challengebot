"""Download/sort congrats media into completion_pictures/.

Sources:
  - otakugifs.xyz reaction GIFs → root + tier pools
  - AniList covers + CLEARED badge → content/<slug>/ themed cards
"""

from __future__ import annotations

import hashlib
import io
import json
import time
from collections import Counter
from pathlib import Path
from urllib.request import Request, urlopen

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1] / "completion_pictures"
UA = "ChallengeTracker/1.0 (local immersion bot)"
MAX_B = 7_500_000
MIN_B = 5_000
TIER = ["easy", "medium", "hard", "ota_master"]

CELEB = [
    "celebrate",
    "cheers",
    "clap",
    "happy",
    "dance",
    "thumbsup",
    "smile",
    "wink",
    "yay",
    "yes",
    "brofist",
    "cool",
    "laugh",
    "wave",
    "hug",
    "love",
    "woah",
    "slowclap",
]

SERIES = [
    ("one-piece", "One Piece", "ANIME"),
    ("steins-gate", "Steins;Gate", "ANIME"),
    ("frieren", "Frieren", "ANIME"),
    ("my-hero-academia", "My Hero Academia", "ANIME"),
    ("one-punch-man", "One Punch Man", "ANIME"),
    ("attack-on-titan", "Attack on Titan", "ANIME"),
    ("naruto", "Naruto", "ANIME"),
    ("jujutsu-kaisen", "Jujutsu Kaisen", "ANIME"),
    ("demon-slayer", "Demon Slayer", "ANIME"),
    ("spy-x-family", "Spy x Family", "ANIME"),
    ("chainsaw-man", "Chainsaw Man", "ANIME"),
    ("death-note", "Death Note", "ANIME"),
    ("fullmetal-alchemist", "Fullmetal Alchemist", "ANIME"),
    ("hunter-x-hunter", "Hunter x Hunter", "ANIME"),
    ("bleach", "Bleach", "ANIME"),
    ("dragon-ball", "Dragon Ball", "ANIME"),
    ("gurren-lagann", "Gurren Lagann", "ANIME"),
    ("fate", "Fate/stay night", "ANIME"),
    ("haruhi", "The Melancholy of Haruhi Suzumiya", "ANIME"),
    ("lucky-star", "Lucky Star", "ANIME"),
    ("berserk", "Berserk", "MANGA"),
    ("dungeon-meshi", "Delicious in Dungeon", "ANIME"),
    ("bocchi-the-rock", "Bocchi the Rock", "ANIME"),
    ("oshi-no-ko", "Oshi no Ko", "ANIME"),
    ("clannad", "Clannad", "ANIME"),
    ("violet-evergarden", "Violet Evergarden", "ANIME"),
    ("cowboy-bebop", "Cowboy Bebop", "ANIME"),
    ("evangelion", "Neon Genesis Evangelion", "ANIME"),
    ("re-zero", "Re:Zero", "ANIME"),
    ("konosuba", "KonoSuba", "ANIME"),
    ("jojo", "JoJo's Bizarre Adventure", "ANIME"),
    ("vinland-saga", "Vinland Saga", "ANIME"),
    ("mob-psycho", "Mob Psycho 100", "ANIME"),
    ("solo-leveling", "Solo Leveling", "ANIME"),
]


def http_bytes(url: str, *, data: bytes | None = None, json_body: bool = False) -> bytes:
    headers = {"User-Agent": UA, "Accept": "*/*"}
    if json_body:
        headers["Content-Type"] = "application/json"
    req = Request(url, data=data, headers=headers, method="POST" if data else "GET")
    with urlopen(req, timeout=40) as r:
        return r.read()


def http_json(url: str, *, data: bytes | None = None) -> dict:
    raw = http_bytes(url, data=data, json_body=data is not None)
    return json.loads(raw.decode())


def has_hash(h: str) -> bool:
    return any(ROOT.rglob(f"*-{h}.*"))


def save(data: bytes, dest: Path) -> bool:
    if not (MIN_B <= len(data) <= MAX_B):
        return False
    h = hashlib.sha1(data).hexdigest()[:12]
    if has_hash(h):
        return False
    dest = dest.with_name(f"{dest.stem}-{h}{dest.suffix}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    print(f"  OK {dest.relative_to(ROOT)} ({len(data) // 1024}KB)")
    return True


def font(size: int, bold: bool = True):
    paths = [
        "C:/Windows/Fonts/segoeuib.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf",
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for p in paths:
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default()


def make_congrats(cover_bytes: bytes, title: str) -> bytes:
    cover = Image.open(io.BytesIO(cover_bytes)).convert("RGB")
    W, H = 480, 720
    cw, ch = cover.size
    scale = max(W / max(cw, 1), H / max(ch, 1))
    nw, nh = max(1, int(cw * scale)), max(1, int(ch * scale))
    cover = cover.resize((nw, nh), Image.Resampling.LANCZOS)
    left, top = (nw - W) // 2, (nh - H) // 2
    cover = cover.crop((left, top, left + W, top + H))
    base = cover.convert("RGBA")
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    for y in range(H // 2, H):
        a = int(200 * ((y - H // 2) / (H / 2)) ** 1.2)
        d.line([(0, y), (W, y)], fill=(10, 12, 18, a))
    for y in range(0, 90):
        a = int(120 * (1 - y / 90))
        d.line([(0, y), (W, y)], fill=(10, 12, 18, a))
    img = Image.alpha_composite(base, overlay)
    d = ImageDraw.Draw(img)
    badge = "CLEARED"
    bf = font(52, True)
    bbox = d.textbbox((0, 0), badge, font=bf)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pad_x, pad_y = 28, 14
    bx0 = (W - tw) // 2 - pad_x
    by0 = H - 140
    bx1 = bx0 + tw + pad_x * 2
    by1 = by0 + th + pad_y * 2
    d.rounded_rectangle((bx0, by0, bx1, by1), radius=18, fill=(61, 214, 140, 230))
    d.text(
        ((bx0 + bx1) / 2, (by0 + by1) / 2),
        badge,
        font=bf,
        fill=(20, 30, 28, 255),
        anchor="mm",
    )
    tf = font(22, True)
    t = " ".join(title.split())
    while d.textlength(t, font=tf) > W - 40 and len(t) > 4:
        t = t[:-2]
    if t != " ".join(title.split()):
        t = t.rstrip() + "…"
    d.text((W / 2, by0 - 18), t, font=tf, fill=(236, 237, 242, 255), anchor="mb")
    ff = font(14, False)
    d.text(
        (W / 2, H - 24),
        "Challenge complete",
        font=ff,
        fill=(180, 185, 200, 255),
        anchor="mm",
    )
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=88, optimize=True)
    return buf.getvalue()


def anilist_cover(title: str, media_type: str) -> tuple[str | None, str | None]:
    q = """
    query ($search: String, $type: MediaType) {
      Media(search: $search, type: $type, sort: SEARCH_MATCH) {
        title { romaji english }
        coverImage { extraLarge large }
      }
    }
    """
    body = json.dumps(
        {"query": q, "variables": {"search": title, "type": media_type}}
    ).encode()
    try:
        data = http_json("https://graphql.anilist.co", data=body)
    except Exception as exc:  # noqa: BLE001
        print(f"  anilist {title}: {exc}")
        return None, None
    media = (data.get("data") or {}).get("Media") or {}
    cover = media.get("coverImage") or {}
    url = cover.get("extraLarge") or cover.get("large")
    t = media.get("title") or {}
    name = t.get("english") or t.get("romaji") or title
    return url, name


def fetch_otaku(count_per: int = 4) -> int:
    print("=== otakugifs celebratory ===")
    saved = 0
    n = 0
    for reaction in CELEB:
        for i in range(count_per):
            try:
                meta = http_json(f"https://api.otakugifs.xyz/gif?reaction={reaction}")
            except Exception as exc:  # noqa: BLE001
                print(f"  api {reaction}: {exc}")
                time.sleep(0.4)
                continue
            url = meta.get("url") or ""
            if not url:
                continue
            try:
                data = http_bytes(url)
            except Exception as exc:  # noqa: BLE001
                print(f"  dl {exc}")
                continue
            time.sleep(0.12)
            if n % 5 == 0:
                dest = ROOT / f"otaku-{reaction}-{i}.gif"
            else:
                dest = ROOT / TIER[n % len(TIER)] / f"otaku-{reaction}-{i}.gif"
            if save(data, dest):
                saved += 1
            n += 1
        print(f"  {reaction} done")
    return saved


def fetch_series_cards() -> int:
    print("=== series CLEARED cards (AniList covers) ===")
    made = 0
    for slug, title, mtype in SERIES:
        dest_dir = ROOT / "content" / slug
        dest_dir.mkdir(parents=True, exist_ok=True)
        if list(dest_dir.glob("cleared-*.jpg")):
            print(f"  {slug}: exists")
            continue
        url, name = anilist_cover(title, mtype)
        time.sleep(0.3)
        if not url:
            print(f"  {slug}: no cover")
            continue
        try:
            raw = http_bytes(url)
            out = make_congrats(raw, name or title)
        except Exception as exc:  # noqa: BLE001
            print(f"  {slug}: {exc}")
            continue
        h = hashlib.sha1(out).hexdigest()[:10]
        path = dest_dir / f"cleared-{slug}-{h}.jpg"
        path.write_bytes(out)
        print(f"  OK {path.relative_to(ROOT)} ({len(out) // 1024}KB) ← {name}")
        made += 1
    return made


def summarize() -> None:
    c: Counter[str] = Counter()
    total = 0
    nbytes = 0
    for p in ROOT.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in {
            ".gif",
            ".png",
            ".jpg",
            ".jpeg",
            ".webp",
        }:
            continue
        total += 1
        nbytes += p.stat().st_size
        rel = p.relative_to(ROOT)
        if rel.parts[0] == "content" and len(rel.parts) > 1:
            c[f"content/{rel.parts[1]}"] += 1
        elif rel.parts[0] in TIER:
            c[rel.parts[0]] += 1
        else:
            c["_root"] += 1
    print(f"\nLibrary: {total} files, {nbytes // 1024 // 1024}MB")
    for k, v in sorted(c.items(), key=lambda x: (-x[1], x[0])):
        print(f"  {k}: {v}")


def main() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    for t in TIER:
        (ROOT / t).mkdir(exist_ok=True)
    (ROOT / "content").mkdir(exist_ok=True)
    n1 = fetch_otaku(count_per=3)
    n2 = fetch_series_cards()
    print(f"\nNew otakugifs: {n1}, series cards: {n2}")
    summarize()


if __name__ == "__main__":
    main()
