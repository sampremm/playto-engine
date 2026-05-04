import os
import random
import uuid
import redis
import json
from celery import Celery

# Setup Django before importing models
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
import django
django.setup()

from django.db import transaction
from django.conf import settings
from payouts.models import Payout
from merchants.models import LedgerEntry
from webhooks.tasks import dispatch_payout_webhook

app = Celery('worker_app', broker=os.environ.get('REDIS_URL', 'redis://localhost:6379/0'))

import logging
logger = logging.getLogger('payouts')

# ── Redis connection for idempotency key cleanup ──────────────────────────────
_idem_redis = redis.Redis.from_url(
    settings.IDEMPOTENCY_REDIS_URL,
    decode_responses=True,
)

# Database alias for idempotency keys
IDEM_DB = getattr(settings, 'IDEMPOTENCY_DB_ALIAS', 'idempotency_db')

# Maximum attempts before force-failing a payout stuck in an idempotency loop
MAX_LOOP_ATTEMPTS = 5


def _clear_idempotency_lock(payout):
    """
    Clear stale idempotency locks for a payout that is being force-failed.
    This prevents the infinite loop where:
      1. Beat finds PENDING payout → queues task
      2. Worker sees idempotency lock → exits in 0.0001s
      3. Payout stays PENDING forever
    """
    from payouts.models import IdempotencyKey

    try:
        # Find and remove any IN_FLIGHT idempotency keys for this merchant
        stale_keys = IdempotencyKey.objects.using(IDEM_DB).filter(
            merchant_id=payout.merchant_id,
            idem_status=IdempotencyKey.STATUS_IN_FLIGHT,
        )
        count = stale_keys.count()
        if count > 0:
            # Also clear from Redis L1
            for key_record in stale_keys:
                rkey = f'idem:{payout.merchant_id}:{key_record.key}'
                _idem_redis.delete(rkey)
                logger.info(f"🧹 [WORKER] Cleared Redis idem lock: {rkey}")

            stale_keys.delete()
            logger.info(f"🧹 [WORKER] Cleared {count} stale idempotency key(s) from PostgreSQL")
    except Exception as e:
        logger.warning(f"⚠️ [WORKER] Could not clear idempotency locks: {e}")


@app.task(name='worker_app.process_payout', bind=True, max_retries=3)
def process_payout(self, payout_id, shard_name=None):
    # Convert string ID back to UUID object — Celery JSON serialization
    # strips the UUID type, causing Postgres queries to silently fail
    try:
        payout_uuid = uuid.UUID(payout_id) if isinstance(payout_id, str) else payout_id
    except ValueError:
        logger.error(f"❌ [WORKER] Invalid Payout ID format: {payout_id}")
        return

    payout = None
    active_shard = 'shard_0'
    
    # Fast path: if the API told us which shard, use it directly
    if shard_name:
        try:
            payout = Payout.objects.using(shard_name).filter(id=payout_uuid).first()
            if payout:
                active_shard = shard_name
                logger.info(f"🎯 [WORKER] Found payout {payout_uuid} in specified shard: {shard_name}")
        except Exception as e:
            logger.warning(f"⚠️ [WORKER] Could not query specified shard {shard_name}: {e}")

    # Fallback: hunt across all shards (used by Beat sweeper)
    if not payout:
        logger.info(f"🔍 [WORKER] Hunting for payout {payout_uuid} across all shards...")
        for shard in ['shard_0', 'shard_1']:
            try:
                payout = Payout.objects.using(shard).filter(id=payout_uuid).first()
                if payout:
                    active_shard = shard
                    logger.info(f"🎯 [WORKER] Found payout {payout_uuid} in shard: {shard}")
                    break
            except Exception as e:
                logger.warning(f"⚠️ [WORKER] Could not query {shard}: {e}")
                continue

    if not payout:
        logger.error(f"❌ [WORKER] Payout {payout_id} NOT FOUND in any shard! Check DB connections.")
        return

    # Skip if already in a terminal state
    if payout.status not in ['PENDING', 'PROCESSING']:
        logger.info(f"⏭️ [WORKER] Payout {str(payout_id)[:8]} already in terminal state: {payout.status}")
        return

    payout.attempt_count += 1
    short_id = str(payout_id)[:8]

    # ── Idempotency Loop Breaker ──────────────────────────────────────────────
    # If a payout has been retried too many times without ever advancing past
    # PENDING, it is stuck in an idempotency loop. Force-fail it and release
    # the held funds so the dashboard reflects reality.
    if payout.attempt_count >= MAX_LOOP_ATTEMPTS and payout.status == 'PENDING':
        logger.warning(
            f"🔓 [WORKER] Payout {short_id} stuck in PENDING after {payout.attempt_count} attempts. "
            f"Breaking idempotency loop — force-failing and releasing funds."
        )
        _clear_idempotency_lock(payout)
        with transaction.atomic(using=active_shard):
            payout.transition_to('PROCESSING', using=active_shard)
            payout.transition_to('FAILED', using=active_shard)
            LedgerEntry.objects.using(active_shard).create(
                merchant=payout.merchant,
                amount_paise=payout.amount_paise,
                entry_type='DEBIT_RELEASE',
                description=f'Refund — idempotency loop breaker for payout {payout.id}'
            )
        dispatch_payout_webhook(payout)
        return

    if payout.status == 'PENDING':
        logger.info(f"📥 [WORKER] Starting new payout {short_id}... (Shard: {active_shard})")
        payout.transition_to('PROCESSING', using=active_shard)
    else:
        logger.info(f"🔄 [WORKER] Retrying payout {short_id} (Attempt {payout.attempt_count})...")
        payout.save(using=active_shard, update_fields=['attempt_count', 'updated_at'])

    outcome = random.random()

    if outcome < 0.1:
        logger.info(f"⏳ [WORKER] Payout {short_id} HUNG. Scheduling retry in 30s.")
        process_payout.apply_async((payout_id,), countdown=30)
        return

    elif outcome < 0.8:
        logger.info(f"✅ [WORKER] Payout {short_id} SUCCESS!")
        with transaction.atomic(using=active_shard):
            payout.transition_to('COMPLETED', using=active_shard)
            LedgerEntry.objects.using(active_shard).create(
                merchant=payout.merchant,
                amount_paise=0,
                entry_type='DEBIT_FINAL',
                description=f'Finalized payout {payout.id}'
            )
        dispatch_payout_webhook(payout)

    else:
        logger.info(f"❌ [WORKER] Payout {short_id} FAILED. Reversing funds.")
        with transaction.atomic(using=active_shard):
            payout.transition_to('FAILED', using=active_shard)
            LedgerEntry.objects.using(active_shard).create(
                merchant=payout.merchant,
                amount_paise=payout.amount_paise,
                entry_type='DEBIT_RELEASE',
                description=f'Refund for failed payout {payout.id}'
            )
        dispatch_payout_webhook(payout)


@app.task(name='worker_app.retry_stuck_payouts')
def retry_stuck_payouts():
    """
    Beat task running every 30 seconds. Checks all shards for:
    1. PENDING payouts that never got a task (orphaned).
    2. PROCESSING payouts that are hanging (backoff/retry).
    """
    from django.utils import timezone
    from datetime import timedelta

    now = timezone.now()
    # Shards we need to check (including default just in case)
    shards = ['shard_0', 'shard_1']

    for shard in shards:
        # ── Case 1: Orphaned PENDING payouts ──
        orphaned = Payout.objects.using(shard).filter(
            status='PENDING',
            created_at__lt=now - timedelta(seconds=30),
        )
        for payout in orphaned:
            logger.info(f"🔁 [BEAT] Re-queuing orphaned PENDING payout {str(payout.id)[:8]} (attempt {payout.attempt_count}, shard: {shard})")
            process_payout.delay(str(payout.id))

        # ── Case 2: Stuck PROCESSING payouts ──
        stuck = Payout.objects.using(shard).filter(status='PROCESSING')
        for payout in stuck:
            delay_seconds = 30 * (2 ** payout.attempt_count)
            if payout.updated_at < now - timedelta(seconds=delay_seconds):
                if payout.attempt_count >= 3:
                    logger.warning(f"💀 [BEAT] Payout {str(payout.id)[:8]} exceeded max retries — force-failing.")
                    with transaction.atomic(using=shard):
                        payout.transition_to('FAILED', using=shard)
                        LedgerEntry.objects.using(shard).create(
                            merchant=payout.merchant,
                            amount_paise=payout.amount_paise,
                            entry_type='DEBIT_RELEASE',
                            description=f'Refund for max-retries payout {payout.id}'
                        )
                    dispatch_payout_webhook(payout)
                else:
                    payout.attempt_count += 1
                    payout.save(using=shard)
                    process_payout.delay(str(payout.id))


app.conf.beat_schedule = {
    'retry-stuck-payouts-every-30-seconds': {
        'task': 'worker_app.retry_stuck_payouts',
        'schedule': 30.0,
    },
}
app.conf.timezone = 'UTC'
