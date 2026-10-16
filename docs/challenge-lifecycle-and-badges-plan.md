# Challenge durations, lifecycle, scoring, and badges: implementation handoff

Status: final planning handoff; application implementation has not started.
Historical plan: implementation and later user-approved changes are tracked in
`challenge-lifecycle-and-badges-implementation.md`. The 2026-10-16 follow-up adds
Weekly (seven days) and a lifetime 週 badge, changes long-duration scoring to
`ceil(multiplier × (base + 1) / 2)` with multipliers 2/3/4 (Yearly 4/6/8/10), styles
duration headings and removes the Past Challenges button, superseding the
corresponding original defaults below.
Prepared: 2026-10-15.
Repository: `/Users/lucid/DevEnvPublic/challengebot` (formerly ChallengeTracker).

## 1. Objective and source of truth

Extend the current challenge system with Daily, Monthly, and Yearly groups, retirement, annual eligibility, adjustable points, and duration-specific badges. Preserve enough original and corrected data to explain historical results and deliberately recalculate them later.

This document supersedes earlier proposals in the planning conversation. In particular:

- Monthly points default to **10×** difficulty base points; Yearly defaults to **50×**.
- Points can be explicitly recalculated retroactively; original awards are retained for audit, not treated as permanently unchangeable totals.
- The profile header shows **at most three badges**, not twelve badges or a large badge grid.
- Daily badges reset annually. Monthly and Yearly badges are lifetime awards.
- Monthly/Yearly deadlines are personal anniversaries, not the end of the calendar month/year.

The user approved the core behavior below. Sections labeled “implementation default” resolve details that were discussed but not individually confirmed; implement these defaults unless repository constraints require a documented adjustment. Do not present those defaults as additional user decisions.

## 2. Agreed product behavior

### Catalog and browsing

- Retain the difficulties **Easy, Medium, Hard, Ota-Master**.
- Add independent duration groups **Daily, Monthly, Yearly**.
- Choose difficulty first; list its challenges under duration headings in that order.
- Do not display empty headings, filters, or duration menu choices. A tier with only Daily has no empty Monthly/Yearly sections.
- Keep completed active challenges visible with completion markers.
- Prefer sections over a mandatory extra duration-selection step. Add an optional duration filter when needed, while retaining an All view.
- Support pagination for both listing and starting challenges. Never silently drop challenges beyond Discord's 25-option limit or truncate the catalog to fit an embed.
- Retirement removes a challenge from new-attempt selection, not from history.

### Personal windows

- Daily: its configured positive duration, at most 24 hours.
- Monthly: one calendar month from the actual attempt start.
- Yearly: one calendar year from the actual attempt start.
- Clamp nonexistent anniversary dates to the target month's final day: January 31 → February 28/29; February 29 → February 28 the following year.
- Show the exact deadline before starting, then persist it. A catalog edit, restart, retirement, or January 1 rollover must not silently change an existing attempt's deadline.
- Scheduled attempts use their scheduled actual start, not the time the schedule was created.
- Duration labels do not imply daily/monthly repeat resets. Repeat eligibility is an independent setting.

### Points

| Difficulty | Base / Daily | Monthly default | Yearly default |
| --- | ---: | ---: | ---: |
| Easy | 1 | 10 | 50 |
| Medium | 2 | 20 | 100 |
| Hard | 3 | 30 | 150 |
| Ota-Master | 4 | 40 | 200 |

- Provide editable global defaults for base values and duration multipliers (initially 1, 10, 50).
- A challenge edition can inherit the applicable formula or have an explicit absolute point override. Clearing an override must be an explicit action that returns to inheritance.
- Support both future-only scoring changes and explicitly scoped retroactive changes.
- Changing only the Tales/Trails award from 10 to 5 must not change other Monthly challenges.
- Changing the shared Monthly multiplier from 10 to 5 can update all inheriting challenges in the selected scope; explicit overrides stay unchanged unless separately included.
- Profiles, category progress, leaderboard totals, and scoring explanations must agree on effective awards. Point-dependent progress denominators also need invalidation/rebuilds.
- Point changes do not change clear counts, completion dates, or completion-based badge eligibility.

### Badges

Show up to three earned badges in the top-right header, left to right:

1. **日 — Daily:** highest tier whose required Daily roster the user completed for the current catalog year.
2. **月 — Monthly:** highest tier in which the user has ever completed at least one Monthly challenge.
3. **年 — Yearly:** highest tier in which the user has ever completed at least one Yearly challenge.

Use difficulty colors, retain readable glyphs, and hide unearned slots. Replace the existing 完 glyph with 日 for Daily. Higher Monthly/Yearly awards upgrade the displayed badge without deleting lower awards.

- Daily requires **all** required Daily challenges in the tier, not one clear.
- Daily display resets each calendar year; preserve prior years' award history.
- Monthly and Yearly awards persist for life, including after a challenge is retired.
- Preserve the existing Daily-only best-tier/name styling and Daily sweep bonuses. Monthly/Yearly badge awards do not introduce extra sweep bonuses.
- Retirement, scoring changes, and ordinary catalog edits do not remove an earned badge. Correcting or voiding a qualifying completion may revoke or downgrade it, with audit history and recomputation from any other qualifying clears.
- Do not add one duplicate badge per year to the header. A historical badge view can show lower-tier awards, years, unlock dates, and qualifying attempts.

The current userinfo card is 560 logical pixels wide with dynamic height. Four existing stamps currently share space with the username; three duration stamps should fit, subject to rendering with long names and actual fonts. Keep leaderboard and event-card headers compact; wherever existing sweep stamps are used, make them explicitly Daily rather than implying a monthly completion is a Daily sweep.

### Retirement and annual eligibility

- Support **Active** and **Retired** states with actual lifecycle history. Future publication/retirement timestamps may be scheduled.
- Retired published challenges must never be hard-deleted through normal administration or catalog import.
- Repeat policy: **once ever** or **once per catalog year**.
- Annual eligibility becomes available again in a new catalog year only when that year's edition is published/available. A retired identity does not automatically return on January 1.
- Retain all old completions, their dates, scores, and awards across renewals and retirement.
- The attempt belongs to the catalog edition/year in which it starts. A December Monthly attempt finished in January credits its original edition and does not earn the new year's Daily badge.
- Default on retirement: existing attempts can finish under their captured rules and deadlines; block new attempts. Admin cancellation is a separate, explicit audited action.
- Lifetime Monthly/Yearly badges can be earned from a valid old-edition attempt finishing after retirement.

## 3. Implementation defaults and boundary rules

### Calendar/timezone policy

Use UTC instants for storage and deadline arithmetic in the first implementation, preserving time of day for month/year anniversaries. Display Discord-localized timestamps to users. Make the application/catalog calendar timezone an explicit recorded setting (initially UTC), rather than relying on the host's timezone. Use that timezone to determine catalog-year rollover consistently. Capture the timezone and calculation policy/version on attempts and scoring/badge operations so later changes are explainable.

Do not silently reinterpret saved UTC offsets as IANA timezones. If local-wall-clock anniversaries become a requirement later, record an actual timezone identifier and handle DST explicitly; old deadlines remain unchanged unless an administrator intentionally revises them.

Treat availability and verification windows as explicit half-open intervals `[start, end)` consistently. Show deadlines without implying that the next instant is still eligible. A manual late confirmation may attest to an earlier in-window completion only through an explicit correction/late-verification path; record both the completion and confirmation times.

### Concurrency and repeats

- New Monthly/Yearly challenges allow concurrency with Daily and other distinct long challenges.
- Retain the existing exclusive Daily slot and block duplicate open attempts of the same permanent challenge identity, including across annual editions.
- Overlapping immersion can satisfy different goals; do not introduce cross-challenge log exclusivity.
- A failed, expired, or abandoned attempt does not consume completion eligibility; retain existing retry controls.
- Once-ever eligibility is checked across all editions of the permanent identity; annual eligibility is checked within the edition year.
- A December open attempt must finish or be abandoned before starting the same identity's next-year edition. Once-ever completion blocks later editions; annual completion does not block the next year.
- Separate administrative/test/repeat exceptions from normal scoring entitlement. Existing production behavior must be characterized before migration; do not silently erase historically awarded repeat points.

### Daily roster and awards

Use explicit, versioned **Daily sweep rosters** per catalog year and tier, rather than querying all current challenge definitions dynamically.

- Include today's Daily Physical challenges as well as Ota challenges, preserving the existing scope.
- Record the roster/version that qualified each Daily award and its bonus.
- Adding a new Daily challenge or retiring an existing one publishes a new prospective roster version. Preserve already-earned awards and bonuses. Users without an award qualify against the applicable published roster; exclude retired requirements from the new roster by default.
- Never award a sweep for an empty roster. Monthly/Yearly challenges are never roster requirements.
- Give at most one normal Daily tier bonus per user/year/tier, even if several roster versions qualify.
- A historical completion correction re-evaluates the roster stored with an earned award. If that award is no longer valid, check other applicable qualifying rosters before revoking it. Retain the original receipt and adjustment history.
- Roster edits must preview affected progress/awards. Administrators can deliberately repair a historical roster, but ordinary edits must not silently revoke prior awards.

Daily awards reset in the header annually; existing annual bonuses remain in historical point totals. Monthly/Yearly lifetime badge tiers are derived from qualifying completion metadata, never from point totals or whichever tier a mutable catalog row currently has.

## 4. First new challenge

Create an Easy / Monthly / Ota edition titled **Complete any Tales or Trails game**, initially **10 points**, preferably inheriting the Monthly formula rather than setting a redundant override.

Implementation defaults for the rule text:

- Finish one Tales or Trails game's main story in Japanese within the personal month.
- Start a fresh playthrough within the attempt window; do not award a full clear for finishing an almost-complete preexisting save.
- Record the selected game title at start and retain it at confirmation; name/evidence corrections must be audited.
- Use the existing honor-based complete-work verification. Tadoku minutes cannot establish that a game's story was finished.
- Completion evidence screenshot is optional; record it if supplied. Do not make a 30-hour estimate into a mandatory minimum or automatic pass threshold.
- Allow concurrent attempts with other distinct challenges.
- Initially use annual eligibility so it can be offered again in a future published year; edition renewal is explicit. This is a planning default, not a user-confirmed choice of repeat policy for this particular challenge.

## 5. Data model: identities, editions, revisions, and receipts

The following are logical records; the implementer may adapt table names and reuse existing structures. Do not collapse these distinctions into one mutable catalog row or a single user-points counter.

### Permanent challenge identity

Track permanent ID, legacy ID aliases, created/recorded timestamps, lifecycle state, repeat-policy history, availability intervals, retirement timestamp/reason, and actor/source for changes. Never reuse a retired ID for unrelated content. Existing IDs referenced by achievements and commands must continue resolving.

### Catalog edition

Track permanent identity, immutable edition ID, catalog year, display number, publication/availability timestamps, revision references, created/updated timestamps, and lifecycle changes. Enforce identity/year uniqueness for annually renewed entries where applicable, and preserve stable existing numbering/lookup compatibility.

Availability is separate from an attempt's deadline. An edition may close for new starts on December 31 while its existing attempts continue into the next year. Retirement of one edition and permanent retirement of the identity must be distinguishable. Evergreen Special entries need an explicit compatibility model and must not be accidentally regenerated annually.

### Definition revisions

Append immutable before/after revisions containing title, description, difficulty, category, duration group, timing policy, goal, media/focus filters, verification policy, concurrency, repeat policy, scoring configuration, availability, and roster membership as applicable.

Record **when the revision was recorded** separately from **when it is effective**, its author/source, reason, and predecessor. Capture enough structure to calculate with it; a human-readable announcement alone is insufficient. Reuse/extend existing catalog history rather than maintaining conflicting histories.

### Attempt and completion facts

Every new attempt must retain:

- Attempt ID, permanent challenge ID, edition ID/year, member identity and existing guild scope.
- Definition revision ID plus immutable start-time rules snapshot.
- Created/recorded time, original scheduled start (if any), current effective scheduled start, actual start, original calculated deadline, and current effective deadline.
- Timing policy, source timezone/calendar policy, duration parameters, and calculation version used.
- Status transitions with effective time and recorded time: scheduled, active, completed, expired, abandoned, void; record cancellation separately from a failed live attempt.
- Actual completion time **when known**, confirmation/submission time, verification time, verification method, evidence references/log IDs, selected work/focus, and relevant verification window.
- Original completion metadata snapshot and current effective corrected metadata, including difficulty/duration/edition attribution used for badges and scoring.
- Current correction revision and provenance/quality markers for imported or uncertain legacy facts.

Use separate fields/events where dates have different meanings. `completed_at`, the time a moderator entered a correction, and the time the server received confirmation are not interchangeable. Preserve original values when correcting any of them. Do not invent an actual completion date from a verification timestamp without labeling it as a proxy.

Validate date relationships and record intentional exceptions with a reason. Correcting a start should preview whether its deadline is recomputed or retained. Correcting a completion across a deadline/year boundary should preview eligibility and awards; do not automatically move an attempt to another edition just because its completion date changed.

### Scoring policy revisions and award ledger

Record policy revision ID, base values/multipliers or override, inheritance source, scope, effective dates, actor, reason, and recorded timestamp.

For each eligible completion (and separately each Daily bonus), retain:

- Unique source entitlement key so the same completion/bonus cannot be awarded twice.
- User, source attempt or badge/roster receipt, edition/year, award type.
- Original points and scoring revision, original award time.
- Current effective points and policy/recalculation revision.
- Append-only point adjustment rows recording old/new values or signed delta, operation ID, reason, actor, effective time, and recorded time.

Store signed deltas as signed values; do not run them through existing nonnegative point clamps. Totals can be a query or a rebuildable projection of the ledger. Do not overwrite only a cached leaderboard total, and do not sum both original awards and their replacement values.

The ledger must support corrections, voids, reinstatements, retirement history, and rerunning the same recalculation without duplicate credit/debit. Keep sources and adjustments transactionally consistent. No administrator should have to edit SQLite manually to repair points.

### Badge and sweep receipts

Record member, duration, difficulty, scope (Daily year versus lifetime), qualifying attempt(s), roster/version for Daily, actual eligibility/unlock time if known, recorded/issued time, rule version, and revoke/reinstate events with reasons.

- For Monthly/Yearly, use the earliest valid qualifying completion for each tier; recompute from remaining completions after a void/correction.
- For Daily, preserve the required set and qualifying clear for each member of the awarded roster. Eligibility time is when the last required clear was completed, if all relevant dates are known.
- Lifetime display selects the highest valid tier; do not store only the currently highest badge and discard lower receipts.
- Legacy awards with unknown unlock dates remain eligible when supported by known facts, but display “date unavailable” rather than fabricating dates.

## 6. Retroactive administration and recalculation workflow

Provide authenticated admin workflows for catalog revisions, timing corrections, completion corrections, point changes, retirement/restoration, and roster repairs. Reuse existing authorization and form protections.

For scoring changes:

1. Choose global defaults or challenge/edition override.
2. Choose future attempts only or retroactive application; explicitly select challenge identities/editions, years/date range, and whether existing open attempts are included.
3. Define date-filter semantics clearly (e.g. attempt start versus actual completion). Default historical scopes to selected editions rather than an ambiguous “this year.”
4. Preview changed rules, affected attempts/users, previous/new totals and deltas, including retired sources where selected. Identify excluded overrides and missing legacy data.
5. Confirm one operation with a stable operation/idempotency ID and reason. Revalidate the preview's revision/version to avoid applying stale results after concurrent edits.
6. Commit policy changes, award adjustments, audit records, and projection invalidation atomically. For larger batches, stage/restart safely and publish totals only when the operation is complete.
7. Offer a compensating revert operation based on recorded affected sources; account for intervening changes rather than blindly restoring cached totals.

“Future attempts only” captures the new scoring policy for attempts started after its effective cutover; already-open attempts retain their accepted award unless explicitly included. Retroactive corrections use the selected replacement policy without altering original start snapshots.

Example: an Easy Tales completion originally awards 10. An admin sets that edition to an override of 5 and applies it retroactively. Record a -5 adjustment, show an effective award of 5, and retain the original 10-point policy/award. The member's Monthly badge and completion date remain unchanged. Re-running the same operation has no effect. Later changes use the effective 5 as their starting point, not a second subtraction from 10.

For non-scoring corrections, preview the dependency chain: verification/eligibility → effective completion facts → points → badge/bonus receipts → profiles/leaderboard/achievement projections. An explicit semantic correction may update classification for past awards, but an ordinary future catalog edit must not reclassify historical clears.

Record actor, source (web/slash/import/migration), reason, original/new values, recorded timestamp, effective timestamp, operation ID, and affected record IDs for every material change. Preserve evidence and prior versions; avoid hard deletion. Existing public Discord posts keep their original advertised points unless explicitly edited/published through a separate authorized action.

## 7. History and user-facing surfaces

- Add Past Challenges with catalog-year filtering and retired entries, completion dates, and yearly completion history.
- Clearly distinguish current versus retired status and original versus currently effective points where a score was adjusted. Make the audit explanation available without crowding the default profile.
- Preserve current achievements and their artwork/list views. Duration badges supplement that system.
- Update start, schedule, list/dump, status, confirm/abandon pickers, announcements, admin catalog forms, YAML export/import, profile cards, leaderboard explanations, and progress calculations.
- Show duration, effective points, and exact personal deadline where useful. Ensure back navigation preserves filters/pagination.
- Imports must preserve IDs, references, revisions, retirement history, attempts, and awards. Omission from an import is not permission to erase history; destructive replace behavior must become safe retirement/edition handling for published entries.
- Add duration/policy fields to change announcements and exports. Do not automatically publish announcements while migrating or recalculating.
- Keep reminder/milestone state tied to attempt and effective deadline revisions. Avoid daily-style reminder spam on long challenges and prevent duplicate notifications after corrections/restarts.

## 8. Migration and compatibility

Before implementation, inspect the actual schema migrations and production behavior, not only the README. The repository already has custom points, opt-in concurrent attempts, achievement snapshots, and transactional catalog history. The README's one-active-attempt description is incomplete.

Relevant starting points:

- `app/db.py`: SQLite schema/migrations.
- `app/catalog.py`, `app/catalog_changes.py`: import/lookup/scoring defaults/history.
- `app/services/attempts.py`: lifecycle, scoring, sweep queries, progress.
- `app/services/achievements.py`: completion snapshots and eligibility.
- `app/services/progress_status.py`, `app/tadoku/client.py`: long-window verification/progress.
- `app/bot_commands.py`, `app/challenge_card.py`, `app/web/app.py`: Discord/cards/admin.
- `challenges/2026.yaml`, `tests/`: catalog and regression coverage.

Migration requirements:

1. Take a recoverable local database/assets backup before touching live state. Never commit it or expose credentials/member activity in artifacts.
2. Add versioned, restart-safe migrations; validate foreign keys and reconciliation totals.
3. Preserve existing IDs as aliases and existing command/achievement references. Do not infer permanent identity matches for future catalogs by title similarity.
4. Classify existing ladder challenges as Daily only after validating their actual windows. Preserve Special custom windows (including six-month and evergreen entries) without forcing them into Monthly/Yearly or the new badges. Flag incompatible ladder rows for review rather than silently changing deadlines.
5. Preserve all current starts, deadlines, statuses, evidence, verification methods, known completion dates, and historical points/bonuses. Backfill from available records with explicit provenance; unknown original facts remain null/unknown.
6. Current scoring queries often join mutable definitions. Capture a migration baseline of reproducible effective awards/totals; label it as a migration baseline, not proof of what was originally advertised. Flag unrecoverable discrepancies rather than inventing old rules.
7. Preserve known existing sweep awards/bonuses; use available historical evidence and document reconstruction limits where no durable award receipts exist. Do not claim that a current query reconstructs every past earned badge.
8. Map legacy tier sweeps to Daily year-scoped receipts where supported. Preserve existing repeated completion awards as identifiable legacy exceptions if required to reconcile totals; enforce new normal eligibility rules prospectively.
9. Ensure migrations/restarts do not reseed over admin edits or resurrect retired challenges. Old-year editions and evergreen Special definitions remain resolvable.
10. Run a read-only reconciliation report before and after migration: attempts/statuses, completions, points/bonuses per member, badge receipts, references, and unknown metadata. Every intentional difference needs an explained operation.

Tadoku fetches currently have bounded pagination. A year of logs can exceed those bounds; incomplete fetches must not silently produce a definitive fail or incorrect progress. Verify endpoint coverage, pagination, and cross-contest/year behavior, and expose incomplete evidence clearly. Honor-based game completion must not depend on this fetch succeeding.

## 9. Implementation sequence

1. Characterize current repeat/scoring/sweep behavior with isolated fixtures and produce migration/reconciliation design.
2. Add identity/edition/revision records, attempt provenance/date fields, lifecycle events, scoring policies/ledger, and badge/roster receipts. Implement safe backfill and rebuildable projections.
3. Implement anniversary deadlines, eligibility, concurrency, retirement, annual edition renewal, and explicit correction/recalculation services. Test invariants before connecting UI writes.
4. Add admin preview/apply/revert workflows, policy controls, history, and safe imports/exports.
5. Update Discord navigation/pagination, personal deadline displays, three-badge header, historical views, point explanations, and reminder behavior.
6. Add the Tales/Trails edition, verify its inherited 10-point award, and run a 10 → 5 → revert exercise against an isolated database.
7. Render real cards and run relevant regression checks. Document validation and any deviations from this plan.
8. Hand off the concrete changes and deployment procedure. Do not infer deployment authorization from this document. Validate Mac deployment builds with `docker-compose.yml` plus `compose.mac.yaml`; do not start old Windows stacks.

## 10. Acceptance criteria

### Catalog and timing

- Empty duration groups disappear; completed active entries remain visible; retired entries appear in history only.
- Every challenge remains selectable with more than 25 entries and with long descriptions.
- Daily ≤24h validation; Monthly anniversaries across 28/29/30/31-day months; leap-year Yearly anniversaries; scheduled starts and restart persistence.
- December/January attempts retain full windows and original edition attribution; new annual editions become eligible only when available.
- Changing a catalog or timezone policy does not silently alter an existing deadline.
- Retirement blocks new starts but permits valid open attempts to finish; identity-level retirement prevents automatic annual return.
- Once-ever, annual, failed-retry, legacy-repeat exceptions, and cross-edition duplicate-open behavior are tested.

### Scoring and corrections

- Formula defaults match 1/10/50 multipliers; overrides and inheritance remain distinguishable.
- Tales 10 → 5 retroactively changes each included eligible completion by -5 and leaves other challenges/badges/dates unchanged.
- Global changes exclude explicit overrides by default; future-only cutovers preserve old open attempts.
- Scopes include/exclude retired editions correctly and have unambiguous date semantics.
- Preview totals match committed totals; repeated operations and interrupted/retried batches cannot double-award or double-debit.
- Void, reinstate, completion-time correction, deadline correction, classification correction, and compensating revert reconcile all dependent projections.
- Original facts and policy revisions remain readable after multiple corrections; unknown dates remain distinguishable from known dates.
- Points/bonuses match across ledger, leaderboard, profile, and category progress, including baseline legacy exceptions.

### Badges and history

- Header shows at most 日/月/年 in that order, with highest earned tier color and no unearned placeholder.
- Daily requires the applicable full roster, excludes Monthly/Yearly, and resets display annually while retaining history/previous bonuses.
- Monthly/Yearly need one qualifying completion in the tier, persist across years/retirement, and display the highest lifetime tier.
- Point recalculation cannot revoke a completion-based badge. Voiding a source checks alternative qualifying completions before downgrading/revoking.
- Roster changes preserve earned Daily awards; empty rosters cannot award; duplicate roster versions cannot duplicate bonuses.
- Historical unlock/issued dates are separate and explainable. Lower-tier and prior-year awards remain available in history.
- Render profiles and affected event/leaderboard cards with no badges, each individual duration, all three at Ota-Master, long/non-Latin names, and mobile-sized display. Preserve the existing achievement section.

### Safety and delivery

- Isolated tests and migration fixtures do not use or modify live member data.
- Migration is restart-safe, reconciliation differences are explained, and backup/restore is documented.
- No runtime databases, credentials, `.env`, or sensitive evidence appear in commits or handoff artifacts.
- Completion report lists implemented behavior, checks run, migration implications, and any unresolved limitations. No feature or live deployment is claimed complete without verification.
