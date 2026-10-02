"""Plain catalog exports and reusable catalog-change announcements."""

from pathlib import Path

from app.catalog import needs_focus_param, reward_points


IMAGE_DIR = Path(__file__).resolve().parent / "images" / "announcements"
CHANGE_LABELS = {
    "new": "New challenges",
    "updated": "Updated challenges",
    "retired": "Retired challenges",
}


def catalog_dump(items: list[dict]) -> str:
    blocks = []
    for c in items:
        lines = [
            f"{c['year']} | {c.get('category') or 'ota'} | {c['tier']} "
            f"#{c['number']} | {c['title']}",
            f"ID: {c['id']}",
            str(c.get("description") or ""),
            f"Goal: {c['goal_amount']:g} {c['goal_kind']} | "
            f"Time: {c['time_limit_hours']:g}h | "
            f"Points: {reward_points(c)} | "
            f"Verify: {c.get('verify') or 'tadoku'} | "
            f"Media: {c.get('media_filter') or 'any'}",
            f"Focus: {c.get('fixed_param') or c.get('param_label') or 'none'} | "
            f"Concurrent: {'yes' if c.get('allows_concurrent') else 'no'}",
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
                f"{c['time_limit_hours']:g}h · {reward_points(c)} points"
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
