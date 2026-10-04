# Security

## Deployment

- Configure a strong, unique `UI_PASSWORD`. Blank passwords disable admin access.
- Keep the admin port on loopback. Use an SSH tunnel or HTTPS reverse proxy for
  remote access. HTTP Basic alone does not encrypt credentials.
- Restrict `ADMIN_ROLE_IDS` and Discord role-management permissions to trusted
  server administrators. Achievement changes recalculate eligibility.
- Set `DISCORD_GUILD_ID` (or the web UI guild setting). Discord admin commands
  fail closed without a configured guild; admins of other servers are not trusted.
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
- Keep dependencies updated and run `python -m pip_audit -r requirements.txt`.
- Keep credentials in `.env` or a secret manager, never source code or images.
  If a token was ever published, rotate it; deleting a file is not remediation.
- Review image licensing before making the repository public.

## Reporting

Use the repository's private vulnerability reporting facility when available,
or contact the maintainer privately. Never put tokens, personal data, database
exports, or exploit details in public issues.
