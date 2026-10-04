# Security

## Deployment

- Configure a strong, unique `UI_PASSWORD`. Blank passwords disable admin access.
- Keep the admin port on loopback. Use an SSH tunnel or HTTPS reverse proxy for
  remote access. HTTP Basic alone does not encrypt credentials.
- Restrict `ADMIN_ROLE_IDS` and Discord role-management permissions to trusted
  server administrators. Achievement changes recalculate eligibility.
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
