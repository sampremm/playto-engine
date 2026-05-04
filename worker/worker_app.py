"""
Playto Payout Engine — Celery Worker
=====================================
Processes payouts across sharded PostgreSQL databases (Neon Cloud).

Architecture:
  - API dispatches: process_payout.apply_async([payout_id, shard_name], countdown=2)
  - Beat sweeper:   every 30s, finds orphaned/stuck payouts and re-queues them
  - Idempotency:    Redis L1 + PostgreSQL L2 prevents double-processing
  - State Machine:  PENDING → PROCESSING → COMPLETED/FAILED (enforced by model)
"""

import os
import sys
import random
import uuid
import redis
from celery import Celery

# ── Django Setup (must happen before model imports) ───────────────────────────
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
import django
django.setup()

from django.db import transaction, close_old_connections
from django.conf import settings
from payouts.models import Payout
from merchants.models import LedgerEntry
from webhooks.tasks import dispatch_payout_webhook

# ── Celery App ────────────────────────────────────────────────────────────────
app = Celery('worker_app', broker=os.environ.get('REDIS_URL', 'redis://localhost:6379/0'))

# ── Redis for idempotency L1 cache ───────────────────────────────────────────
_idem_redis = redis.Redis.from_url(
    settings.IDEMPOTENCY_REDIS_URL,
    decode_responses=True,
)

# Database alias for idempotency keys
IDEM_DB = getattr(settings, 'IDEMPOTENCY_DB_ALIAS', 'idempotency_db')

# Max retry attempts before force-failing a stuck payout
MAX_ATTEMPTS = 5

# All data shards (no 'default' — it mirrors shard_0 and causes confusion)
SHARDS = ['shard_0', 'shard_1']


def log(msg):
    """Print to stdout with flush — the ONLY reliable way to see output in Docker logs."""
    print(f"[WORKER] {msg}", flush=True)


# ── Helper: find payout across shards ─────────────────────────────────────────
def _find_payout(payout_uuid, hint_shard=None):
    """
    Locate a payout across shards.
    
    Args:
        payout_uuid: UUID object for the payout
        hint_shard: Optional shard name (fast path from API dispatch)
        
    Returns:
        (payout, shard_name) or (None, None)
    """
    # Close stale DB connections before querying (critical for long-lived workers)
    close_old_connections()
    
    # Fast path: API told us exactly which shard
    if hint_shard:
        try:
            payout = Payout.objects.using(hint_shard).filter(id=payout_uuid).first()
            if payout:
                log(f"🎯 Found {payout_uuid} in specified shard: {hint_shard}")
                return payout, hint_shard
            else:
                log(f"⚠️ Not found in specified shard {hint_shard}, falling back to hunt")
        except Exception as e:
            log(f"⚠️ Error querying specified shard {hint_shard}: {e}")

    # Fallback: scan all shards
    for shard in SHARDS:
        try:
            payout = Payout.objects.using(shard).filter(id=payout_uuid).first()
            if payout:
                log(f"🎯 Found {payout_uuid} in shard: {shard}")
                return payout, shard
        except Exception as e:
            log(f"⚠️ Could not query {shard}: {e}")
            continue

    return None, None


# ── Helper: clear stale idempotency locks ────────────────────────────────────
def _clear_idempotency_lock(payout):
    """Remove stale IN_FLIGHT idempotency keys from both Redis and PostgreSQL."""
    from payouts.models import IdempotencyKey

    try:
        stale_keys = IdempotencyKey.objects.using(IDEM_DB).filter(
            merchant_id=payout.merchant_id,
            idem_status=IdempotencyKey.STATUS_IN_FLIGHT,
        )
        count = stale_keys.count()
        if count > 0:
            for key_record in stale_keys:
                rkey = f'idem:{payout.merchant_id}:{key_record.key}'
                _idem_redis.delete(rkey)
                log(f"🧹 Cleared Redis idem lock: {rkey}")
            stale_keys.delete()
            log(f"🧹 Cleared {count} stale idempotency key(s) from PostgreSQL")
    except Exception as e:
        log(f"⚠️ Could not clear idempotency locks: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN TASK: Process a single payout
# ══════════════════════════════════════════════════════════════════════════════
@app.task(name='worker_app.process_payout', bind=True, max_retries=3,
          acks_late=True, reject_on_worker_lost=True)
def process_payout(self, payout_id, shard_name=None):
    """
    Process a payout end-to-end.
    
    Called by:
      - API dispatch: process_payout.apply_async([id, shard], countdown=2)
      - Beat sweeper: process_payout.delay(id)  (no shard hint)
      
    State machine: PENDING → PROCESSING → COMPLETED/FAILED
    """
    log(f">>> process_payout(id={payout_id}, shard={shard_name})")

    # ── Step 1: Convert string to UUID ────────────────────────────────────
    try:
        payout_uuid = uuid.UUID(payout_id) if isinstance(payout_id, str) else payout_id
    except (ValueError, AttributeError) as e:
        log(f"❌ Invalid payout ID: {payout_id} ({e})")
        return

    short_id = str(payout_uuid)[:8]

    # ── Step 2: Find the payout in the sharded databases ──────────────────
    payout, active_shard = _find_payout(payout_uuid, hint_shard=shard_name)

    if not payout:
        log(f"❌ Payout {short_id} NOT FOUND in any shard!")
        return

    log(f"📋 Payout {short_id}: status={payout.status}, attempts={payout.attempt_count}, shard={active_shard}")

    # ── Step 3: Skip terminal states ──────────────────────────────────────
    if payout.status not in ('PENDING', 'PROCESSING'):
        log(f"⏭️ Payout {short_id} already terminal: {payout.status}")
        return

    # ── Step 4: Increment attempt counter ─────────────────────────────────
    payout.attempt_count += 1

    # ── Step 5: Idempotency loop breaker ──────────────────────────────────
    # After MAX_ATTEMPTS, force-fail and refund to prevent infinite loops
    if payout.attempt_count >= MAX_ATTEMPTS and payout.status == 'PENDING':
        log(f"🔓 Payout {short_id} stuck after {payout.attempt_count} attempts — force-failing")
        _clear_idempotency_lock(payout)
        try:
            with transaction.atomic(using=active_shard):
                payout.transition_to('PROCESSING', using=active_shard)
                payout.transition_to('FAILED', using=active_shard)
                LedgerEntry.objects.using(active_shard).create(
                    merchant=payout.merchant,
                    amount_paise=payout.amount_paise,
                    entry_type='DEBIT_RELEASE',
                    description=f'Refund — loop breaker for payout {payout.id}'
                )
            log(f"💸 Payout {short_id} force-FAILED, funds released")
            dispatch_payout_webhook(payout)
        except Exception as e:
            log(f"❌ Failed to force-fail payout {short_id}: {e}")
        return

    # ── Step 6: Transition to PROCESSING ──────────────────────────────────
    if payout.status == 'PENDING':
        try:
            payout.transition_to('PROCESSING', using=active_shard)
            log(f"📥 Payout {short_id} → PROCESSING (shard: {active_shard})")
        except ValueError as e:
            log(f"⚠️ Cannot transition {short_id}: {e}")
            return
    else:
        # Already PROCESSING — just update attempt count
        payout.save(using=active_shard, update_fields=['attempt_count', 'updated_at'])
        log(f"🔄 Retrying payout {short_id} (attempt {payout.attempt_count})")

    # ── Step 7: Simulate bank transfer (mock) ─────────────────────────────
    # In production, this would call a real payment gateway API
    outcome = random.random()

    if outcome < 0.1:
        # 10% chance: bank timeout → retry later
        log(f"⏳ Payout {short_id} — bank timeout. Retrying in 30s.")
        process_payout.apply_async(
            args=[str(payout_uuid), active_shard], countdown=30
        )
        return

    elif outcome < 0.85:
        # 75% chance: success
        try:
            with transaction.atomic(using=active_shard):
                payout.transition_to('COMPLETED', using=active_shard)
                LedgerEntry.objects.using(active_shard).create(
                    merchant=payout.merchant,
                    amount_paise=0,
                    entry_type='DEBIT_FINAL',
                    description=f'Finalized payout {payout.id}'
                )
            log(f"✅ Payout {short_id} COMPLETED!")
            dispatch_payout_webhook(payout)
        except Exception as e:
            log(f"❌ Error completing payout {short_id}: {e}")

    else:
        # 15% chance: bank rejection → fail and refund
        try:
            with transaction.atomic(using=active_shard):
                payout.transition_to('FAILED', using=active_shard)
                LedgerEntry.objects.using(active_shard).create(
                    merchant=payout.merchant,
                    amount_paise=payout.amount_paise,
                    entry_type='DEBIT_RELEASE',
                    description=f'Refund for failed payout {payout.id}'
                )
            log(f"❌ Payout {short_id} FAILED — funds refunded")
            dispatch_payout_webhook(payout)
        except Exception as e:
            log(f"❌ Error failing payout {short_id}: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# BEAT TASK: Sweep for orphaned/stuck payouts every 30 seconds
# ══════════════════════════════════════════════════════════════════════════════
@app.task(name='worker_app.retry_stuck_payouts')
def retry_stuck_payouts():
    """
    Periodic sweep that catches payouts the real-time path missed.
    
    Handles two cases:
      1. PENDING > 30s old: API task was lost, re-dispatch with shard hint
      2. PROCESSING too long: Exponential backoff, force-fail after max retries
    """
    from django.utils import timezone
    from datetime import timedelta

    close_old_connections()
    now = timezone.now()

    for shard in SHARDS:
        try:
            # ── Case 1: Orphaned PENDING payouts (older than 30s) ─────────
            orphaned = list(Payout.objects.using(shard).filter(
                status='PENDING',
                created_at__lt=now - timedelta(seconds=30),
            ))
            for payout in orphaned:
                log(f"🔁 [BEAT] Re-queuing orphaned PENDING {str(payout.id)[:8]} "
                    f"(attempt {payout.attempt_count}, shard: {shard})")
                # Pass shard hint so the worker doesn't have to hunt
                process_payout.apply_async(
                    args=[str(payout.id), shard], countdown=1
                )

            # ── Case 2: Stuck PROCESSING payouts ──────────────────────────
            stuck = list(Payout.objects.using(shard).filter(status='PROCESSING'))
            for payout in stuck:
                delay_seconds = 30 * (2 ** payout.attempt_count)
                if payout.updated_at < now - timedelta(seconds=delay_seconds):
                    if payout.attempt_count >= 3:
                        log(f"💀 [BEAT] Payout {str(payout.id)[:8]} exceeded max retries — force-failing")
                        try:
                            with transaction.atomic(using=shard):
                                payout.transition_to('FAILED', using=shard)
                                LedgerEntry.objects.using(shard).create(
                                    merchant=payout.merchant,
                                    amount_paise=payout.amount_paise,
                                    entry_type='DEBIT_RELEASE',
                                    description=f'Refund for max-retries payout {payout.id}'
                                )
                            dispatch_payout_webhook(payout)
                        except Exception as e:
                            log(f"❌ [BEAT] Failed to force-fail {str(payout.id)[:8]}: {e}")
                    else:
                        payout.attempt_count += 1
                        payout.save(using=shard, update_fields=['attempt_count', 'updated_at'])
                        process_payout.apply_async(
                            args=[str(payout.id), shard], countdown=2
                        )
                        log(f"🔄 [BEAT] Retrying PROCESSING {str(payout.id)[:8]} "
                            f"(attempt {payout.attempt_count})")
        except Exception as e:
            log(f"⚠️ [BEAT] Error scanning shard {shard}: {e}")


# ── Beat Schedule ─────────────────────────────────────────────────────────────
app.conf.beat_schedule = {
    'retry-stuck-payouts-every-30-seconds': {
        'task': 'worker_app.retry_stuck_payouts',
        'schedule': 30.0,
    },
}
app.conf.timezone = 'UTC'
