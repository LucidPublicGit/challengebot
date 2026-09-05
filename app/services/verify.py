from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
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


def _param_match(log: dict, param: Optional[str]) -> bool:
    if not param or not str(param).strip():
        return True
    needle = str(param).strip().lower()
    blob = " ".join(
        [
            _description(log).lower(),
            " ".join(_tags(log)),
            str(log.get("series") or "").lower(),
        ]
    )
    return needle in blob


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
    pages_per_volume: float = 180.0,
    episode_minutes: float = 24.0,
) -> VerifyResult:
    kind = (goal_kind or "").lower()
    target = float(goal_amount)
    filtered = [
        log
        for log in logs
        if _media_match(log, media_filter) and _param_match(log, param)
    ]
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
