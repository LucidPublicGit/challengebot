"""Render real achievement cards using an isolated demo database."""

import asyncio
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.catalog import list_challenges, upsert_challenges_from_yaml
from app.challenge_card import (
    ACHIEVEMENT_LIST_PAGE_SIZE, demo_avatar_png, render_achievement_list, render_userinfo_card,
)
from app.db import db_session, init_db
from app.services import achievements as ach
from app.services import attempts as att

OUT = ROOT / "design-previews"


async def render_demo(user, label):
    entries = ach.for_user(user)
    swept = att.completed_tiers(user)
    standing = att.leaderboard_standing(user)
    completed = att.list_attempts(discord_id=user, status="completed", limit=100)
    # Keep the demo focused on the new section, rather than a 30-row clear history.
    completed = completed[:3]
    png = await render_userinfo_card(
        display_name="Monty", avatar=demo_avatar_png(), tadoku_linked=True,
        points=att.challenge_points(user), clears=att.completion_counts(user)["total"],
        rank=standing["rank"], ranked=standing["ranked"],
        best_tier=swept[-1] if swept else None, cleared_tiers=swept,
        categories=att.category_progress(user), achievements=entries, completed=completed,
    )
    (OUT / f"achievements-{label}-usercard.png").write_bytes(png)
    n = ACHIEVEMENT_LIST_PAGE_SIZE
    for page in range((len(entries) + n - 1) // n):
        png = await render_achievement_list(display_name="Monty", achievements=entries, page=page)
        (OUT / f"achievements-{label}-list-{page + 1}.png").write_bytes(png)


async def main():
    OUT.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as directory:
        init_db(str(Path(directory) / "demo.db"))
        upsert_challenges_from_yaml(ROOT / "challenges" / "2026.yaml")
        for user, challenges in (
            ("one", [c for c in list_challenges() if c["id"] == "easy-1"]),
            ("all", list_challenges()),
        ):
            att.ensure_user(user)
            for index, challenge in enumerate(challenges):
                start = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=index)
                with db_session() as conn:
                    aid = conn.execute(
                        "INSERT INTO attempts(discord_id,challenge_id,status,started_at,ends_at) "
                        "VALUES (?,?,'active',?,?)",
                        (user, challenge["id"], start.isoformat(), (start + timedelta(days=1)).isoformat()),
                    ).lastrowid
                result = att.admin_update_attempt(
                    aid, status="completed", completed_at=(start + timedelta(hours=12)).isoformat(),
                )
                assert result["ok"], result
            await render_demo(user, user)
    print(f"Rendered live-code user cards and list pages to {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
