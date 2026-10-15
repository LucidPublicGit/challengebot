# Security

## Deployment

- Configure a strong, unique `UI_PASSWORD`. Blank passwords disable admin access.
- Keep the admin port on loopback. Use an SSH tunnel or HTTPS reverse proxy for
  remote access. HTTP Basic alone does not encrypt credentials.
- Restrict `ADMIN_ROLE_IDS` and Discord role-management permissions to trusted
  server administrators. Achievement changes recalculate eligibility.
- Set `DISCORD_GUILD_ID` (or the web UI guild setting). All slash commands
  fail closed outside that guild, in DMs, and when no guild is configured.
  Stale global registrations cannot bypass this check.
- Discord admin access is not host access. Congrats paths stay inside
  `CONGRATS_DIR`; cover downloads use approved HTTPS image hosts, no redirects,
  and a streaming size limit. Keep managed asset directories operator-owned.
- Run the bot as an unprivileged account/container, with only its data directory
  writable and no Docker socket or host secrets mounted. These checks do not
  sandbox an operating-system administrator or an attacker who can modify code.
- The supplied container runs as UID/GID 10001 with a read-only root filesystem,
  all capabilities dropped, and privilege escalation disabled. On Linux, make
  the bind-mounted `data/` directory writable by that UID before starting it.
- Run one application worker/container. SQLite and one-use form tokens assume this.
- Back up SQLite and its adjacent `achievement_images/` folder securely; they
  contain member identifiers and activity. Do not commit or distribute backups.
- Tadoku links are self-declared and usable immediately, without administrator
  approval. Log-based verification checks the linked profile's activity, not
  ownership of that profile. Treat clears and rewards as community trust features.
  Honor clears remain self-declared and are labelled accordingly.
- Use only cosmetic reward roles with no guild permissions. The bot refuses
  roles with guild permissions, managed roles, and the default role. Also inspect
  channel permission overrides: a cosmetic role must not unlock sensitive access.
- Admin bodies are authenticated before parsing and capped while streaming:
  ordinary forms 256 KiB, artwork 4 MiB + 256 KiB overhead, catalog imports
  512 KiB + overhead, announcements 10 MiB + overhead. Endpoint image/catalog
  limits still apply. Keep proxy limits and authentication throttling as defense
  in depth; do not expand direct listener access without them.
- Build from `requirements.lock` with `--require-hashes`. To update intentionally,
  run `uv pip compile --universal --generate-hashes --python-version 3.12
  --output-file requirements.lock requirements.txt`, review the diff, then run
  `python -m pip_audit -r requirements.lock` and the full image tests.
  Pin updates to the Python image digest and Actions SHAs deliberately too.
- Keep credentials in `.env` or a secret manager, never source code or images.
  If a token was ever published, rotate it; deleting a file is not remediation.
- Review image licensing before making the repository public.

## Reporting

Use the repository's private vulnerability reporting facility when available,
or contact the maintainer privately. Never put tokens, personal data, database
exports, or exploit details in public issues.
