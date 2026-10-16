"""Explicit administrative completion attestation, never synthetic proof.

Authorization belongs to the transport. The returned ordinary correction preview
must still pass lifecycle.apply_correction's atomic revalidation on confirmation.
"""
import json
from datetime import datetime, timezone

from app.services import attempts, lifecycle


def preview_attestation(attempt_id, *, discord_id, actual_completed_at, actor, reason,
                        content=None, exception_reason=None):
    lifecycle._reason(actor, reason, 'preview:admin-attestation')
    att = attempts.get_attempt(int(attempt_id))
    if not att or str(att['discord_id']) != str(discord_id):
        raise ValueError('Attempt not found for the selected member')
    if att['status'] not in ('active', 'expired', 'abandoned'):
        raise ValueError('Attestation requires an active, expired or abandoned attempt; use the correction page for other states')
    actual = lifecycle._date(actual_completed_at)
    now = datetime.now(timezone.utc)
    if actual > now:
        raise ValueError('Cannot attest a future completion')
    evidence = json.loads(att.get('evidence_json') or '{}')
    if not isinstance(evidence, dict):
        raise ValueError('Existing evidence requires the full correction page')
    evidence['administrative_attestation'] = {'actor': str(actor), 'reason': reason,
        'actual_completed_at': actual.isoformat(), 'recorded_at': now.isoformat(),
        'source': 'admin_attestation', 'synthetic_verification': False}
    changes = {'status': 'completed', 'actual_completed_at': actual.isoformat(),
               'confirmed_at': now.isoformat(), 'verified_at': now.isoformat(),
               'verify_method': 'admin_attestation', 'evidence_json': json.dumps(evidence),
               'note': '\n'.join(s for s in (att.get('note'), f'Administrative attestation: {reason}') if s)}
    if content is not None and str(content).strip():
        changes['content'] = str(content).strip()
    return lifecycle.preview_correction(att['id'], changes, exception_reason=exception_reason)
