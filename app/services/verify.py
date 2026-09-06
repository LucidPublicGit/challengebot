from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

_EP_RE = re.compile(r"[Ss]?\d{0,2}[Ee](?P<ep>\d{1,3})")
_VOL_RE = re.compile(
    r"(?:Vol\.?|Volume|v|第)\s*(?P<vol>\d{1,4})",
    re.I,
)

_CHAR_UNITS = {"character", "characters", "char", "chars"}
_PAGE_UNITS = {
    "page",
    "pages",
    "comic page",
    "comic pages",
    "comic_page",
    "comic_pages",
    "2 column page",
    "two_column_pages",
}
_MINUTE_UNITS = {
    "minute",
    "minutes",
    "min",
    "minute (high density)",
    "minutes_high_density",
    "minute_high_density",
}
_VOLUME_UNITS = {"volume", "volumes", "vol", "vols"}
_EPISODE_UNITS = {"episode", "episodes", "ep", "eps"}


def _tags(log: dict) -> set[str]:
    raw = log.get("tags") or []
    if isinstance(raw, str):
        raw = [raw]
    out = set()
    for t in raw:
        if isinstance(t, dict):
            name = t.get("name") or t.get("tag") or ""
        else:
            name = str(t)
        out.add(name.strip().lower())
    return out


def _unit_name(log: dict) -> str:
    u = log.get("unit") or log.get("unit_name") or ""
    if isinstance(u, dict):
        u = u.get("name") or u.get("label") or ""
    return str(u).strip().lower()


def _activity_name(log: dict) -> str:
    a = log.get("activity") or log.get("activity_name") or log.get("log_activity") or ""
    if isinstance(a, dict):
        a = a.get("name") or ""
    # sometimes activity_id only
    return str(a).strip().lower()


def _amount(log: dict) -> float:
    try:
        return float(log.get("amount") or 0)
    except (TypeError, ValueError):
        return 0.0


def _duration_minutes(log: dict) -> float:
    """Prefer duration_seconds; else amount if unit is minutes."""
    ds = log.get("duration_seconds")
    if ds is not None:
        try:
            return float(ds) / 60.0
        except (TypeError, ValueError):
            pass
    unit = _unit_name(log)
    if unit in _MINUTE_UNITS:
        return _amount(log)
    return 0.0


def _description(log: dict) -> str:
    return str(log.get("description") or log.get("title") or "")


def parse_content_items(raw: str | None, *, limit: int = 12) -> list[str]:
    """Split content into one work per entry (newlines preferred, commas OK)."""
    if not raw or not str(raw).strip():
        return []
    text = str(raw).replace("\r\n", "\n").replace("\r", "\n")
    parts: list[str] = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        if line[:1] in "-•*" and len(line) > 1:
            line = line[1:].strip()
        if re.match(r"^\d+[.)]\s+", line):
            line = re.sub(r"^\d+[.)]\s+", "", line).strip()
        if "," in line:
            bits = [b.strip() for b in line.split(",") if b.strip()]
            if len(bits) >= 2 and all(len(b) <= 60 for b in bits):
                parts.extend(bits)
                continue
        parts.append(line)
    out: list[str] = []
    seen: set[str] = set()
    for p in parts:
        key = p.lower()
        if key in seen:
            continue
        seen.add(key)
        label = p.strip()
        if len(label) > 80:
            label = label[:77] + "…"
        out.append(label)
        if len(out) >= limit:
            break
    return out


def format_content_items(raw: str | None) -> str | None:
    """Canonical multi-work storage: one title per line."""
    items = parse_content_items(raw)
    if not items:
        return None
    return "\n".join(items)


def content_labels_from_logs(
    logs: list[dict[str, Any]],
    matched_ids: list[str] | None = None,
    *,
    limit: int = 6,
) -> list[str]:
    """Unique work titles from matched Tadoku logs (description/series/title)."""
    id_set = {str(x) for x in (matched_ids or []) if x}
    chosen = logs
    if id_set:
        filtered = [log for log in logs if str(log.get("id") or "") in id_set]
        if filtered:
            chosen = filtered
    labels: list[str] = []
    seen: set[str] = set()
    for log in chosen:
        raw = (
            str(log.get("series") or "").strip()
            or _description(log).strip()
            or str(log.get("title") or "").strip()
        )
        if not raw:
            continue
        # Drop episode markers noise for display: keep short label
        label = raw.split("\n")[0].strip()
        if len(label) > 80:
            label = label[:77] + "…"
        key = label.lower()
        if key in seen:
            continue
        seen.add(key)
        labels.append(label)
        if len(labels) >= limit:
            break
    return labels


def _exclude_hit(label: str, exclude: list[str] | None) -> bool:
    if not exclude:
        return False
    low = label.lower()
    return any(ex.strip().lower() in low for ex in exclude if ex and str(ex).strip())


def resolve_content(
    *,
    param: Optional[str] = None,
    logs: list[dict[str, Any]] | None = None,
    matched_ids: list[str] | None = None,
    note: Optional[str] = None,
    content_override: Optional[str] = None,
    exclude: list[str] | None = None,
) -> Optional[str]:
    """Best-effort content label stored on completion."""
    if content_override and str(content_override).strip():
        return format_content_items(content_override)
    labels = [
        lb
        for lb in content_labels_from_logs(logs or [], matched_ids)
        if not _exclude_hit(lb, exclude)
    ]
    if labels:
        return format_content_items("\n".join(labels))
    if param and str(param).strip():
        return format_content_items(str(param).strip())
    if note and str(note).strip():
        return format_content_items(str(note).strip())
    return None


def strip_content_labels(content: str | None, remove: list[str]) -> str:
    """Drop labels that match any remove needle."""
    items = parse_content_items(content)
    if not items:
        return ""
    needles = [r.strip().lower() for r in remove if r and str(r).strip()]
    if not needles:
        return format_content_items("\n".join(items)) or ""
    kept = [
        label
        for label in items
        if not any(n in label.lower() for n in needles)
    ]
    return format_content_items("\n".join(kept)) or ""


def _media_match(log: dict, media_filter: str) -> bool:
    mf = (media_filter or "any").lower()
    if mf in ("any", "all", "*"):
        return True
    tags = _tags(log)
    act = _activity_name(log)
    desc = _description(log).lower()

    def has(*names: str) -> bool:
        return any(n in tags for n in names)

    if mf == "manga":
        return has("manga") or "manga" in desc
    if mf == "anime":
        return has("anime") or "anime" in desc
    if mf == "youtube":
        return has("youtube", "youtube:video") or "youtube" in desc
    if mf in ("vn", "visual_novel"):
        return has("vn", "visual novel", "visual_novel") or "vn" in tags
    if mf == "book":
        return has("book", "novel") or act == "reading"
    if mf == "reading":
        return act == "reading" or has(
            "book", "manga", "vn", "visual novel", "visual_novel"
        )
    return has(mf)


def _norm_match_key(s: str) -> str:
    """Lowercase + strip non-alnum so 'One Piece' ≈ 'OnePiecee' prefix-ish."""
    import unicodedata

    t = unicodedata.normalize("NFKC", s or "").lower()
    return re.sub(r"[^a-z0-9\u3040-\u30ff\u3400-\u9fff]+", "", t)


def _param_match(
    log: dict,
    param: Optional[str],
    *,
    aliases: list[str] | None = None,
) -> bool:
    needles_raw = []
    if param and str(param).strip():
        needles_raw.append(str(param).strip())
    for a in aliases or []:
        if a and str(a).strip():
            needles_raw.append(str(a).strip())
    if not needles_raw:
        return True
    blob = " ".join(
        [
            _description(log),
            " ".join(_tags(log)),
            str(log.get("series") or ""),
            str(log.get("title") or ""),
        ]
    )
    blob_l = blob.lower()
    blob_n = _norm_match_key(blob)
    for needle in needles_raw:
        n = needle.lower()
        if n and n in blob_l:
            return True
        nn = _norm_match_key(needle)
        if nn and len(nn) >= 3 and nn in blob_n:
            return True
        # one extra letter on needle (OnePiecee → onepiece)
        if nn and len(nn) >= 5 and nn[:-1] in blob_n:
            return True
    return False


@dataclass
class VerifyResult:
    ok: bool
    progress: float
    target: float
    unit_label: str
    detail: str
    matched_log_ids: list[str] = field(default_factory=list)
    method: str = "tadoku"


def evaluate_logs(
    logs: list[dict[str, Any]],
    *,
    goal_kind: str,
    goal_amount: float,
    media_filter: str,
    param: Optional[str] = None,
    exclude: list[str] | None = None,
    aliases: list[str] | None = None,
    include_log_ids: list[str] | None = None,
    pages_per_volume: float = 180.0,
    episode_minutes: float = 24.0,
) -> VerifyResult:
    kind = (goal_kind or "").lower()
    target = float(goal_amount)

    def _log_label_blob(log: dict) -> str:
        return " ".join(
            [
                _description(log),
                str(log.get("series") or ""),
                str(log.get("title") or ""),
            ]
        )

    include_ids = {str(x) for x in (include_log_ids or []) if x}
    filtered = []
    for log in logs:
        lid = str(log.get("id") or "")
        forced = bool(lid and lid in include_ids)
        if not _media_match(log, media_filter) and not forced:
            continue
        if not forced and not _param_match(log, param, aliases=aliases):
            continue
        if not forced and _exclude_hit(_log_label_blob(log), exclude):
            continue
        filtered.append(log)
    ids = [str(log.get("id") or "") for log in filtered if log.get("id")]

    if kind == "characters":
        total = 0.0
        used = []
        for log in filtered:
            if _unit_name(log) in _CHAR_UNITS or (
                _activity_name(log) == "reading" and _unit_name(log) in _CHAR_UNITS
            ):
                total += _amount(log)
                if log.get("id"):
                    used.append(str(log["id"]))
            elif _unit_name(log) in _CHAR_UNITS:
                total += _amount(log)
        # also accept reading + characters even if unit nested oddly
        if total == 0:
            for log in filtered:
                if _activity_name(log) == "reading" and _unit_name(log) in _CHAR_UNITS.union(
                    {""}
                ):
                    # only count if unit is chars
                    pass
            for log in filtered:
                u = _unit_name(log)
                if "char" in u:
                    total += _amount(log)
                    if log.get("id"):
                        used.append(str(log["id"]))
        ok = total >= target
        return VerifyResult(
            ok=ok,
            progress=total,
            target=target,
            unit_label="characters",
            detail=f"{total:,.0f} / {target:,.0f} characters",
            matched_log_ids=used or ids,
        )

    if kind == "minutes":
        total = sum(_duration_minutes(log) for log in filtered)
        ok = total >= target
        return VerifyResult(
            ok=ok,
            progress=total,
            target=target,
            unit_label="minutes",
            detail=f"{total:.1f} / {target:.0f} minutes",
            matched_log_ids=ids,
        )

    if kind == "immersion_hours":
        total_m = sum(_duration_minutes(log) for log in filtered)
        # reading chars do not convert by default (anything = time-based logs)
        hours = total_m / 60.0
        ok = hours >= target
        return VerifyResult(
            ok=ok,
            progress=hours,
            target=target,
            unit_label="hours",
            detail=f"{hours:.2f} / {target:.0f} hours (from timed logs)",
            matched_log_ids=ids,
        )

    if kind == "volumes":
        vols = 0.0
        used = []
        for log in filtered:
            u = _unit_name(log)
            amt = _amount(log)
            desc = _description(log)
            if u in _VOLUME_UNITS:
                vols += amt
                used.append(str(log.get("id") or ""))
                continue
            m = _VOL_RE.search(desc)
            if m and amt <= 1:
                # single volume mention counts 1 if amount is tiny/flag
                vols += 1.0
                used.append(str(log.get("id") or ""))
                continue
            if u in _PAGE_UNITS and pages_per_volume > 0:
                vols += amt / pages_per_volume
                used.append(str(log.get("id") or ""))
                continue
            if "char" in u and pages_per_volume > 0:
                # weak: skip chars for volume goals
                continue
        ok = vols >= target
        return VerifyResult(
            ok=ok,
            progress=vols,
            target=target,
            unit_label="volumes",
            detail=(
                f"{vols:.2f} / {target:.0f} volumes "
                f"(pages÷{pages_per_volume:g} or volume units)"
            ),
            matched_log_ids=[x for x in used if x] or ids,
        )

    if kind == "episodes":
        eps = 0.0
        seen_ep_keys: set[str] = set()
        used = []
        for log in filtered:
            u = _unit_name(log)
            amt = _amount(log)
            desc = _description(log)
            if u in _EPISODE_UNITS:
                eps += amt
                used.append(str(log.get("id") or ""))
                continue
            m = _EP_RE.search(desc)
            if m:
                key = desc.strip().lower()
                if key not in seen_ep_keys:
                    seen_ep_keys.add(key)
                    eps += 1.0
                    used.append(str(log.get("id") or ""))
                continue
            mins = _duration_minutes(log)
            if mins > 0 and episode_minutes > 0:
                eps += mins / episode_minutes
                used.append(str(log.get("id") or ""))
        ok = eps >= target
        return VerifyResult(
            ok=ok,
            progress=eps,
            target=target,
            unit_label="episodes",
            detail=(
                f"{eps:.2f} / {target:.0f} episodes "
                f"(E-markers, episode units, or minutes÷{episode_minutes:g})"
            ),
            matched_log_ids=[x for x in used if x] or ids,
        )

    if kind == "complete_work":
        return VerifyResult(
            ok=False,
            progress=0,
            target=target,
            unit_label="work",
            detail="This challenge is honor-based (cannot fully verify via Tadoku).",
            matched_log_ids=[],
            method="honor",
        )

    return VerifyResult(
        ok=False,
        progress=0,
        target=target,
        unit_label=kind,
        detail=f"Unknown goal_kind={kind}",
        matched_log_ids=[],
    )


def parse_iso(dt: str) -> datetime:
    s = dt.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def synthetic_logs_for_goal(
    *,
    goal_kind: str,
    goal_amount: float,
    media_filter: str = "any",
    param: str | None = None,
    meet_goal: bool = True,
    pages_per_volume: float = 180.0,
    episode_minutes: float = 24.0,
) -> list[dict[str, Any]]:
    """
    Build fake Tadoku-shaped logs for admin tests (no network, no real logs).
    meet_goal=False produces ~half target so verify should fail.
    """
    kind = (goal_kind or "").lower()
    target = float(goal_amount)
    amt = target if meet_goal else max(target * 0.4, 0.0)
    if kind not in ("complete_work", "immersion_hours") and amt == 0 and meet_goal:
        amt = target
    media = (media_filter or "any").lower()
    tag = {
        "manga": "manga",
        "anime": "anime",
        "youtube": "youtube",
        "vn": "vn",
        "visual_novel": "vn",
        "book": "book",
        "reading": "book",
        "any": "anime",
    }.get(media, media if media != "any" else "anime")
    title = (param or "Test Work").strip() or "Test Work"
    base = {
        "id": f"fake-{'pass' if meet_goal else 'fail'}-1",
        "tags": [tag],
        "description": title,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    if kind == "characters":
        return [
            {
                **base,
                "amount": amt,
                "unit": "characters",
                "activity": "reading",
            }
        ]
    if kind == "minutes":
        return [
            {
                **base,
                "duration_seconds": int(amt * 60),
                "amount": amt,
                "unit": "minutes",
                "activity": "listening",
            }
        ]
    if kind == "immersion_hours":
        mins = amt * 60.0
        return [
            {
                **base,
                "duration_seconds": int(mins * 60),
                "amount": mins,
                "unit": "minutes",
                "activity": "listening",
                "tags": ["anime", "youtube"],
            }
        ]
    if kind == "volumes":
        pages = amt * float(pages_per_volume or 180.0)
        return [
            {
                **base,
                "amount": pages,
                "unit": "comic pages",
                "activity": "reading",
                "tags": ["manga"],
                "description": f"{title} vol 1",
            }
        ]
    if kind == "episodes":
        # Prefer explicit episode units so we don't depend on description parsing alone
        return [
            {
                **base,
                "amount": amt,
                "unit": "episodes",
                "activity": "listening",
                "tags": ["anime"],
                "description": f"{title} S01E01",
                "duration_seconds": int(amt * float(episode_minutes or 24) * 60),
            }
        ]
    if kind == "complete_work":
        # Honor path — empty logs; complete_work never auto-passes via evaluate
        return []
    return [{**base, "amount": amt, "unit": "characters", "activity": "reading"}]
