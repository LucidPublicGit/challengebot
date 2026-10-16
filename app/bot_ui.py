"""Pure presentation helpers for Discord's bounded menus and embeds."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone


DURATION_LABELS = {
    "daily": "Daily", "monthly": "Monthly", "yearly": "Yearly", "special": "Special",
}


def duration_group(ch: dict) -> str:
    return str(ch.get("duration_group") or (
        "special" if ch.get("tier") == "special" else "daily"
    )).lower()


def duration_label(ch: dict) -> str:
    group = duration_group(ch)
    return DURATION_LABELS.get(group, group.title())


def timestamp(value: str | datetime | None) -> str:
    if not value:
        return "date unavailable"
    try:
        dt = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return f"<t:{int(dt.timestamp())}:F>"
    except (ValueError, TypeError):
        return "date unavailable"


def section_pages(
    challenges: list[dict], render: Callable[[dict], str], *,
    duration: str = "all", max_chars: int = 3500, max_options: int = 25,
) -> list[tuple[list[dict], str]]:
    """Keep every entry and every description character; repeat headings on pages.

    A single very long entry continues onto another page, retaining its selector.
    The budget leaves room for the view's intro and footer.
    """
    groups = list(DURATION_LABELS)
    groups.extend(sorted({duration_group(c) for c in challenges} - set(groups)))
    ordered = [c for group in groups for c in challenges
               if duration_group(c) == group and duration in ("all", group)]
    pages: list[tuple[list[dict], str]] = []
    rows: list[dict] = []
    body = ""
    previous = None
    for ch in ordered:
        group = duration_group(ch)
        remaining = render(ch)
        while remaining:
            heading = f"**{duration_label(ch)}**\n" if previous != group else ""
            prefix = ("\n\n" if body else "") + heading
            room = max_chars - len(body) - len(prefix)
            if len(rows) >= max_options or room < min(len(remaining), 150):
                pages.append((rows, body))
                rows, body, previous = [], "", None
                continue
            piece, remaining = remaining[:room], remaining[room:]
            rows.append(ch)
            body += prefix + piece
            previous = group
            if remaining:
                pages.append((rows, body))
                rows, body, previous = [], "", None
    if rows:
        pages.append((rows, body))
    return pages or [([], "No challenges in this view.")]


def point_explanation(row: dict) -> str:
    """Explain service-provided award values without reconstructing old policies."""
    effective = row.get("effective_points")
    original = row.get("original_points")
    if effective is None:
        return "Award details unavailable"
    text = f"{effective} effective pts"
    if original is not None and original != effective:
        text += f" (originally {original}; adjusted {effective - original:+})"
    return text
