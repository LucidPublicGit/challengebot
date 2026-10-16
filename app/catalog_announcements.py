"""Plain catalog exports and reusable catalog-change announcements."""

from pathlib import Path

from app.catalog import needs_focus_param, reward_points


IMAGE_DIR = Path(__file__).resolve().parent / "images" / "announcements"
CHANGE_LABELS = {
    "new": "New challenges",
    "updated": "Updated challenges",
    "retired": "Retired challenges",
}


def _window_label(c: dict) -> str:
    group = c.get('duration_group') or ('special' if c.get('tier') == 'special' else 'daily')
    if group == 'weekly':
        return 'Weekly · seven days from actual start (UTC)'
    if group == 'monthly':
        return 'Monthly · one personal UTC calendar month (anniversary clamped)'
    if group == 'yearly':
        return 'Yearly · one personal UTC calendar year (anniversary clamped)'
    return f"{group.title()} · {c['time_limit_hours']:g}h"


def catalog_dump(items: list[dict]) -> str:
    blocks = []
    for c in items:
        lines = [
            f"{c['year']} | {c.get('category') or 'ota'} | {c['tier']} "
            f"#{c['number']} | {c['title']}",
            f"ID: {c['id']} | Identity: {c.get('identity_id') or c['id']} | Edition: {c.get('edition_id') or c['id']}",
            f"State: {c.get('lifecycle_state') or 'active'} | Repeat: {c.get('repeat_policy') or 'legacy'} | "
            f"Scoring: {'inherited formula' if c.get('points') is None else 'absolute override'}",
            str(c.get("description") or ""),
            f"Goal: {c['goal_amount']:g} {c['goal_kind']} | "
            f"Window: {_window_label(c)} | "
            f"Points: {reward_points(c)} | "
            f"Verify: {c.get('verify') or 'tadoku'} | "
            f"Media: {c.get('media_filter') or 'any'}",
            f"Focus: {c.get('fixed_param') or c.get('param_label') or 'none'} | "
            f"Concurrent: {'yes' if c.get('allows_concurrent') or c.get('duration_group') in ('weekly', 'monthly', 'yearly') else 'no'}",
            f"Published: {c.get('published_at') or 'unspecified'} | Available: {c.get('available_from') or 'unbounded'} to {c.get('available_until') or 'unbounded'} (exclusive) | Retired: {c.get('retired_at') or 'no'}",
        ]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) or "No challenges found."


def text_chunks(text: str, limit: int = 1900) -> list[str]:
    """Prefer line boundaries; retain every character, even on oversized lines."""
    chunks = []
    while len(text) > limit:
        end = text.rfind("\n", 0, limit)
        end = end + 1 if end >= 0 else limit
        chunks.append(text[:end])
        text = text[end:]
    if text:
        chunks.append(text)
    return chunks


def announcement_text(items: list[dict], change: str, message: str = "") -> str:
    blocks = [message] if message else []
    for c in items:
        title = f"{c['tier'].replace('_', '-')} #{c['number']}: {c['title']}"
        lines = [title, str(c.get("description") or "")]
        if change != "retired":
            lines.append(
                f"Goal: {c['goal_amount']:g} {c['goal_kind']} · "
                f"{_window_label(c)} · {reward_points(c)} points · "
                f"repeat {c.get('repeat_policy') or 'legacy'} · "
                f"{'inherited formula' if c.get('points') is None else 'absolute override'}"
            )
            if needs_focus_param(c):
                lines.append(f"Choose your {c['param_label']} when starting.")
        blocks.append("\n".join(lines))
    blocks.append(
        "These challenges are no longer available to start."
        if change == "retired"
        else "Browse with /challenge list · Join with /challenge start"
    )
    return "\n\n".join(blocks)
