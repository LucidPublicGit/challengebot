# Mac mini deployment verification — 15 October 2026

The rebuilt Challengebot application is running and healthy on the Mac mini.
The Tailscale gateway was left running. `/health` reports both application and
Discord readiness. The running image matches the image tested and scanned:

`sha256:245db8215ee6309985757d5013dcab74cea30cd064e96b9533ce78981ec3a819`

## Workflow validation

All 129 tests passed in the Python 3.12 image, without network access and with
an unprivileged user, read-only root, dropped capabilities, and temporary data.
The new workflow regression executes the real link modal callback, starts an
attempt, runs the bot's automatic progress/completion loop, checks the First
Clear achievement receipt, and verifies cosmetic-role assignment exactly once.
Discord messages/roles and Tadoku responses use synthetic boundaries.

The bot is installed only in KanjiEater's Deep Weeb Lounge. Per the maintainer's
instruction, no separate test guild was used. The live reward-role map is empty,
so there are no configured completion roles or associated channel overrides to
review. No synthetic live attempts, achievements, announcements, or role grants
were created. Live interaction dispatch and role assignment were not exercised.

## Proxy and application checks

- Port 8090 publishes only on `127.0.0.1`.
- Tailscale Serve terminates HTTPS and proxies to the application; no Funnel
  exposure is configured in the active Serve configuration.
- HTTPS probes through an existing tailnet peer verified the TLS certificate,
  public health response, anonymous 401, and authenticated admin-page 200.
- Authenticated same-origin POST to an unused probe path returned 404, proving
  valid proxy scheme/host handling without invoking a mutation. Cross-origin
  POST returned 403.
- Forwarded-header trust was narrowed to `127.0.0.1` and the actual Challengebot
  proxy at `172.30.50.250`. The host bridge and the other app's proxy were removed.
  A spoofed forwarded scheme from the host bridge was ignored after deployment.
- Upload probes declared an excessive body but sent zero body bytes. Missing and
  incorrect credentials returned 401 immediately; valid credentials returned 413.
- The root filesystem remains read-only, all capabilities are dropped, and
  privilege escalation is disabled. The service runs as its unprivileged user.

The global tailnet ACL policy and distributed-client behavior were not audited.
The Mac host itself could not connect directly to the tailnet HTTPS address;
HTTPS verification used the existing tailnet peer instead. No access policy or
other service configuration was changed.

## Image vulnerability scan

Grype 0.120.1 was downloaded from its official release and verified against the
published SHA-256 checksum. Its vulnerability database was current to 15 October
2026. Both scans used local Docker image archives, without registry credentials
or Docker Scout login.

The initial scan found six advisory matches for the unused runtime `pip`
installer. The Dockerfile now removes pip after installing the hashed application
dependencies. The final scan has zero pip matches.

Final image results are package/advisory matches, so one CVE can appear against
multiple related packages:

- OS packages: 55 high, 48 medium, 10 low, and 46 negligible matches.
- Python interpreter: 6 medium and 1 negligible matches.
- Critical matches: zero.

The scanner lists no fixed package versions for the 55 high OS matches. Those
base/distribution findings remain unresolved; this is not a clean image scan.
The Python interpreter matches list fixes on newer Python branches and need
separate upgrade/reachability review. No major Python or distribution migration
was made during this deployment. The earlier clean application dependency audit
does not cover these OS/interpreter findings.

Full scan JSON, summary, image-test log, and rollback-image metadata are retained
locally under ignored `data/security-audit-20261015/` with restricted permissions.

## Backup and rollback

A consistent SQLite backup passed `PRAGMA integrity_check`; no uploaded artwork
was present. Backup:

`data/backups/pre-security-rollout-20261015T183002Z/`

Previous application image retained locally:

`challengebot:rollback-20261015T183048Z`

Only the application service was recreated, using the combined base and Mac
Compose configuration. The Tailscale gateway and production data mounts were
preserved.
