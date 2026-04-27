import os
import random
from celery import Celery

# Setup Django before importing models
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
import django
django.setup()

from django.db import transaction
from payouts.models import Payout
from merchants.models import LedgerEntry
from webhooks.tasks import dispatch_payout_webhook

app = Celery('worker_app', broker=os.environ.get('REDIS_URL', 'redis://localhost:6379/0'))

import logging
logger = logging.getLogger('payouts')


@app.task(name='worker_app.process_payout', bind=True, max_retries=3)
def process_payout(self, payout_id):
    payout = None
    active_shard = 'default'
    
    # Iterate shards to find this specific payout
    for shard in ['default', 'shard_0', 'shard_1']:
        try:
            payout = Payout.objects.using(shard).get(id=payout_id)
            active_shard = shard
            break
        except Payout.DoesNotExist:
            continue

    if not payout:
        return

    # Skip if already in a terminal state
    if payout.status not in ['PENDING', 'PROCESSING']:
        return

    payout.attempt_count += 1
    short_id = str(payout_id)[:8]
    
    if payout.status == 'PENDING':
        logger.info(f"📥 [WORKER] Starting new payout {short_id}... (Shard: {active_shard})")
        payout.transition_to('PROCESSING', using=active_shard)
    else:
        logger.info(f"🔄 [WORKER] Retrying payout {short_id} (Attempt {payout.attempt_count})...")
        payout.save(using=active_shard, update_fields=['attempt_count', 'updated_at'])

    outcome = random.random()

    if outcome < 0.1:
        logger.info(f"⏳ [WORKER] Payout {short_id} HUNG. Will retry later.")
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
    from django.conf import settings

    now = timezone.now()
    # Shards we need to check (including default just in case)
    shards = ['default', 'shard_0', 'shard_1']

    for shard in shards:
        # ── Case 1: Orphaned PENDING payouts ──
        orphaned = Payout.objects.using(shard).filter(
            status='PENDING',
            created_at__lt=now - timedelta(seconds=30),
        )
        for payout in orphaned:
            process_payout.delay(str(payout.id))

        # ── Case 2: Stuck PROCESSING payouts ──
        stuck = Payout.objects.using(shard).filter(status='PROCESSING')
        for payout in stuck:
            delay_seconds = 30 * (2 ** payout.attempt_count)
            if payout.updated_at < now - timedelta(seconds=delay_seconds):
                if payout.attempt_count >= 3:
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
