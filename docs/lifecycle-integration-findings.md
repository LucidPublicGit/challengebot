# Integration findings during implementation

Historical working notes, not the current defect list. Closeout: the command-limit,
font, Daily-slot leakage, scheduled scoring-cutover, baseline totals, semantic
correction/rescoring and negative-total consistency regressions were resolved.
The latest guarded local suite passes 279 tests. Follow-up implements historical
roster repair, scheduled restoration/cancellation, catalog impact previews, reviewed
Discord catalog/void operations and owner content/match audit. Independent follow-up
review found roster-version allocation collisions after repair and premature progress
denominator removal on scheduled retirement; both are fixed with regression tests.
The re-review confirmed those fixes and found stale eventual point entitlement after
classification corrections on open attempts. Award creation now applies the corrected
classification under the accepted policy/override and records correction attribution;
eight active/scheduled inheritance/override regression cases pass. A subsequent review
found loss of scoped policy values in include-open replacement snapshots; corrected
classification plus complete scoped policy are now preserved, including explicit
post-start recalculation across delayed activation and compensating revert. Catalog
merge and administrative completion now use captured preview/apply; synthetic tests
are diagnostic-only. Latest independent review was interrupted by the provider usage
limit and returned no signoff. The isolated source-only Mac Compose build and tmpfs,
network-disabled Linux smoke passed. Remaining product/deployment gates are in
`challenge-lifecycle-and-badges-implementation.md`. The 2026-10-16 release is now
deployed: 279 tests, production-backup rehearsal/reconciliation, isolated old-image
restore drill, browser 10→5→10, and live Discord health passed. Release browser QA
also fixed full-width checkboxes and `no-referrer` producing null form POST origins.
No completed independent review or real Discord interaction acceptance is claimed.

- Parent test environment: `/tmp/challengebot-fixes-venv/bin/python -m pytest` (has dependencies).
- Baseline: 128 passed, Japanese font _oneline failure. Renderer agent fixed Mac CJK fonts; renderer now passes 42 tests.
- Mid-implementation full suite: bot_commands module import raises `ValueError: maximum number of child commands exceeded` at new `/challenge past` near line 4306. Discord top-level group already has 25 commands; history must be integrated into an existing command or command subgroup. Bot agent: please resolve before completion.
- Renderer integration must pass `duration_badges=badges.duration_badges(discord_id, year)` to profile call.
- Parent owns `app/tadoku/client.py`, `app/services/progress_status.py`, scripts/reconcile_lifecycle.py, scripts/dry_run_lifecycle.py and tests for these. Do not overwrite.
- Backend review attention: `capture_attempt` currently captures scoring even for scheduled rows. Please ensure future-only policy cutovers use scheduled **actual start**, not schedule creation, while keeping original advertised rules/deadline/audit. Core tests should cover schedules crossing policy changes. Also ensure category/tier progress denominators exclude Monthly from Daily sweeps/slots.
- Midflight targeted suite sees expected obsolete fixture assumptions (catalog count 30→31, safe new-year import now requires new edition ID) and Daily tier_slot_progress includes 7 slots instead of 6 because Tales leaks in. Fix Daily-slot query; update obsolete catalog tests to intentional changed behavior, not undo safe imports.
- Parent added complete/incomplete evidence handling: bounded fetch refuses partial response; progress caches invalidate on window changes and cached goal_ok clears on fetch failure. Lifecycle corrections should retain sent notification markers rather than zero status_json to avoid duplicates.
