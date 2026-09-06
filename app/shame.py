"""Light-roast lines for failed / expired challenge posts."""

from __future__ import annotations

import random

# First loss (or check fail with a clean record)
_SINGLE = (
    "The goal waved. You nodded politely and left.",
    "Close enough is a lifestyle, apparently.",
    "Tadoku has seen things. Tadoku is disappointed.",
    "The clock won. It always believed in itself.",
    "You didn't fail — you speedran the L.",
    "Immersion incomplete. Shame fully rendered.",
    "Plot twist: the challenge was optional. Your dignity wasn't.",
    "Logged hours: optimistic. Logged results: interpretive dance.",
)

# 2–3 lifetime losses on record
_MULTI = (
    "Fail #{n}. At this point the challenge is collecting you.",
    "That's {n} on the board. The easy tier is filing a restraining order.",
    "{n} losses. Even the honor challenges are starting to side-eye you.",
    "Streak broken so many times it filed for workers' comp. ({n})",
    "{n} fails deep. Have you considered… reading? Bold, I know.",
    "Career total: {n} losses. The leaderboard pretends not to know you.",
)

# 4+ — still playful, slightly meaner
_HABIT = (
    "Fail #{n}. This isn't a slump. This is a brand.",
    "{n} losses. Your Tadoku graph looks like a sad trombone solo.",
    "Hall of shame seat #{n} is warm. You never left.",
    "{n} fails. Somewhere an ota_master is powering up out of spite.",
    "Loss count: {n}. The bot considered silence. It chose violence.",
    "{n} on record. Retry is free. Your reputation is not.",
)

_CHECK = (
    "Check failed. The numbers did not clap back.",
    "Progress noted. Goal unimpressed.",
    "Almost counts in horseshoes, not challenges.",
    "Verify said no. Verify does not do pep talks.",
)


def pick_shame(kind: str, fail_count: int) -> str:
    """One roast line. fail_count = lifetime expired challenges (incl. this one if expired)."""
    n = max(0, int(fail_count or 0))
    if kind == "fail" and n < 2:
        line = random.choice(_CHECK + _SINGLE)
    elif n >= 4:
        line = random.choice(_HABIT)
    elif n >= 2:
        line = random.choice(_MULTI)
    else:
        line = random.choice(_SINGLE)
    return line.format(n=n)


def format_shame_caption(kind: str, fail_count: int) -> str:
    """Extra caption block under the card mention line."""
    n = max(0, int(fail_count or 0))
    quote = pick_shame(kind, n)
    if kind == "expired" and n >= 2:
        return f"📉 **{n} failures on record**\n_{quote}_"
    if kind == "fail" and n >= 2:
        return f"💀 prior losses: **{n}**\n_{quote}_"
    return f"_{quote}_"


_EMPTY_MONTH = (
    "A whole month. Zero clears. The leaderboard is a blank stare.",
    "No completions. Tadoku closed the tab out of secondhand embarrassment.",
    "The challenges waited. Nobody came. Awkward.",
    "Empty board. Even the bots stopped pretending to be impressed.",
)

_NEW_YEAR_OK = (
    "{year} challenges are loaded. Fresh year, fresh excuses — go make some clears.",
    "Happy {year}. Catalog's ready. Your streak (or lack of one) resets in spirit only.",
    "{year} is live. The easy tier is already judging your January plans.",
)

_NEW_YEAR_MODS = (
    "It's {year} and the challenge catalog is empty. Mods: the YAML isn't going to write itself.",
    "New year, same missing challenges. Someone with admin perms owes the server a catalog.",
    "{year} called. It wants challenges. The mods appear to be AFK in the wrong channel.",
    "Zero challenges for {year}. Immersion trackers everywhere; definitions nowhere. Fix it.",
)


def empty_month_shame(month_label: str) -> str:
    line = random.choice(_EMPTY_MONTH)
    return (
        f"📉 **Monthly shame · {month_label}**\n"
        f"No challenge clears on the board.\n_{line}_"
    )


def new_year_welcome(year: int, n_challenges: int) -> str:
    line = random.choice(_NEW_YEAR_OK).format(year=year)
    return (
        f"🎆 **Happy {year}** · {n_challenges} challenges loaded\n"
        f"_{line}_\n"
        f"Nothing cleared yet — that's normal. Don't let it stay that way."
    )


def new_year_mod_shame(year: int) -> str:
    line = random.choice(_NEW_YEAR_MODS).format(year=year)
    return f"📢 **{year} catalog missing**\n_{line}_"


def pick_shame_image():
    """Disappointment pool: CONGRATS_DIR/shame/, else None."""
    from app.congrats import _iter_dir_images, congrats_root  # noqa: PLC0415

    pool = _iter_dir_images(congrats_root() / "shame")
    return random.choice(pool) if pool else None


if __name__ == "__main__":
    # Small demo of what Discord captions look like
    random.seed(0)
    mention = "@Monty"
    samples = [
        ("fail", 0, f"💀 {mention} failed a check"),
        ("fail", 2, f"💀 {mention} failed a check"),
        ("expired", 1, f"⏰ {mention} ran out of time"),
        ("expired", 3, f"⏰ {mention} ran out of time"),
        ("expired", 5, f"⏰ {mention} ran out of time"),
    ]
    for kind, n, head in samples:
        print("---")
        print(head)
        print(format_shame_caption(kind, n))
