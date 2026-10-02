# Achievement design decisions

Status: implemented; Pixel Arcade theme selected; 18 editable achievements.

## Separation from badges

Difficulty-tier sweep badges stay in the top-right of the user card.
Achievements reward exploration, distinct-clear milestones, and finished
projects. Display a compact showcase below Category progress; use a separate
paginated collection for the full list. Every achievement has a slot:
unlocked slots show their real artwork and names; locked slots show a neutral
pixel-art question-mark stub, without the real artwork or name on the card.
Do not show status labels, progress bars, next targets, or unlocked/total counts.
Do not add achievement points to the existing leaderboard.

## Discoverability and locked artwork

The achievement list reveals every achievement's name and exact requirement,
including achievements not yet unlocked. Only the artwork is a surprise.
This supersedes both the earlier unlocked-only display and the suggestion of
fully secret names/requirements.

Use a real Discord message button or `/achievements` to open the
list; controls drawn inside the PNG are not interactive. List entries show the
same neutral stub until earned, then the unique artwork. Unlocked detail entries
may show the unlock date. Do not deliver locked artwork in UI attachments.

Order earned achievements by unlock date, newest first, in both the user card
and the achievement list. Break equal-date ties by catalog order. Locked stubs
follow earned achievements in catalog order because they have no unlock date.
This supersedes the earlier stable-position/catalog-order decision.

Make the user-card tiles smaller than the first stub mockup, targeting roughly
24–28px artwork at the normal 560px card width. Use tight wrapping and remove
unused section height; preserve readable names and requirements in the list.
Repeated question-mark stubs are intentionally neutral;
the unique-silhouette requirement applies to earned artwork, not these stubs.
Allow the grid to wrap without leaving unused vertical space. Use pagination
for the detailed list as the catalog grows.

Cards use the capitalized heading **Achievements**. Do not publish the helper
sentences "Earn an achievement to reveal its artwork" or "Newest earned first".
Names and requirements use consistent capitalization, spelling, and punctuation.

## Tracking and administration

Definitions have permanent generic IDs, unrelated to the title or image.
The authenticated admin UI supports additions, edits, image uploads, and
removals. Removal hides a definition without deleting its ID or unlock history;
it can be restored through visibility. Defaults are seeded once, not reapplied
on every restart.

Unlock receipts record the user, achievement ID, UTC unlock date, and source
attempt. Completed attempts retain a challenge metadata snapshot and UTC
completion date. Editing a completed attempt does not overwrite that date.
Moderation corrections and rule changes reconcile eligibility; title and image
changes leave eligibility unchanged. Missing legacy dates are not invented.
The admin attempt form can supply a known missing date.

## No failure-based achievements

Achievements must never depend on failing, expiring, abandoning, or quitting
a challenge, nor on avoiding those outcomes. Do not use failure counts,
failure history, perfect records, or recovery from failure as unlock criteria,
progress, rarity, or achievement recommendations.

**Comeback is removed from the proposed catalog.** Do not replace it with
another failure-related achievement. Existing non-achievement failure tracking
is outside the scope of this decision.

## Approved design catalog: 18 achievements

Count distinct challenge IDs for clear milestones. Assign media formats
explicitly; generic `any` and broad `reading` challenges do not automatically
qualify as a specific format. The five formats are manga, shows, YouTube,
visual novels, and novels.

| Achievement | Requirement | Unique artwork direction |
| --- | --- | --- |
| First clear | Complete your first challenge | A single waving pennant |
| Getting rolling | Clear 5 distinct challenges | A rolling wheel with a motion trail |
| Double digits | Clear 10 distinct challenges | A sculpted numeral 10 |
| Seasoned challenger | Clear 20 distinct challenges | A worn adventure journal |
| Media sampler | Clear challenges in 4 of the 5 formats | Four interlocking puzzle pieces |
| All-rounder | Clear challenges in all 5 formats | A five-point orbital constellation |
| Double feature | Clear a reading and a listening challenge | Headphones wrapped around an open book |
| Two worlds | Clear an Ota and a Physical challenge | Two linked globes |
| Moving mind | Clear a walking and an endurance challenge | A winged running shoe |
| The long game | Finish a designated long-running Special challenge | A tall hourglass |
| Progress published | Complete JP Progress Video | A compact video camera |
| Grand Line | Complete the One Piece challenge | A pirate sailing ship |
| Into the uncanny | Complete the Umezu Kazuo challenge | An eerie spiral eye |
| Toriyama's world | Complete the Toriyama Akira challenge | An ink pen with a sweeping brushstroke |
| History buff | Complete the history manga challenge | An unfurled historical scroll |
| Cover to cover | Complete Novel in 24 Hours | A closed book with a ribbon bookmark |
| Page to screen | Clear a manga and a shows challenge | A filmstrip unfurling from a manga page |
| Quarter century | Clear 25 distinct catalog challenges | A freestanding arcade cabinet with 25 on its screen |

### Harder achievement decision

Only Quarter century was accepted from the eight additional proposals.
Reading beyond, Manga odyssey, Signal explorer, Masters' shelf, Long-haul legend,
Mind in motion, and Five-way summit were rejected and are not catalog entries.
Quarter century counts distinct challenge IDs, not repeat clears. It is not a
difficulty-tier sweep and introduces no shared deadline or failure condition.

## Artwork requirements

Each achievement must have its own recognizable subject, silhouette,
composition, and shape. No reused pictures, common medal/shield/trophy outlines,
or color-swapped duplicates. Related subject matter must still have distinct
silhouettes (for example, closed book versus journal versus unfurling page).

Typography, palette, lighting, rendering technique, and spacing may be shared
within the chosen theme. Neutral UI containers may repeat; the collectible
artwork itself must not rely on a repeated enclosing shape.

Display the neutral question-mark stub for locked achievements, never a dimmed
or blurred version of the real artwork. The earlier all-achievements mockup
depicts all 17 original achievements earned and predates Quarter century
and the decision to show locked stubs. The stub demonstration shows First clear
earned, with the other 17 slots concealed behind question-mark stubs. Names and
requirements remain visible in the separate achievement list.

## Theme selection

Selected: **Pixel arcade**, using crisp sprite artwork, limited-color shading,
and unique silhouettes. Keep the existing user-card chrome and place the
achievement artwork under Category progress. Profile statistics in mockups
are illustrative.

The user requested one example with all 17 achievements and authorized deleting
the earlier achievement mockups. This does not authorize deleting unrelated
card previews or application assets.
