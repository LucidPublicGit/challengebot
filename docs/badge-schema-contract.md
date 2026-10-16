# Badge integration note

Badge service now follows incoming `docs/lifecycle-schema.sql` exactly (not the earlier proposed table layout). No additional schema changes needed.

- daily_rosters.year; badge_receipts.scope is `year:{year}` for Daily and `lifetime` for Monthly/Yearly. badge_receipts.year NULL for lifetime; status active/revoked.
- Daily requirements are sorted edition ID arrays, include Physical. All old roster versions retained. Awarded set is also captured in receipt requirements_json.
- Receipt events preserve originals and every revoke/reinstate/requalification.
- Bonus source `daily_bonus:{discord_id}:{year}:{tier}` inserted into award_ledger then adjusted with scoring.set_award(conn, source_key, points, operation_id=..., actor=..., reason=...). One entitlement per tier/year.
- sync_user and publish_rosters are caller-transaction-local. No schema initialization by badge service. Reads do not sync/publish.
