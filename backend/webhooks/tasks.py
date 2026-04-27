"""
Webhook delivery task — the async equivalent of BullMQ's worker.

Lifecycle per delivery:
  QUEUED → PROCESSING → SENT          (happy path)
  QUEUED → PROCESSING → RETRYING      (transient failure, attempts < max)
  QUEUED → PROCESSING → RETRYING → … → FAILED  (max attempts exhausted)

Idempotency guard: if a delivery is already SENT, the task exits immediately.
This prevents duplicate delivery if Celery retries the task after a timeout
but before it received the ACK.
"""
import hashlib
import hmac
import json
import logging
import time

import requests
from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)

# Exponential backoff delays (seconds) per attempt index
BACKOFF = [30, 120, 600]   # 30s, 2min, 10min


@shared_task(name='webhooks.deliver', bind=True, max_retries=0)
def deliver_webhook(self, delivery_id: str, shard: str = 'default'):
    """
    Attempt to POST the webhook payload to the registered endpoint.
    Uses exponential back-off via Celery's ETA scheduling.
    """
    from .models import WebhookDelivery

    # ── Idempotency guard ──────────────────────────────────────────────────
    try:
        delivery = WebhookDelivery.objects.using(shard).get(id=delivery_id)
    except WebhookDelivery.DoesNotExist:
        logger.error(f"WebhookDelivery {delivery_id} not found in {shard} — skipping")
        return

    if delivery.status == WebhookDelivery.STATUS_SENT:
        # Already successfully delivered — do not process again
        logger.info(f"Delivery {delivery_id} already SENT — idempotency guard exit")
        return

    if delivery.status == WebhookDelivery.STATUS_FAILED:
        logger.info(f"Delivery {delivery_id} is FAILED (terminal) — skipping")
        return

    # ── Transition to PROCESSING ───────────────────────────────────────────
    try:
        delivery.transition_to(WebhookDelivery.STATUS_PROCESSING, using=shard)
    except ValueError as e:
        logger.error(f"State transition error for {delivery_id}: {e}")
        return

    delivery.attempt_count += 1
    delivery.save(using=shard, update_fields=['attempt_count'])

    # ── Build the signed request ───────────────────────────────────────────
    endpoint = delivery.endpoint
    payload_bytes = json.dumps(delivery.payload).encode('utf-8')
    headers = {
        'Content-Type': 'application/json',
        'X-Playto-Event': delivery.event_type,
        'X-Playto-Delivery': str(delivery.id),
    }
    if endpoint.secret:
        sig = hmac.new(endpoint.secret.encode(), payload_bytes, hashlib.sha256).hexdigest()
        headers['X-Playto-Signature'] = f"sha256={sig}"

    # ── Attempt the HTTP POST ─────────────────────────────────────────────
    try:
        response = requests.post(
            endpoint.url,
            data=payload_bytes,
            headers=headers,
            timeout=10,
        )
        response.raise_for_status()

        # ✅ Success
        delivery.transition_to(WebhookDelivery.STATUS_SENT, using=shard)
        delivery.last_http_status = response.status_code
        delivery.delivered_at = timezone.now()
        delivery.save(using=shard, update_fields=['last_http_status', 'delivered_at'])
        logger.info(f"Delivery {delivery_id} SENT (attempt {delivery.attempt_count})")

    except Exception as exc:
        # ── Failure path ──────────────────────────────────────────────────
        http_status = getattr(getattr(exc, 'response', None), 'status_code', None)
        delivery.last_http_status = http_status
        delivery.last_error = str(exc)

        if delivery.attempt_count >= delivery.max_attempts:
            # Max attempts exhausted — move to terminal FAILED
            delivery.transition_to(WebhookDelivery.STATUS_FAILED, using=shard)
            delivery.save(using=shard, update_fields=['last_http_status', 'last_error'])
            logger.warning(
                f"Delivery {delivery_id} FAILED after {delivery.attempt_count} attempts: {exc}"
            )
        else:
            # Schedule a retry with exponential backoff
            delivery.transition_to(WebhookDelivery.STATUS_RETRYING, using=shard)
            delivery.save(using=shard, update_fields=['last_http_status', 'last_error'])

            delay = BACKOFF[min(delivery.attempt_count - 1, len(BACKOFF) - 1)]
            deliver_webhook.apply_async(args=[delivery_id], kwargs={'shard': shard}, countdown=delay)
            logger.info(
                f"Delivery {delivery_id} RETRYING in {delay}s "
                f"(attempt {delivery.attempt_count}/{delivery.max_attempts})"
            )


def dispatch_payout_webhook(payout):
    """
    Called after a payout state change (COMPLETED or FAILED).
    Enqueues a delivery for every active endpoint registered by the merchant.
    """
    from .models import WebhookEndpoint, WebhookDelivery

    # Use the shard where the payout (and merchant) already lives
    active_shard = payout._state.db or 'default'

    endpoints = WebhookEndpoint.objects.using(active_shard).filter(
        merchant=payout.merchant, is_active=True
    )
    if not endpoints.exists():
        return

    event_type = (
        'payout.completed' if payout.status == 'COMPLETED' else 'payout.failed'
    )
    payload = {
        'event': event_type,
        'payout_id': str(payout.id),
        'merchant_id': str(payout.merchant.id),
        'amount_paise': payout.amount_paise,
        'status': payout.status,
        'timestamp': timezone.now().isoformat(),
    }

    for endpoint in endpoints:
        delivery = WebhookDelivery.objects.using(active_shard).create(
            endpoint=endpoint,
            event_type=event_type,
            payload=payload,
            status=WebhookDelivery.STATUS_QUEUED,
        )
        # Fire-and-forget — Celery picks it up asynchronously
        deliver_webhook.apply_async(args=[str(delivery.id)], kwargs={'shard': active_shard})
        logger.info(f"Queued {event_type} delivery {delivery.id} → {endpoint.url} (Shard: {active_shard})")
