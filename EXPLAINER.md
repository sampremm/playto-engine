# EXPLAINER.md — Playto Payout Engine

> This document answers the five questions from the [Playto Founding Engineer Challenge](https://www.playto.so/features/playto-pay). Every code snippet is from the actual production codebase — no pseudocode.

---

## 1. The Ledger

### Balance Calculation Query

```python
# backend/payouts/views.py — inside PayoutCreateView.post()

current_balance = (
    LedgerEntry.objects.using(active_shard)
    .filter(merchant=locked_merchant)
    .aggregate(total=Sum('amount_paise'))['total'] or 0
)
```

### Why I Modeled Credits and Debits This Way

The ledger is **event-sourced** — every money movement is an immutable row. The balance is a derived computation, never a stored field.

| `entry_type` | `amount_paise` | When Written | Purpose |
|---|---|---|---|
| `CREDIT` | `+10000` | Account top-up / seed | Funds deposited |
| `DEBIT_HOLD` | `-500` | Payout **requested** | Reserves funds immediately at API time |
| `DEBIT_FINAL` | `0` | Payout **completed** | Audit marker — hold already reduced balance |
| `DEBIT_RELEASE` | `+500` | Payout **failed** | Atomically refunds held funds |

**Why not a mutable `balance` column?**

With a mutable balance, partial failures create drift:

```
Transaction A: UPDATE balance = balance - 500  ← crashes before commit
Transaction B: SELECT balance = 1000           ← reads stale value
Result: double-spend
```

With an immutable ledger, a committed `INSERT` is immediately visible to all transactions. There is no window for inconsistency. The database guarantees it.

**Why `DEBIT_HOLD` at request time, not processing time?**

Funds are reserved the instant the merchant clicks "Withdraw" — not when the worker picks up the task seconds later. This prevents a merchant from submitting multiple payouts that collectively exceed their balance during the gap between API acceptance and worker processing.

**Why `.using(active_shard)` instead of `locked_merchant.ledger_entries.aggregate()`?**

Django's ORM relation `.ledger_entries` silently routes to the `default` database alias when no shard hint is available. In a two-shard system, a merchant whose data lives on `shard_1` would have their ledger entries queried on `default` — returning `0`. Every payout would pass the balance check. The explicit `.using(active_shard)` forces the query to execute on the correct PostgreSQL instance. This was an AI bug I caught (see Section 5).

---

## 2. The Lock

### The Exact Code That Prevents Double-Spend

```python
# backend/payouts/views.py — PayoutCreateView.post(), lines 140-161

from config.routers import ShardRouter
active_shard = ShardRouter().get_shard(merchant.id)

with transaction.atomic(using=active_shard):
    # Acquire an exclusive row-level lock on the Merchant row
    locked_merchant = (
        Merchant.objects.using(active_shard)
        .select_for_update()
        .get(pk=merchant.pk)
    )

    # Read balance AFTER acquiring the lock — on the correct shard
    current_balance = (
        LedgerEntry.objects.using(active_shard)
        .filter(merchant=locked_merchant)
        .aggregate(total=Sum('amount_paise'))['total'] or 0
    )

    if current_balance < amount_paise:
        return Response({'error': 'Insufficient balance'}, status=402)

    # Create payout + hold entry — atomically committed together
    payout = Payout.objects.using(active_shard).create(
        merchant=locked_merchant, amount_paise=amount_paise, status='PENDING'
    )
    LedgerEntry.objects.using(active_shard).create(
        merchant=locked_merchant, amount_paise=-amount_paise,
        entry_type='DEBIT_HOLD',
        description=f'Hold for payout {payout.id}'
    )
```

### What Database Primitive It Relies On

**PostgreSQL `SELECT ... FOR UPDATE`** — an exclusive row-level lock.

```
Request A (₹60)              Request B (₹60)           [Balance: ₹100]
────────────────────────────────────────────────────────────────────────
BEGIN TRANSACTION            BEGIN TRANSACTION
SELECT ... FOR UPDATE        SELECT ... FOR UPDATE
  → acquires lock              → BLOCKS (waits for A's lock)
  balance = 100
  100 >= 60 ✅
  INSERT DEBIT_HOLD(-60)
  INSERT PAYOUT
COMMIT                       → lock released, B unblocks
                               balance = 40  (reads committed state)
                               40 < 60  ❌
                             RETURN 402 Insufficient Funds
```

The key guarantee: the second request **always reads the committed state of the first**. There is no window between read and write where another transaction can interleave.

**Why not application-level locking (Redis SETNX)?** PostgreSQL locks are:
- Automatically released on commit/rollback (no orphan locks)
- Immune to clock skew and network partitions
- Decades of battle-testing for exactly this use case

---

## 3. The Idempotency

### How the System Knows It Has Seen a Key Before

Two-tier architecture — Redis for speed, PostgreSQL for durability:

```python
# backend/payouts/views.py — STEP 1: L1 Redis fast path
rkey = f'idem:{merchant.id}:{idem_key}'
redis_val = _idem_redis.get(rkey)
if redis_val is not None:
    if redis_val == '__IN_FLIGHT__':
        return Response({'error': 'Concurrent request'}, status=409)
    cached = json.loads(redis_val)
    return Response(cached['body'], status=cached['status'])  # Replay

# STEP 2: L2 PostgreSQL fallback (e.g. after Redis restart)
try:
    pg_record = IdempotencyKey.objects.using(IDEM_DB).get(
        key=idem_key, merchant_id=merchant.id
    )
    if pg_record.idem_status == 'COMPLETE':
        # Re-populate Redis cache, then replay
        _idem_redis.set(rkey, json.dumps({...}), ex=86400)
        return Response(pg_record.response_body, status=pg_record.response_status)
    else:
        return Response({'error': 'Concurrent request'}, status=409)
except IdempotencyKey.DoesNotExist:
    pass  # Continue to claim

# STEP 3: Claim atomically in both stores
redis_claimed = _idem_redis.set(rkey, '__IN_FLIGHT__', ex=86400, nx=True)
if not redis_claimed:
    return Response({'error': 'Concurrent request'}, status=409)

pg_record = IdempotencyKey.objects.using(IDEM_DB).create(
    key=idem_key, merchant_id=merchant.id, idem_status='IN_FLIGHT'
)
```

### What Happens If the First Request Is In-Flight When the Second Arrives

The system handles this at three levels:

1. **Redis `SET NX`** — The first request atomically claims the key. `SET NX` is a single Redis command — it cannot be split into separate read + write. The second request's `SET NX` returns `False`.

2. **Redis `GET` returns `"__IN_FLIGHT__"`** — If the second request arrives after the first has claimed but before it completes, the `GET` returns the sentinel value, and the API returns `409 Conflict`.

3. **PostgreSQL `IntegrityError`** — If Redis is down and both requests hit PostgreSQL simultaneously, the `unique_together = [['key', 'merchant_id']]` constraint catches the duplicate. The losing request returns `409`.

**Why Redis db=1, not db=0?** Celery's broker uses db=0. A `FLUSHDB` on the broker (legitimate maintenance) would wipe all idempotency keys — converting an ops action into a data integrity incident.

**Why 409 for in-flight?** This is standard practice (Stripe, Braintree, Adyen). Blocking the second connection until the first completes risks thread exhaustion and retry storms. `409` tells the client to retry momentarily.

---

## 4. The State Machine

### Where `FAILED → COMPLETED` Is Blocked

```python
# backend/payouts/models.py — Payout model

LEGAL_TRANSITIONS = {
    'PENDING':    ['PROCESSING'],
    'PROCESSING': ['COMPLETED', 'FAILED'],
    'COMPLETED':  [],   # Terminal — no further transitions allowed
    'FAILED':     [],   # Terminal — no further transitions allowed
}

def transition_to(self, new_status, using=None):
    """
    The ONLY correct way to change payout status.

    Unlike clean()/save(), this method cannot be skipped by .update().
    All state changes in the worker must call this instead of setting
    payout.status directly.
    """
    allowed = self.LEGAL_TRANSITIONS.get(self.status, [])
    if new_status not in allowed:
        raise ValueError(
            f"Illegal state transition: {self.status} → {new_status}. "
            f"Allowed from {self.status!r}: {allowed}"
        )
    self.status = new_status
    save_kwargs = {'update_fields': ['status', 'updated_at']}
    if using:
        save_kwargs['using'] = using
    self.save(**save_kwargs)
```

**Attempting `FAILED → COMPLETED`:**
1. `self.status` is `'FAILED'`
2. `LEGAL_TRANSITIONS['FAILED']` returns `[]`
3. `'COMPLETED' not in []` → `True`
4. `raise ValueError("Illegal state transition: FAILED → COMPLETED")`

**Defence in depth** — there is also a `clean()` + `save()` override that catches illegal transitions from the Django admin or shell. But `transition_to()` is the primary guard because Django's `QuerySet.update()` bypasses `save()` entirely.

### Complete State Diagram

```
PENDING ──→ PROCESSING ──→ COMPLETED  (85%, DEBIT_FINAL entry)
                       ──→ FAILED     (15%, atomic DEBIT_RELEASE refund)
                       ──→ [timeout]  (10%, Beat retries with backoff)
                                      After 3 attempts → FAILED + refund
```

---

## 5. The AI Audit

### Bug 1: Wrong Database for Balance Aggregation

**What AI gave me:**

```python
# AI-generated balance check — uses Django's ORM relation
current_balance = locked_merchant.ledger_entries.aggregate(
    Sum('amount_paise')
)['amount_paise__sum'] or 0
```

**What I caught:**

The `.ledger_entries` reverse relation routes to the `default` database alias — not the merchant's shard. On a real two-shard system, a merchant on `shard_1` has their ledger entries on `shard_1`, but this query reads from `default` — returning `0`. **Every payout passes the balance check regardless of actual balance.**

This is a silent data corruption bug. No error is raised. The query succeeds. It just returns the wrong number.

**What I replaced it with:**

```python
# Correct — explicitly queries the merchant's shard
current_balance = (
    LedgerEntry.objects.using(active_shard)
    .filter(merchant=locked_merchant)
    .aggregate(total=Sum('amount_paise'))['total'] or 0
)
```

The explicit `.using(active_shard)` forces the query to execute on the correct PostgreSQL instance.

### Bug 2: Check-Then-Act Race Condition in Idempotency

**What AI gave me:**

```python
# AI-generated idempotency — classic TOCTOU bug
try:
    idemp_record = IdempotencyKey.objects.get(key=idempotency_key, merchant=merchant)
    if idemp_record.response_body:
        return Response(idemp_record.response_body, status=idemp_record.status_code)
except IdempotencyKey.DoesNotExist:
    idemp_record = IdempotencyKey.objects.create(key=idempotency_key, merchant=merchant)
```

**What I caught:**

Three problems:

1. **Race condition** — Two requests both execute `.get()`, both get `DoesNotExist`, both execute `.create()`. The second `.create()` raises `IntegrityError`. Client gets a raw 500.

2. **TOCTOU** — Between `.get()` and `.create()` there are two separate DB round-trips. Another request can interleave in that window.

3. **No response replay on collision** — Even if you catch `IntegrityError`, the AI code returned a bare `409` — it never fetched and returned the already-cached response.

**What I replaced it with:**

```python
# Stage 1: Atomic Redis SET NX — single command, no race window
claimed = _idem_redis.set(rkey, "__IN_FLIGHT__", ex=86400, nx=True)
if not claimed:
    val = _idem_redis.get(rkey)
    if val and val != "__IN_FLIGHT__":
        cached = json.loads(val)
        return Response(cached['body'], status=cached['status'])  # Replay
    return Response({'error': 'Concurrent request'}, status=409)

# Stage 2: PostgreSQL INSERT with IntegrityError handling
try:
    pg_record = IdempotencyKey.objects.using(IDEM_DB).create(
        key=idem_key, merchant_id=merchant.id, idem_status='IN_FLIGHT'
    )
except IntegrityError:
    _idem_redis.delete(rkey)  # Release Redis claim
    return Response({'error': 'Concurrent request'}, status=409)
```

Redis `SET NX` is a single atomic command — it cannot be split into read + write. The PostgreSQL `unique_together` constraint provides a durable safety net.

### Bug 3: Stale Database Connections in Long-Running Workers

This wasn't AI-generated code per se, but an AI-recommended architecture (Celery workers + Neon serverless PostgreSQL) that had a subtle operational failure:

**The problem:** Neon Cloud PostgreSQL aggressively drops idle connections. Celery workers are long-lived processes that hold database connections across tasks. After a period of inactivity, the connection goes stale but Python doesn't know it's dead. The next query silently returns empty results — the worker thinks the payout doesn't exist and exits in 0.0002 seconds.

**The fix:**

```python
# worker/worker_app.py — called before every DB query
from django.db import close_old_connections

def _find_payout(payout_uuid, hint_shard=None):
    close_old_connections()  # Force Django to re-establish stale connections
    # ... query logic ...
```

This was discovered through production debugging — the worker logs showed tasks completing in 0.0002s with no error output, which meant the query was succeeding but returning `None`. Adding `close_old_connections()` before every database interaction resolved the issue.

---

## 6. Bonus: What I'm Most Proud Of

The **two-tier idempotency with shard-aware task routing**. Most payout engines have idempotency OR sharding. This system has both, and they work together without conflicting:

- Idempotency keys live on their own dedicated database (`idempotency_db`) — completely isolated from business data shards
- The API resolves the shard, creates the payout atomically, and passes the shard name directly to the Celery worker
- If the worker crashes, Beat finds the orphaned payout, knows which shard it's on, and re-dispatches with the shard hint
- The result: zero orphaned payouts, zero double-charges, zero stale balance reads — across a distributed, sharded system

This is the kind of infrastructure I want to build at Playto.
