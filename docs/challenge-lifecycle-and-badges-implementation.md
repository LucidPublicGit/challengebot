# Challenge lifecycle and badges: implementation and deployment handoff

Status: **follow-up patch deployed on the Mac mini, 2026-10-17 UTC
(2026-10-16 Pacific)** from source commit `9594779`. The earlier release includes
Weekly, compressed scoring, four duration badges and scoped formula administration.
The patch adds reviewed confirmation/scoring fixes and the revert optimization.
Discord is connected and commands are synced. Advanced workflow limitations and
remaining manual acceptance coverage are listed below; deployment does not claim
every item in the broader planning document is implemented.

## Follow-up patch deployment — 2026-10-17 UTC

- Explicit user authorization to deploy followed the completed commit/push.
  Built with `docker-compose.yml` plus `compose.mac.yaml` from an allow-listed,
  tracked source/assets context. Runtime data, `.env` and Tailscale state were
  excluded; the build used `--env-file /dev/null` and `env_file: !reset []`.
- Release image: `challengebot-release:patch-20261017t014342z`.
  Deployed image identity:
  `sha256:93078a02b133ff0f793b67cacff3e4d3ab45c60fbb1a4a62c8c1ce6bb0e6023d`.
  Deployed application file hashes match the committed source.
- Before cutover, saved a private consistent SQLite online backup plus matching
  catalogs, achievement definitions/uploads, artwork and protected configuration.
  Retained the exact running image as
  `challengebot-rollback:pre-patch-20261017t014342z`. No Tailscale state was copied.
- Initial backup rehearsal passed initialization and second-start reconciliation.
  Then stopped only the application writer, verified no nonempty WAL remained,
  took a final consistent pre-upgrade snapshot, and repeated both rehearsal checks
  successfully before recreating only `challengetracker` with the tested image.
- Exact final candidate passed the network-disabled, read-only, tmpfs-only Linux
  scoring smoke at 1,000 synthetic members / 12,000 attempts: preview 0.376s,
  apply 0.422s, revert 0.037s, including retries, snapshot/award restoration,
  initialization after apply/revert, integrity and foreign keys.
- Post-deployment consistent snapshot reconciles against the final pre-upgrade
  baseline: every member's effective points and attempt counts/statuses are
  unchanged; database integrity is `ok` and foreign keys are clean.
- `/health` reports `{"ok":true,"discord":true}`; the application is healthy.
  Authenticated lifecycle/catalog/history/roster pages return 200 with expected
  controls and origin/referrer policy. Discord login and command sync are confirmed.
  The gateway remains healthy with the same container ID and start time; it was
  not recreated or restarted. No test announcements were sent.
- An isolated restore drill using the preserved old image and final pre-upgrade
  backup passed integrity/FKs and reconciled counts, statuses and all member totals.
  Backups, runtime configuration, logs, reports and metadata remain private outside
  Git. The deployed patch is complete; live interaction/endpoint-contract and broader
  load acceptance remain the separately documented limitations, not new release work.

## Follow-up validation history — before patch deployment

- Revalidated the current checkout: **347 passed, 1 skipped**, with the two
  existing deprecation warnings; `git diff --check` is clean. The skip is the new
  opt-in synthetic scale check, which passed separately at both sizes below.
- Added `tests/test_scoring_load.py`, using the existing fail-closed disposable
  SQLite guards, real catalog backfill and receipt synchronization, and no network
  providers. It covers scoped Monthly recalculation with inherited/absolute Monthly
  and unrelated Weekly awards; completed/active/scheduled attempts; original rules
  and award preservation; every member's totals; apply/revert retries; initialization
  after apply and revert; exact snapshot restoration; and SQLite integrity/FKs.
- **200 synthetic members / 2,400 attempts** passed: preview 0.081s, apply 0.096s,
  revert 0.352s. **1,000 members / 12,000 attempts** passed: preview 0.405s,
  apply 0.471s, revert **8.551s**. These are single-run local observations, not
  latency guarantees, concurrent-load results, Docker benchmarks or live acceptance.
  Seeded histories intentionally exercise multiple statuses on the same edition;
  this is a scoring stress fixture, not an end-to-end eligibility simulation.
- Profiling reproduced the slow revert and attributed over eight seconds to JSON
  decoding: each receipt/open snapshot reparsed the complete saved operation result.
  `revert_operation` now decodes that immutable result once under the existing write
  lock, retaining the same policy checks, compensation and legacy snapshot fallback.
  A non-timing regression verifies one decode across multiple receipts and opens.
- Both scale checks pass after optimization: at 200 members, preview 0.081s,
  apply 0.093s, revert **0.010s**; at 1,000 members, preview 0.399s, apply 0.468s,
  revert **0.049s** (about 170× faster than the initial observation). Exact award,
  rule, snapshot and total restoration, retry idempotency and restart checks pass.
  These remain local synthetic observations, not general performance guarantees.
- Focused independent review returned **no actionable findings**, with narrow
  signoff for cached result decoding, the decode-count regression and disposable
  scale test. The reviewer independently passed **342 tests, 1 skipped** and the
  1,000-member / 12,000-attempt scale check (revert **0.054s**), plus five
  disposable checks for missing snapshot maps/entries and complete transaction
  rollback on changed-open, changed-receipt-policy and void-receipt conflicts.
  `git diff --check` passed. This is local scoped signoff, not release-wide/live
  acceptance. Concurrent and broader-catalog performance remain unvalidated.
- Converted the reviewer's five one-off probes into durable parameterized tests:
  missing open-snapshot map, missing individual entry, and transaction rollback on
  changed-open, changed-receipt-policy and void-receipt conflicts. The rollback
  tests compare attempts, awards, adjustments, policies, operations, definitions
  and metadata before/after rejection. All 27 focused scoring regressions pass.
  A second focused independent review returned **no actionable findings** and
  narrow signoff for all five additions. The reviewer passed 44 scoring tests
  and the 1,000-member scale check (revert 0.054s), including retries/restarts.
- Built `challengebot-lifecycle-validation:release-candidate` with the Mac Compose
  pair from an allow-listed temporary source/assets context. Runtime `.env`, DB,
  Tailscale state and other runtime directories were excluded; `--env-file /dev/null`
  plus `env_file: !reset []` excluded runtime configuration. No Compose `up` ran.
  Built manifest: `sha256:54d07a2611c80193da947ccba1214627c02bdd987a4c48a0c945cf21d8f506bc`.
- The Python 3.12 Linux image passed the 1,000-member / 12,000-attempt scoring
  round trip with preview 0.375s, apply 0.421s and revert **0.038s**. The smoke
  reused the scale-test function with only its pytest import/decorator removed,
  invoking it against a new temporary directory. The container had network disabled,
  read-only root, dropped capabilities, no host mounts/configuration and tmpfs-only
  SQLite. It checked scoped awards/overrides, open snapshots, exact restoration,
  retries, initialization after apply/revert, and integrity/FKs. This was a targeted
  scoring smoke, not a full Linux pytest run or live Discord/Tadoku acceptance.
- The validation work above initially used no production database, credentials,
  member evidence or running service. Source commit `9594779` was subsequently
  pushed and deployed with explicit authorization as recorded at the top of this
  document. No further implementation or deployment step is pending for this patch.
  Real Discord member/admin acceptance, controlled live Tadoku contract checks and
  broader concurrent/large-catalog load coverage remain outstanding.

Reproduce the opt-in synthetic check (use the project's Python environment):

```sh
CHALLENGEBOT_LOAD_MEMBERS=1000 python -m pytest -o addopts='' -q -s tests/test_scoring_load.py
```

## Earlier post-deployment validation notes — historical, since deployed above

- Re-ran the baseline suite: **302 passed**, with the same two existing
  deprecation warnings. Added eight disposable-database regression cases covering
  the default-scoring cutover across active/scheduled attempts, inherited/absolute
  awards, and corrected tier/duration classifications. They verify scoped base
  preservation for inherited awards, unchanged absolute overrides, retained original
  rules and audited prior snapshots, restart/delayed activation, completion totals
  and foreign-key integrity.
- The expanded local suite passes: **341 passed**, with only the existing
  Starlette/httpx and Pillow deprecation warnings; `git diff --check` is clean.
  These tests are local follow-up changes, not a new deployed release.
- The fresh read-only independent review completed and found three issues:
  in-flight confirmation could overwrite a reviewed correction, current catalog
  overrides could hide older inherited awards from recalculation, and revert
  conflicts included disjoint or already-withdrawn policy revisions. These are
  resolved locally by the changes below, not deployed fixes or a release-wide
  review signoff.
- Confirmation now checks active status and correction revision under a write
  lock before committing, rejects stale verification, and retains current scoring
  snapshots for scoring-only changes. Four synthetic provider-wait regressions
  cover reviewed reclassification, owner content edits, voids and scoring-only
  revisions.
- Historical recalculation now inspects each receipt/open snapshot's accepted
  override rather than excluding an entire edition using its current catalog
  override. Revert conflicts ignore withdrawn revisions and proven-disjoint
  catalog scopes, while retaining protection for still-effective overlapping
  revisions and scopes that may intersect in future editions. Twenty-one new
  scoring regressions cover inherited versus absolute awards, open/scheduled
  snapshots, mixed/disjoint scopes, successive reverts and genuine conflicts.
- The scoring implementation delegate ended with a provider authentication error
  after leaving changes; the parent inspected those changes and reran the complete
  suite successfully. The second read-only review confirmed the two scoring
  fixes and original provider-wait reproduction, but found a narrower concurrent
  writer edge when saving aliases/log IDs adopted a newer revision after deriving
  old verification inputs. Matching saves now require the originally selected
  active revision and return their accepted row under the same lock; confirmation
  derives verification inputs only afterward. Six additional cases cover edits
  before/after matching saves and ordinary alias/log-ID confirmation.
- The third focused read-only review returned **no actionable findings** and
  narrow signoff for the matching-revision fix and its interactions with the
  previously reviewed changes. It independently passed all **341 tests**, reran
  all six matching scenarios with corrections on a separate writer thread, and
  confirmed `git diff --check`. Together with the second round's scoring signoff,
  this closes the reported local review findings, not release-wide/live acceptance.
  These fixes are local and uncommitted; production remains unchanged.
- No production database, runtime configuration, live member evidence or running
  service was accessed or changed for this follow-up. Live Discord interaction,
  Tadoku endpoint-contract and load acceptance remain outstanding.

## Weekly and revised defaults — deployed 2026-10-16

- Removed the **Past Challenges** button from the Discord difficulty picker.
  The existing `/challenge list past:true` history command remains available.
- Duration sections now use distinct colored markers, uppercase labels and
  divider lines: 🟦 Daily, 🟩 Weekly, 🟪 Monthly, 🟨 Yearly. Discord embed text
  cannot use custom font colors. Only populated sections and filters appear.
- Added Weekly across catalog validation/import/export, admin duration and
  correction controls, scoring policies, browsing and concurrent attempts.
  Its deadline is exactly seven days from actual start, including scheduled
  starts, with UTC storage and saved deadlines. Complete-work honor verification
  retains the originally selected work. Event cards derive duration from the
  actual saved window rather than a legacy hours field.
- Daily uses the difficulty base (1/2/3/4). Weekly/Monthly/Yearly use
  `ceil(multiplier × (difficulty base + 1) / 2)`, with multipliers 2/3/4.
  The resulting Easy/Medium/Hard/Ota-Master awards are Daily 1/2/3/4,
  Weekly 2/3/4/5, Monthly 3/5/6/8 and Yearly **4/6/8/10**.
  Absolute overrides remain distinct. Formula versions are captured in policies
  and attempt snapshots; earlier linear policies stay reproducible.
- Per the user's follow-up, startup appends one audited default revision and
  recalculates applicable open Weekly/Monthly/Yearly snapshots, retaining their
  old snapshots in the operation record. Completed awards and Daily attempts are
  unchanged. Scheduled attempts retain actual-start policy capture; explicit
  revisions survive delayed activation. The cutover runs once, so later admin
  changes survive restarts. Unknown historical awards retain the old migration
  baseline rather than being reconstructed with the new formula.
- Added lifetime Weekly **週** receipts, alternative-clear/revocation handling and
  a fourth header badge, in 日/週/月/年 order. Weekly completions do not count
  toward Daily sweep rosters or bonuses. Four-badge cards with long/Japanese
  names were rendered and inspected at 320px.
- **302 tests passed** using disposable SQLite and synthetic providers, with the
  same two existing deprecation warnings. New coverage checks seven-day UTC
  windows, year-end schedules/restarts, concurrency, honor completion and work
  preservation, annual eligibility, compressed points at every difficulty, web
  Weekly creation/scoped formulas, duration ordering/styling, button removal,
  open-attempt cutover with completed awards preserved, formula-version revert,
  and Weekly badge alternatives/history/glyphs/layout. Historical regression
  fixtures explicitly retain their prior linear 1/10/50 policy; new-default tests
  use fresh defaults.
- Deployment explicitly authorized by the user. The Mac Compose pair built the
  image from an allow-listed source/assets context excluding runtime data,
  credentials and Tailscale state. The network-disabled, read-only Linux smoke
  passed Yearly 4/6/8/10, Weekly timing/badge, Monthly 3→5→3 and idempotency,
  effective category totals, restart, command limit/button removal, four-badge
  CJK PNG rendering and SQLite integrity.
- A private consistent online backup and matching catalogs/artwork/uploads/runtime
  configuration were saved before cutover, with the exact prior image retained.
  The first final-snapshot attempt rejected a read-only SQLite sidecar requirement
  and automatically restarted the old app without migrating production. The
  successful cutover then stopped the writer, verified there was no nonempty WAL,
  obtained a consistent final snapshot, rehearsed migration and second startup,
  and recreated only the application service. The gateway was unchanged.
- Both rehearsals and the post-deployment production snapshot reconcile cleanly:
  attempt/status counts and every member's effective points are unchanged;
  integrity is `ok` and foreign keys are clean. There were **zero open attempts**
  to revise. An isolated restore drill with the preserved old image and final
  pre-upgrade backup also reconciles. All backups/reports/metadata remain private.
- Deployed image manifest:
  `sha256:1bdfc6af2c767843797b4c9ad7bd0dac932131812223dd00988bab9204d7c3c0`.
  `/health` reports `{"ok":true,"discord":true}`; both app and gateway are healthy.
  Discord command sync is confirmed. Authenticated lifecycle/history/roster/catalog
  pages return 200 with Weekly controls and the new formula. No test announcements
  were published. No independent review signoff is claimed.

## Scoped formulas — deployed 2026-10-16

- Added a **Shared formula — selected scope** target at `/lifecycle`. Edition,
  permanent identity and catalog-year selections intersect. Future-only and
  retroactive applications use the existing reviewed/idempotent operations;
  inheritance remains intact. Whole-catalog changes still require explicit
  all-editions confirmation, and ambiguous global/scoped selections are rejected.
  Date filters restrict historical attempts, not future policy applicability.
- Fixed formula reverts with overlapping prior scopes. A compensating revision
  now withdraws the original formula at the revert cutover and replays the remaining
  applicable revisions, rather than flattening different edition baselines into
  one formula. Historical cutoffs and revision-bounded classification corrections
  retain the original policy. Later overlapping formula changes still require a
  fresh explicit operation. Awards and included open snapshots retain their
  existing transactional compensation and conflict checks.
- No schema migration is needed. New compensating revisions contain an internal
  `reverts_policy_id` marker rejected by public policy validation. Older code does
  not interpret this marker; rollback after using these operations requires the
  matching database backup, not merely an older image against the newer database.
- **289 tests passed** on disposable SQLite, including ten new scope/revert cases;
  only the existing Starlette/httpx and Pillow deprecation warnings remain.
  Real browser form submission on synthetic data passed scoped Monthly 10→5,
  apply and compensating revert while the unrelated Monthly award stayed at 10.
  The combined deployed release passed the Linux build/smoke and private
  reconciliation checks above; no independent review signoff is claimed.

## Release verification — 2026-10-16

- Explicit user authorization: finish and deploy, with a backup before changing
  any existing database content. A consistent SQLite online backup, matching
  catalogs/artwork/uploads, and protected runtime configuration were saved outside
  the checkout before migration. The old image is retained as
  `challengebot-rollback:pre-lifecycle-20261016`; private deployment metadata records
  the image IDs and backup checksum. No Tailscale state was copied.
- Production-backup rehearsal passed migration and second-start reconciliation:
  attempt/status counts and every member's effective points were unchanged;
  integrity was `ok`, foreign keys were clean, and no definitions required review.
  Original unknown historical facts remain labeled rather than invented.
- **279 tests passed** after the release fixes. The Mac Compose pair built the
  Linux image. Its network-disabled, read-only, tmpfs-only smoke passed anniversary
  timing, Tales honor completion, 10→5→revert, idempotency, lifetime badges, restart,
  Discord command limit, CJK profile rendering and database integrity.
- Real browser acceptance on disposable synthetic data passed scoring preview,
  apply and compensating revert (10→5→10). Desktop forms and 320px profile cards
  with long/Japanese names and no/all/individual badges were visually inspected.
  Fixed checkbox widths and a browser-only form submission failure: `no-referrer`
  produced `Origin: null`; `Referrer-Policy: same-origin` permits same-origin form
  posts while continuing to suppress cross-origin referrers. The origin guard
  still rejects missing, null and foreign origins.
- The application was stopped for cutover, but the container was recreated with
  the new image during the attempted final stopped-writer snapshot. No additional
  pre-migration snapshot was obtained at that step. The earlier verified backup
  remains the rollback baseline; production reconciliation against it is clean.
- Deployed image manifest:
  `sha256:ca133053477e89976ba9b9c8c78fcc37cc30a8f1318036726c223d1ecd7e9c84`.
  `/health` returns `{"ok":true,"discord":true}`. Authenticated lifecycle, roster,
  operation-history, catalog and Tales pages return 200 with the corrected header.
  The Tailscale gateway remained healthy. No test announcements were published.
- A post-migration consistent production snapshot reconciles cleanly against the
  pre-migration report, with no member-point or attempt-status differences. An
  isolated restore drill started the preserved old image against a private copy
  of the backup; its integrity, foreign keys, counts and totals also reconcile.
  Backups, reports and deployment metadata remain private, outside Git.


## Implemented

- Additive, transactional/restart-safe lifecycle migration with permanent identity,
  edition and definition revision records. Legacy IDs remain resolvable; renewal
  requires a new edition ID with an explicit permanent identity, not title matching.
- Attempts capture accepted rules/scoring, edition/year, original start/deadline,
  timezone/calculation version and completion evidence/facts. Legacy rules/awards
  are labeled migration baselines, not asserted to be original advertised facts.
  Actual completion time is not fabricated from legacy confirmation timestamps.
- Daily positive windows up to 24 hours; clamped UTC month/year anniversaries;
  persisted scheduled deadlines; distinct long-challenge concurrency; cross-edition
  duplicate-open prevention and once-ever/annual eligibility. Existing attempts can
  complete after retirement. Scheduled scoring uses actual scheduled start and can
  reconstruct audited override cutovers across a delayed restart.
- Safe retirement/restoration and scheduled retirement/publication/restoration.
  Restoration boundaries apply to edition or identity scope; startup and scheduled
  processing materialize due restorations without changing accepted attempts.
  Pending restorations can be explicitly cancelled with audit/idempotency.
  Replace-import
  retires omitted published rows, never hard-deletes their references. Startup
  seeds only missing entries and does not reseed over admin changes/retirement.
- Incompatible legacy ladder windows are preserved as custom Special durations and
  flagged in `meta.lifecycle_review_required`; saved deadlines are not shortened.
  Calendar policy is explicitly recorded as UTC. Non-UTC calendar arithmetic is
  deliberately unsupported, not silently inferred from saved member UTC offsets.
- Point inheritance versus explicit overrides; editable difficulty bases and
  duration multipliers; scoped future/retroactive previews, atomic apply,
  idempotency IDs, stale/tampered-preview rejection and compensating revert.
  Original awards plus signed adjustments are retained in a source-keyed ledger.
  Leaderboard/profile totals and category progress use effective awards. Semantic
  classification corrections survive later rescoring and point-only reverts.
- Scoped `include_open` recalculations retain the complete challenge-applicable
  policy and effective corrected classification, not a globally merged approximation.
  Explicit open scoring revisions carry policy ID, actor/reason/cutover and actual
  applied snapshots for exact compensating revert. An explicit revision applied
  after a delayed scheduled start survives scheduler catch-up.
- Versioned Daily rosters include Physical and exclude Weekly/Monthly/Yearly. Earned
  receipts retain their qualifying roster; completion correction checks alternatives
  before revocation. Daily bonus entitlement is unique per user/year/tier. Lifetime
  Weekly/Monthly/Yearly receipts retain lower tiers and alternative qualifying clears.
- Deliberate historical Daily roster repair at `/lifecycle/rosters`: read-only
  preview, explicit requirements, member progress/award/signed-bonus impact,
  stale/tamper rejection and atomic idempotent apply. Immutable supersession chains
  retain old roster and receipt history and cannot branch. Repair reconciliation
  is scoped to the selected Daily cohort, not unrelated lifetime awards.
- Up to four header badges 日/週/月/年, highest tier for each duration; Daily header,
  compact stamps and name styling use the current UTC year. Achievement artwork
  and profile sections remain. Mac CJK fallback fonts and long-name truncation added.
- Difficulty-first duration sections, populated duration filters with All, bounded
  listing/start/attempt/history pages, continued long descriptions, completion
  markers, retired/year-filtered Past Challenges (`/challenge list past:true`),
  effective/original point explanations (`/challenge scores attempt_id:`), and
  duration/policy details in catalog dumps and announcements. The slash-command
  root remains within Discord's 25-command limit.
- Authenticated same-origin-protected admin scoring/lifecycle/correction
  preview/apply/revert/history pages at `/lifecycle`. Existing attempts now use
  reviewed correction previews rather than immediate web writes. Catalog override
  clearing uses an explicit scoring action. Ordinary catalog forms and
  retirement/restoration now preview current member progress/award effects before
  applying. Future lifecycle boundaries include a separately labeled forecast based
  only on known clears; forecast awards are rolled back, never granted early.
- Discord catalog add/edit/toggles/retirement and attempt void now collect a human
  reason and require an owner- and current-role-checked immutable review/apply step.
  The complete private preview is an in-memory JSON attachment, not truncated to an
  embed budget or written to disk. Stable operation IDs make retries idempotent.
  Start events retain original selected work; owner content/focus/match edits retain
  before/after audit, increment correction revisions and invalidate stale evidence
  caches/previews. Correction-generated completion debits/reinstatements retain
  the reviewing actor/reason; combined reinstatement/reclassification uses linked
  adjustment IDs. Test-derived completion receipts are explicitly labeled.
- Catalog merges now require reason/preview/apply at `/challenges/seed`. Pasted YAML
  or bounded source files under `challenges/` are captured in the preview; apply never
  reopens the source. Omitted entries/fields, overrides and retirement history remain
  unchanged. Override/lifecycle changes use their dedicated workflows. Atomic batches
  publish only final roster sets, never transient shrunken intermediate requirements.
- Administrative diagnostics are read-only, including overdue and due-scheduled
  attempts; synthetic `apply=True` and forced/synthetic direct confirmation are
  rejected. The Discord admin panel offers an explicit completion-time attestation
  with human reason, preserved evidence and reviewed dependency effects. Future dates
  and wrong-member attempts are rejected; window exceptions need a separate reason.
  Attestations are labeled administrative exceptions, retain actor/reason/operation,
  and do not automatically announce or grant external rewards. Existing historical
  synthetic awards are preserved rather than erased by the new prospective guard.
- Easy Monthly Tales/Trails (`tales-trails-2026`), annual policy, inherited 10 points,
  selected game retained at start/confirmation, honor-based fresh Japanese main-story
  playthrough rules, optional evidence, no mandatory hour minimum/Tadoku dependency.
- Tadoku pagination refuses truncated/invalid/unavailable evidence instead of
  returning authoritative partial results. Long requests use user-wide logs, not
  one contest as proof of cross-year coverage. Incomplete polls clear stale
  automatic-pass/progress signals. Deadline corrections retain delivered reminder
  markers while invalidating evidence caches.

## Validation

All automated tests use disposable SQLite or synthetic provider responses. The
initial synthetic rehearsals and container smoke checks used no live database,
member evidence, service credentials or Tailscale state. The release rehearsal
above separately used a private recoverable production backup.

- Latest follow-up local suite: **341 passed**, with Starlette/httpx and Pillow
  deprecation warnings only. `git diff --check` is clean.
- `tests/conftest.py` installs safety guards during collection: SQLite connections
  may target only memory or resolved OS temporary roots; repository runtime `.env`
  loading is skipped. A temporary default DB and per-test configuration reset prevent
  helpers from accidentally initializing the configured runtime DB. Guard tests verify
  runtime paths are rejected before opening a file.
- Renderer validation includes no badges, individual durations, all three at
  Ota-Master, long/non-Latin names and 320px mobile previews. Synthetic previews
  are gitignored under `data/card_previews/duration/`.
- Synthetic pre-lifecycle migration reconciles attempt counts/statuses, repeated
  completion awards, current-year sweep bonuses and per-member effective totals.
  Unknown completion dates stay unknown; repeated initialization is clean. Old-year
  badges reconstructed from known clears do not invent additional old-year bonus
  points absent a supported baseline receipt; these zero-point baseline projections
  explicitly carry `baseline_bonus_unknown` provenance.
- CLI copy/rehearsal run: before→migration and migration→second-start comparisons
  both clean. Reports remain private temporary files, not repository artifacts.
- The earlier implementation-phase Mac build (before this follow-up) used `docker compose -f docker-compose.yml -f compose.mac.yaml build
  challengetracker`. No Compose `up`, restart or old Windows stack was run.
- The resulting Linux image passed an isolated network-disabled, read-only-root
  container smoke check: imports, clamped anniversary, Tales honor completion,
  ledger 10→5→revert plus idempotency, lifetime badge, restart, command limit and
  CJK profile PNG rendering. It used only tmpfs SQLite, no host data mounts or env file.
- The earlier delegated independent review was interrupted by the provider usage
  limit and returned no signoff. A completed read-only follow-up review found a
  roster-version collision after historical repairs and premature progress removal
  when retirement was only scheduled. Both are fixed with regression tests for
  reviewed edits, boundary restart, and edition/identity progress at exact cutovers.
  The second review confirmed those fixes and found that open-attempt classification
  corrections could still issue the old point amount on eventual completion. Award
  creation now resolves effective tier/duration under the accepted policy and absolute
  override while retaining original snapshots and the correcting actor/reason/operation.
  Eight active/scheduled × inherited/override × tier/duration cases cover this fix,
  including badge/category agreement. A subsequent review found scoped policy values
  were lost during `include_open` recalculation; this is fixed and covered by scoped
  base → duration correction → recalculation → completion cases, plus delayed
  activation/revert. The latest review request covering those fixes and new import/
  attestation safeguards was interrupted by the provider usage limit, without signoff.
- Latest Linux validation also passed: the Mac Compose pair built the independent
  `challengebot-lifecycle-validation:followup` image from an allow-listed temporary
  source/assets copy. `--env-file /dev/null` and an `env_file: !reset []` override
  excluded runtime configuration; no runtime DB, `.env` or Tailscale files entered
  the context. The network-disabled, read-only image smoke used only tmpfs SQLite
  and no host mounts. It covered scoped corrected 200-point entitlement, blocked
  synthetic apply, attestation, import retries, repair → new roster, scheduled
  restoration/restart, category totals, command limit, CJK PNG and SQLite integrity.
  No existing service was started or restarted.

Reproduce local tests with a Python environment containing requirements plus pytest:

```sh
python -m pytest -o addopts='' -q
```

## Known limitations and follow-up acceptance

1. Browser scoring preview/apply/revert and card visual inspection passed. Discord
   command registration and gateway readiness are verified; real member/admin
   interaction acceptance has not been exercised. Targeted authorization, actor,
   source, stale-preview and entitlement tests pass. The local follow-up fixes
   have narrow independent review signoff as recorded above, not release-wide or
   live-interaction acceptance. Private compatibility helpers with `legacy_admin`
   defaults must not be exposed as a shortcut around reviewed operations.
2. Edition/identity retirement, restoration and cancellation support one pending
   retirement/restoration pair. A dedicated editor for multiple disjoint future
   identity availability intervals is not implemented.
3. Catalog YAML exports preserve definition/duration/policy fields, not the entire
   attempts/ledger/receipt/event graph. Use the verified SQLite backup plus matching
   assets for full recoverability.
4. Reconstruction is bounded by known facts. `legacy_baseline`, unknown completion
   dates and unknown historical bonuses remain explicit provenance. The production
   rehearsal and deployed reconciliation passed without rewriting these unknowns.
5. Startup observed successful live user-wide Tadoku pagination. A controlled live
   endpoint-contract/cross-contest/year acceptance test has not been performed.
   The local synthetic scoring scale check above passed through 12,000 attempts,
   and the identified repeated-decoding revert bottleneck is fixed locally;
   concurrent workload, large-catalog and
   live-data performance acceptance remain open. Incomplete evidence remains unavailable rather than
   definitive failure. Full-graph fingerprints and atomic recalculations favor
   correctness; staged background batches are not implemented.
6. Calendar arithmetic is UTC. Host Compose timezone does not change captured
   attempt deadlines.

## Repeatable private backup and rehearsal procedure

1. Schedule a maintenance window. Record the running image ID and code revision.
   Preserve the pre-migration image until restore validation is complete. Never
   assume a fresh image tag identifies the previously running image.
2. Create a private, timestamped backup outside this Git checkout. Use SQLite's
   online `Connection.backup()` API or stop writes before copying; never copy only
   a live main `.db` file while omitting committed WAL state. Existing
   `app.services.db_backup.snapshot_sqlite` provides the backup API operation.
3. Back up the matching catalogs, achievement image uploads, achievement definitions,
   and locally customized completion/new-challenger artwork. Preserve required local
   configuration separately and securely. Do not commit any backup/settings export,
   `.env`, activity report, evidence or credentials. Never copy Tailscale state into
   implementation/test artifacts.
4. Rehearse on a new private directory:

```sh
python scripts/dry_run_lifecycle.py \
  --source /private/path/recoverable-backup.sqlite \
  --work-dir /private/path/new-lifecycle-rehearsal
```

The source is opened read-only. The work directory must not exist. The command
creates a consistent copy, writes before/after reports, migrates **only the copy**,
reinitializes it, and reports baseline/restart reconciliation. Exit 2 means an
unexplained difference; do not deploy until each is resolved or deliberately
approved with an auditable operation. Reports include hashed member keys, which
still represent private activity and must not be shared/committed.

Standalone read-only comparison:

```sh
python scripts/reconcile_lifecycle.py --database /private/path/backup.sqlite \
  --output /private/path/before.json
python scripts/reconcile_lifecycle.py --database /private/path/migrated.sqlite \
  --compare /private/path/before.json --output /private/path/after.json
```

5. Inspect foreign keys/integrity, completion/status counts, every member-point
   difference, review-required rows, unknown dates and receipt provenance. Exercise
   an isolated Tales 10→5→revert operation, correction/void/reinstate, retirement and
   profile render against that copy. No public announcements should be sent.
6. Only after remaining gates are addressed and deployment is explicitly authorized,
   build with the Mac Compose pair and start the selected service in the maintenance
   window. Startup automatically applies migrations; taking a backup **after**
   startup is not a pre-migration backup.
7. Restore drill: stop the application writer, preserve the failed upgraded state
   privately, restore the complete known-good SQLite backup and matching assets,
   remove only the stopped restored database's stale WAL/SHM companions, and start
   the preserved old image. Never restore while an old connection remains open.
   Reconcile the restored database against its pre-migration report before reopening
   writes. An old image must not be pointed at an upgraded database as a rollback
   strategy without a tested compatibility guarantee.
