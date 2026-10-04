# Security and sharing audit

Audited: 2026-10-04. Target repository is **private**. A private repository is
not a substitute for removing secrets and personal data before sharing.

## Findings and remediation

| Severity | Finding | Resolution |
| --- | --- | --- |
| High | An empty admin password allowed unauthenticated reads and writes, including member data and database backups. | Admin access now fails closed with 503 unless `UI_PASSWORD` is configured. Health remains public. |
| High | HTTP Basic credentials could accompany forged cross-site form submissions to older admin routes. | All write requests require a matching Origin or Referer and reject cross-site Fetch Metadata. Existing achievement/announcement one-use tokens remain in place. |
| High | Pinned Starlette and multipart parser versions had published denial-of-service and other advisories. | Updated FastAPI, explicitly pinned Starlette and multipart to patched compatible releases; pinned current Pillow. Fresh dependency audit: 35 resolved packages, zero known findings. |
| Medium | Docker published the unencrypted admin UI on every host interface. | Published port is loopback-only; native host defaults to loopback. Docker listens on all interfaces only inside the container. |
| Medium | Server announcement IDs and a community contest ID were embedded in defaults/examples. | Removed; configure privately in `.env` or admin settings. |
| Medium | Existing Git authors, demo names, preview images, and the original remote identified the developer. | Anonymized demo text. Sharing snapshot excludes previews and carries a new anonymous-account root commit, not development history. The original local repository/history remain intact and must not be published. |
| Low | Build context could send secrets, runtime databases, agent sessions, and local artifacts to Docker. | Added `.dockerignore`; expanded Git ignores for local previews, temporary virtual environments, database sidecars and private key files. |
| Low | Admin pages could be framed/cached; API schema remained available. | Disabled OpenAPI endpoint; no-store, frame denial, nosniff, and no-referrer headers applied centrally. |

## Admin achievement commands

`/achievement_admin add`, `edit`, and `remove` are standalone because
`/challenge` is already at Discord's child-command limit. They are guild-only
and use the existing owner / Administrator / configured role authorization
check on every invocation. Responses are private.

All writes reuse the shared achievement validation and transactional
reconciliation service. Unknown edits do not create entries, IDs stay stable,
omitted edit fields are preserved, and removal needs explicit confirmation.
Removed definitions and historical receipts remain stored; edit can restore
publication. Artwork filenames must resolve to managed PNGs. New image upload
continues through the authenticated web UI rather than duplicating its parser.

## Verification

- Fresh isolated environment installed directly from the updated requirements.
  Sanitized Docker image built successfully; Python 3.12 container tests also
  passed (113 pytest / 110 standard runner), with zero dependency-audit findings.
- Pytest: **113 passed**. Standard-library test runner: **110 passed**.
  Pytest also discovers additional poster checks.
- Regression checks cover fail-closed auth, Unicode credentials, cross-site and
  missing-origin write rejection, schema hiding, safe response headers, command
  authorization, DM denial, achievement CRUD/restore, invalid image paths, and
  edits of missing IDs.
- Scanned 274 originally tracked paths and 1,008 reachable historical Git
  objects for identifying text, environment files and credential signatures.
  Historical text was compared against locally configured credential values
  without displaying those values. No matching credentials or tracked private
  environment files were found. Historical demo identity text was found.
- Inspected metadata of 197 runtime images for the known identifying strings;
  none matched. Development previews are excluded from the sharing snapshot.
- Scanned the 279-file sharing snapshot, including raw asset bytes, against
  known identifying strings, local credentials/server IDs, private path patterns,
  and common token/private-key signatures: zero matches.
- Existing SQL mutation/query construction and YAML loading were reviewed;
  inspected dynamic SQL uses controlled field names and bound values, and YAML
  uses `safe_load`. No shell execution or unsafe pickle deserialization was
  found in application paths inspected.
- Added GitHub CI for Python 3.12 tests and dependency auditing.
- Follow-up deployment verification: rebuilt and recreated the container;
  health returned 200, unauthenticated admin access returned 401, and configured
  credentials returned 200. Discord startup was ready with no error indicators.
  The full 113-test pytest suite passed again.

These checks are bounded source/dependency scans, not a penetration test or a
guarantee that every possible secret or identifying image detail was detected.

## Before deploying or making public

1. **Set a strong `UI_PASSWORD` privately.** At the initial audit, the local
   configuration had no password. Rebuild/restart after changing configuration to
   apply hardening and resync slash commands. The follow-up deployment has now
   configured authentication and applied the rebuild.
2. Configure contest and destination IDs privately. Defaults intentionally no
   longer point at any particular community or server.
3. Remote admin access needs an SSH tunnel or HTTPS reverse proxy. Preserve the
   browser-facing Host/scheme for write checks and trust forwarded headers only
   from the proxy. Do not make HTTP Basic available over public plain HTTP.
   Add rate limiting and body-size limits at the proxy if exposing the UI.
4. **Use a fresh clone of the new repository for future shared development.**
   Never push the original repository's history to it. Private files, history,
   backups, logs, local metadata, and original remote config were not copied.
5. Runtime user databases are excluded. Database exports contain identifiers,
   activity, settings and moderation notes; share code, not exports.
6. Images include third-party anime/manga/GIF assets. This audit does **not**
   establish distribution rights. Review asset licensing and visible contents
   before making the repository public. No project license was assumed or added.
7. Full production Discord invocation, TLS/proxy configuration, production
   runtime behavior, image OCR, and public account profile privacy are outside the
   regression checks. GitHub account/profile activity is not anonymized by Git.
8. Invite only intended collaborators. No collaborators were added automatically.

## Local commands

```powershell
python -m pip install -r requirements.txt pytest pip-audit
python -m pytest tests
python tests/run_all.py
python -m pip_audit -r requirements.txt
```

Keep audit output containing machine paths or local environment details out of
commits. Public reports should describe findings, not reproduce private values.
