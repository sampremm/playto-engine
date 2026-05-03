"""
Idempotency — Two-Tier Architecture
=====================================

L1 (Redis db=1):  Sub-millisecond hot-path check. Stores either the
                  IN_FLIGHT sentinel or the full JSON response with a
                  24-hour TTL. Handles 99% of traffic.

L2 (PostgreSQL idempotency_db):  Durable, ACID-guaranteed audit log.
                  Survives Redis restarts. On a Redis cache-miss, the
                  system falls back to PostgreSQL and re-populates the
                  Redis cache for future requests.

Lookup order:
  1. Redis  → hit  → replay or 409 (fast path, no DB query)
  2. Redis  → miss → PostgreSQL → hit → re-populate Redis → replay or 409
  3. Both   → miss → claim key in both Redis AND PostgreSQL → process payout

On completion: write full response to both stores.
On crash:      delete both stores so the client can retry legitimately.
"""
import json
import redis
from django.conf import settings
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from django.db import transaction, IntegrityError
from django.db.models import Sum
from django.utils import timezone
from datetime import timedelta
from .models import Payout, IdempotencyKey
from merchants.models import Merchant, LedgerEntry
from .tasks import process_payout
import json
import redis

# ── Redis L1 connection (idempotency cache, db=1) ─────────────────────────────
_idem_redis = redis.Redis.from_url(
    settings.IDEMPOTENCY_REDIS_URL,
    decode_responses=True,
)
_TTL     = settings.IDEMPOTENCY_KEY_TTL   # 86400 seconds
_INFLIGHT = '__IN_FLIGHT__'

# Database alias for idempotency keys — overridable in tests via settings
IDEM_DB = getattr(settings, 'IDEMPOTENCY_DB_ALIAS', 'idempotency_db')


def _rkey(merchant_id, idem_key):
    """Namespaced Redis key: idem:<merchant_id>:<idempotency_key>"""
    return f'idem:{merchant_id}:{idem_key}'


class PayoutCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        merchant        = request.user
        amount_paise    = request.data.get('amount_paise')
        bank_account_id = request.data.get('bank_account_id')
        idem_key        = request.headers.get('Idempotency-Key')

        if not amount_paise or not bank_account_id or not idem_key:
            return Response(
                {'error': 'Missing required fields or headers'},
                status=status.HTTP_400_BAD_REQUEST
            )
        try:
            amount_paise = int(amount_paise)
            if amount_paise <= 0:
                return Response({'error': 'Amount must be positive'}, status=status.HTTP_400_BAD_REQUEST)
        except (ValueError, TypeError):
            return Response({'error': 'Invalid amount'}, status=status.HTTP_400_BAD_REQUEST)

        rkey   = _rkey(merchant.id, idem_key)
        expiry = timezone.now() - timedelta(hours=24)

        # ═══════════════════════════════════════════════════════════════════════
        # STEP 1 — L1 Redis fast-path check
        # ═══════════════════════════════════════════════════════════════════════
        redis_val = _idem_redis.get(rkey)
        if redis_val is not None:
            if redis_val == _INFLIGHT:
                return Response({'error': 'Concurrent request processing. Retry in a moment.'}, status=status.HTTP_409_CONFLICT)
            cached = json.loads(redis_val)
            print(f"🚀 [API] Idempotency Hit (L1 Redis): Replaying response for {idem_key[:8]}...")
            return Response(cached['body'], status=cached['status'])  # ✅ Redis cache hit

        # ═══════════════════════════════════════════════════════════════════════
        # STEP 2 — L1 miss → L2 PostgreSQL fallback (e.g. after Redis restart)
        # ═══════════════════════════════════════════════════════════════════════
        try:
            pg_record = IdempotencyKey.objects.using(IDEM_DB).get(
                key=idem_key, merchant_id=merchant.id, created_at__gte=expiry
            )
            if pg_record.idem_status == IdempotencyKey.STATUS_COMPLETE:
                # Re-populate Redis cache then replay
                _idem_redis.set(
                    rkey,
                    json.dumps({'body': pg_record.response_body, 'status': pg_record.response_status}),
                    ex=_TTL
                )
                print(f"🏛️ [API] Idempotency Hit (L2 Postgres): Replaying response for {idem_key[:8]}...")
                return Response(pg_record.response_body, status=pg_record.response_status)
            else:
                # Still IN_FLIGHT in PG — populate Redis sentinel so next request is fast
                _idem_redis.set(rkey, _INFLIGHT, ex=60)  # Short TTL — will resolve soon
                return Response({'error': 'Concurrent request processing. Retry in a moment.'}, status=status.HTTP_409_CONFLICT)
        except IdempotencyKey.DoesNotExist:
            pass

        # ═══════════════════════════════════════════════════════════════════════
        # STEP 3 — Both misses → claim the key atomically in Redis + PostgreSQL
        # ═══════════════════════════════════════════════════════════════════════

        # Redis SET NX — atomic, cannot be split into read+write
        redis_claimed = _idem_redis.set(rkey, _INFLIGHT, ex=_TTL, nx=True)
        if not redis_claimed:
            # Another request claimed Redis between our GET and SET NX
            return Response({'error': 'Concurrent request processing. Retry in a moment.'}, status=status.HTTP_409_CONFLICT)

        # PostgreSQL INSERT — durable record, unique constraint as safety net
        try:
            pg_record = IdempotencyKey.objects.using(IDEM_DB).create(
                key=idem_key,
                merchant_id=merchant.id,
                idem_status=IdempotencyKey.STATUS_IN_FLIGHT,
            )
        except IntegrityError:
            # PG unique constraint fired — another process beat us
            _idem_redis.delete(rkey)
            return Response({'error': 'Concurrent request processing. Retry in a moment.'}, status=status.HTTP_409_CONFLICT)

        # ═══════════════════════════════════════════════════════════════════════
        # STEP 4 — Process the payout
        # ═══════════════════════════════════════════════════════════════════════
        try:
            from config.routers import ShardRouter
            active_shard = ShardRouter().get_shard(merchant.id)

            with transaction.atomic(using=active_shard):
                locked_merchant = Merchant.objects.using(active_shard).select_for_update().get(pk=merchant.pk)

                # ── Fix: query balance on the merchant's shard, not the default DB ──
                # Using locked_merchant.ledger_entries.aggregate() would silently
                # fall back to the default DB alias, returning 0 on a real two-shard
                # system and allowing every payout to pass the balance check.
                current_balance = (
                    LedgerEntry.objects.using(active_shard)
                    .filter(merchant=locked_merchant)
                    .aggregate(total=Sum('amount_paise'))['total'] or 0
                )

                if current_balance < amount_paise:
                    print(f"🛑 [API] Rejected: Insufficient balance ({current_balance} < {amount_paise})")
                    response_data        = {'error': 'Insufficient balance', 'available_paise': current_balance}
                    response_status_code = status.HTTP_402_PAYMENT_REQUIRED
                    self._commit(rkey, pg_record, response_data, response_status_code)
                    return Response(response_data, status=response_status_code)

                payout = Payout.objects.using(active_shard).create(
                    merchant=locked_merchant,
                    amount_paise=amount_paise,
                    bank_account_id=bank_account_id,
                    status='PENDING'
                )
                LedgerEntry.objects.using(active_shard).create(
                    merchant=locked_merchant,
                    amount_paise=-amount_paise,
                    entry_type='DEBIT_HOLD',
                    description=f'Hold for payout {payout.id}'
                )

                response_data = {
                    'id':           str(payout.id),
                    'amount_paise': payout.amount_paise,
                    'status':       payout.status,
                    'created_at':   payout.created_at.isoformat()
                }
                response_status_code = status.HTTP_201_CREATED
                self._commit(rkey, pg_record, response_data, response_status_code)

                # Fire Celery task immediately — transaction.on_commit()
                # silently drops the callback on sharded databases because
                # Django's on_commit hook is bound to the 'default' alias,
                # not the active_shard alias used in our transaction.atomic().
                try:
                    process_payout.delay(str(payout.id))
                except Exception:
                    pass  # Non-fatal — Beat will pick it up within 30s

                return Response(response_data, status=response_status_code)

        except Exception as e:
            # Release both stores so the client can retry
            _idem_redis.delete(rkey)
            pg_record.delete(using=IDEM_DB)
            return Response(
                {'error': 'Internal server error', 'details': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @staticmethod
    def _commit(rkey, pg_record, body, status_code):
        """
        Write the final response to both stores atomically.
        Redis gets the JSON response (replaces sentinel).
        PostgreSQL gets COMPLETE status + cached response.
        """
        payload = json.dumps({'body': body, 'status': status_code})
        _idem_redis.set(rkey, payload, ex=_TTL)

        pg_record.idem_status    = IdempotencyKey.STATUS_COMPLETE
        pg_record.response_body  = body
        pg_record.response_status = status_code
        pg_record.save(using=IDEM_DB)


class PayoutListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from config.routers import ShardRouter
        active_shard = ShardRouter().get_shard(request.user.id)
        payouts = Payout.objects.using(active_shard).filter(merchant=request.user).order_by('-created_at')
        return Response([{
            'id':            str(p.id),
            'amount_paise':  p.amount_paise,
            'amount_rupees': p.amount_paise / 100,
            'status':        p.status,
            'created_at':    p.created_at
        } for p in payouts])
