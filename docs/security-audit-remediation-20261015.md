# Security audit remediation — 15 October 2026

Source audit: https://htmlpreview.github.io/?https://gist.githubusercontent.com/antonve/31125f8da7f70f6776387f65e8f66707/raw/2ad8fec012a4767558e638e7a411fed07c00ec9b/challengebot-security-audit-20261015.html

Implemented:

- **F01:** ASGI middleware checks disabled-admin and Basic authentication before
  receiving bodies. It rejects excessive Content-Length immediately and stops
  streamed bodies at endpoint-specific total limits, including multipart overhead.
  Parsing starts only after the bounded body is received. Existing CSRF, image,
  catalog, and replay checks remain in effect.
- **F02 / accepted trust model:** Tadoku links remain self-declared and usable
  immediately, per the maintainer's decision; there is no approval requirement.
  Log verification does not establish account ownership. The ownership finding
  remains an accepted limitation. Completion does reject a binding changed while
  fetching logs, and evidence records the linked account used for verification.
- **R01:** a shared Discord command-tree check rejects commands in foreign guilds,
  DMs, and when no guild is configured, including stale global registrations.
- **R02:** provider and Tadoku JSON responses have a 2 MiB decoded-byte ceiling.
  Covers must be JPEG/PNG/WebP, at most 4096 pixels per side and 8 million total
  pixels; Pillow bomb warnings fail closed. Cropping before resizing prevents
  extreme aspect ratios from creating oversized intermediate images.
- **R03:** the Python base image is digest-pinned; GitHub Actions are SHA-pinned.
  Docker and CI install the full application dependency closure from a hashed
  universal lockfile. Advisory scanning remains enabled in CI.
- **R04 / rewards:** reward assignment rejects roles with guild permissions,
  managed roles, and the default role. Deployment documentation requires checking
  channel overrides and proxy throttling before expanding access.

Validation:

- All 129 tests passed in the Python 3.12 production image with Noto CJK fonts,
  as UID 10001, with network disabled, read-only root, dropped capabilities, and
  temporary writable test data. The stdlib runner now includes poster and audit
  regression tests that it previously omitted.
- Regression checks cover zero body reads for missing/wrong credentials and
  disabled admin access; declared and chunked oversized requests; immediate
  linked-account polling/manual confirmation; relinking during a fetch;
  completion; foreign-guild/DM rejection; remote-response and
  cover limits; and cosmetic-only reward assignment.
- `pip-audit -r requirements.lock`: no known vulnerabilities found.
- The production Dockerfile built successfully with `--require-hashes`.
- The combined Mac Compose configuration validated successfully.
- Read-only inspection of the running application confirmed loopback-only
  publication of port 8090, an unprivileged user, read-only root, all capabilities
  dropped, no-new-privileges, a bounded temporary filesystem, and the single
  `python -m app.main` entry point. The Tailscale configuration proxies HTTPS.

Deployment verification:

- The Mac mini application was rebuilt and restarted after a consistent SQLite
  backup. The running container is healthy and Discord is connected.
- The isolated workflow test exercises the real link modal, automatic completion,
  achievement unlock, and cosmetic-role assignment with synthetic Discord/Tadoku
  boundaries. No test guild was used and no production rewards were issued.
- Existing Tadoku links continue to work without migration or approval.
- Live HTTPS, authentication, origin protection, forwarded-header handling, and
  pre-body upload rejection passed after deployment. Forwarded-header trust now
  includes only loopback and the Challengebot Tailscale proxy.
- The image scan was completed with checksum-verified Grype, without Docker Scout
  login. OS-package findings remain; see the full results and operational limits
  in [deployment verification](deployment-verification-20261015.md).
