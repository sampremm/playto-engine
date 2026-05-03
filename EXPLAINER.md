# Playto Payout Engine — Architecture Deep Dive (EXPLAINER)

---

## 1. The Ledger — Immutable Accounting

### Balance Calculation Query

```python
current_balance = (
    LedgerEntry.objects.using(active_shard)
    .filter(merchant=locked_merchant)
    .aggregate(total=Sum('amount_paise'))['total'] or 0
)
```

### Why This Design?

This is an **event-sourced ledger** — the canonical pattern used by every major payment company (Stripe, Braintree, Wise). The key insight is that **a balance is not a fact — it is a derived computation**.

Instead of storing a mutable `balance` column and updating it on every transaction, every money movement is stored as an immutable `LedgerEntry` row. The current balance is always derived by summing all entries.

#### Entry Types and Their Meaning

| `entry_type` | `amount_paise` | When Written | Why |
|---|---|---|---|
| `CREDIT` | `+10000` | On account top-up / seed | Funds deposited |
| `DEBIT_HOLD` | `-500` | When payout is **requested** | Reserves funds immediately so balance never over-promises |
| `DEBIT_FINAL` | `0` | When payout **completes** | No-op — hold already reduced the balance. This entry exists only as an audit marker |
| `DEBIT_RELEASE` | `+500` | When payout **fails** | Atomically refunds the held amount back into the spendable balance |

#### Why Not a Mutable Balance Column?

Consider this failure scenario with a mutable balance:

```
Transaction A: SELECT balance = 1000
Transaction A: UPDATE balance = balance - 500  ← crashes here
Transaction B: SELECT balance = 1000  ← reads stale, undeducted value
Transaction B: Payout of 500 approved  ← double spend!
```

With an immutable ledger, this is impossible:

```
Transaction A: INSERT LedgerEntry(-500)  ← atomic, either committed or not
Transaction B: SUM(ledger_entries) = 500  ← sees correct value post-commit
Transaction B: 402 Insufficient Funds  ← correctly rejected
```

There is no window for inconsistency. The database guarantees that a committed `INSERT` is immediately visible to other transactions. A partial write is impossible.

#### The `.using(active_shard)` Clause

This is critical. Using `locked_merchant.ledger_entries.aggregate()` via the Django ORM relation silently routes to the **default** database alias. In a two-shard system where a merchant's ledger lives on `shard_1`, this query returns `0` — and every payout passes the balance check regardless of actual balance. The explicit `.using(active_shard)` ensures the query always executes on the correct PostgreSQL instance.

---

## 2. The Lock — Preventing Double-Spend

### The Exact Locking Code

```python
from config.routers import ShardRouter
active_shard = ShardRouter().get_shard(merchant.id)

with transaction.atomic(using=active_shard):
    # Acquire an exclusive row-level lock on the Merchant row
    locked_merchant = Merchant.objects.using(active_shard).select_for_update().get(pk=merchant.pk)

    # Read balance from the correct shard AFTER acquiring the lock
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
        merchant=locked_merchant, amount_paise=-amount_paise, entry_type='DEBIT_HOLD'
    )
```

### The Database Primitive: `SELECT ... FOR UPDATE`

PostgreSQL's row-level lock works as follows:

```
Request A (₹60)         Request B (₹60)        [Balance: ₹100]
─────────────────────────────────────────────────────────────
BEGIN TRANSACTION       BEGIN TRANSACTION
SELECT ... FOR UPDATE   SELECT ... FOR UPDATE
  → acquires lock         → BLOCKS (waits for A)
  balance = 100
  100 >= 60 ✅
  INSERT DEBIT_HOLD(-60)
  INSERT PAYOUT
COMMIT                  → lock released, B unblocks
                          balance = 40  (reads new state)
                          40 < 60  ❌
                        RETURN 402 Insufficient Funds
```

The key guarantee: **the second request always reads the committed state of the first request**. There is no window between the read and the write where another transaction can interleave.

### Why Not Application-Level Locking?

Alternatives like Redis `SETNX` locks for application-level mutual exclusion have failure modes:
- Process crash without releasing the lock → deadlock
- Clock skew between nodes → premature lock expiry
- Network partition → split-brain

`SELECT FOR UPDATE` delegates the locking responsibility entirely to PostgreSQL, which has decades of battle-testing for exactly this use case and automatically releases locks on transaction commit/rollback.

---

## 3. The Idempotency — Two-Tier Architecture

### How the System Knows It Has Seen a Key Before

The system uses a **two-tier idempotency store**:

```
Incoming POST /api/v1/payouts/  [Idempotency-Key: uuid-xyz]
        │
        ▼
┌─────────────────────────────────────────────────────┐
│  L1: Redis db=1  (sub-millisecond, 99% of traffic)  │
│  Key: idem:<merchant_id>:<uuid>                     │
│  TTL: 24 hours                                      │
└──────────────┬──────────────────────────────────────┘
               │ Miss (Redis evicted / restarted)
               ▼
┌─────────────────────────────────────────────────────┐
│  L2: PostgreSQL idempotency_db  (ACID, crash-safe)  │
│  Table: payouts_idempotencykey                      │
│  Columns: key, merchant_id, idem_status,            │
│           response_body, response_status            │
└──────────────┬──────────────────────────────────────┘
               │ Miss → Process new payout
               ▼
         Claim both stores → Create payout → Commit to both
```

**Why two tiers?**
- Redis is fast but volatile — a restart wipes all keys
- PostgreSQL is durable but ~1ms per query vs ~0.1ms for Redis
- L1 handles 99% of traffic at minimal latency
- L2 provides crash recovery — if Redis is lost, responses are replayed from PostgreSQL

**Why Redis db=1, not db=0?**
The Celery broker lives on db=0. A `FLUSHDB` on the broker (legitimate operational action) would simultaneously evict all idempotency keys — converting a maintenance action into a data integrity incident. Isolation prevents this.

### The `SET NX` Primitive

```python
rkey = f"idem:{merchant.id}:{idempotency_key}"
claimed = _idem_redis.set(rkey, "__IN_FLIGHT__", ex=86400, nx=True)
```

`SET NX` (Set if Not eXists) is a **single atomic Redis command** — it cannot be split into separate `GET` and `SET` operations. This is critical:

```
Without SET NX (broken):           With SET NX (correct):
──────────────────────────         ────────────────────────
A: GET key → None                  A: SET NX → True (owns it)
B: GET key → None                  B: SET NX → False (blocked)
A: SET key → "__IN_FLIGHT__"       → No race possible
B: SET key → "__IN_FLIGHT__"  ← both "win", two payouts created!
```

### What Happens at Each Stage

| Scenario | Redis `GET` | Redis `SET NX` | Action |
|---|---|---|---|
| First request | `None` | `True` | Claims key, processes payout |
| Second (in-flight) | `"__IN_FLIGHT__"` | — | Returns `409 Conflict` |
| Second (after completion) | `{"body": ..., "status": 201}` | — | Replays exact original response |
| After Redis restart (L2 hit) | `None` (evicted) | — | PostgreSQL fallback, re-populates L1 |
| Crash during processing | — | — | Both stores deleted, client retries legitimately |

### On Returning 409 for In-Flight Requests

Returning `409 Conflict` for genuinely concurrent in-flight requests is **standard industry practice** — used by Stripe, Braintree, and Adyen. The alternative — blocking the second connection until the first request completes — risks:

- Long-held database connections under load
- Client timeouts cascading into retry storms
- Server thread exhaustion

The `409` tells the client: "I know about your key, processing is underway, retry in a moment." On retry, the client receives the full cached `201` response. This satisfies **eventual idempotency** — the correct semantics for distributed systems.

---

## 4. The State Machine — Transition Guards

### Where Illegal Transitions Are Blocked

The state machine is enforced at **two independent layers**. This is intentional defence-in-depth.

#### Layer 1: `LEGAL_TRANSITIONS` + `transition_to()` (Primary)

```python
# payouts/models.py

LEGAL_TRANSITIONS = {
    'PENDING':    ['PROCESSING'],
    'PROCESSING': ['COMPLETED', 'FAILED'],
    'COMPLETED':  [],   # Terminal — no further transitions
    'FAILED':     [],   # Terminal — no further transitions
}

def transition_to(self, new_status, using=None):
    """
    The ONLY correct way to change payout status in production code.

    Unlike clean()/save(), this method cannot be bypassed by
    Django's QuerySet.update() — which skips save() entirely.
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

#### Layer 2: `clean()` + `save()` override (Secondary / Admin Guard)

```python
def clean(self):
    """Fallback guard for admin panel, shell usage, and any code that
    calls save() directly without going through transition_to()."""
    if self.pk:
        old_status = Payout.objects.get(pk=self.pk).status
        allowed = self.LEGAL_TRANSITIONS.get(old_status, [])
        if old_status != self.status and self.status not in allowed:
            raise ValueError(f"Illegal state transition from {old_status} to {self.status}")

def save(self, *args, **kwargs):
    self.clean()
    super().save(*args, **kwargs)
```

### Why `transition_to()` Is Necessary (The `.update()` Problem)

Django's `QuerySet.update()` executes a direct `UPDATE` SQL statement, completely bypassing the Python model layer:

```python
# This bypasses save() AND clean() — the guard is invisible to .update()
Payout.objects.filter(id=payout_id).update(status='COMPLETED')
```

Any code using `.update()` — including third-party libraries, Django admin actions, or a developer's quick fix — would silently skip the transition check. By mandating `transition_to()` in the worker and documenting it as the required API, the guard is enforced at the **call site** rather than relying on Python method dispatch.

### Complete State Diagram

```
                    ┌─────────────────────────┐
                    │         PENDING         │
                    │  (Created by API view)  │
                    └───────────┬─────────────┘
                                │ transition_to('PROCESSING')
                                ▼
                    ┌─────────────────────────┐
                    │       PROCESSING        │
                    │  (Worker picked up)     │
                    └───┬─────────────────┬───┘
                        │                 │
          70% success   │                 │  20% failure
  transition_to('COMPLETED')   transition_to('FAILED')
                        │                 │
                        ▼                 ▼
           ┌────────────────┐   ┌──────────────────┐
           │   COMPLETED    │   │     FAILED       │
           │ DEBIT_FINAL    │   │ DEBIT_RELEASE    │
           │ (terminal)     │   │ (terminal)       │
           └────────────────┘   └──────────────────┘

10% — PROCESSING but no terminal transition:
Beat task sees updated_at < now - backoff_delay → requeues
After 3 attempts → forced to FAILED + DEBIT_RELEASE
```

---

## 5. The AI Audit — Catching Subtle Bugs

### What AI Gave Me (Broken Idempotency)

```python
# AI-generated — contains a check-then-act race condition
try:
    idemp_record = IdempotencyKey.objects.get(key=idempotency_key, merchant=merchant)
    if idemp_record.response_body:
        return Response(idemp_record.response_body, status=idemp_record.status_code)
except IdempotencyKey.DoesNotExist:
    idemp_record = IdempotencyKey.objects.create(key=idempotency_key, merchant=merchant)
```

### What I Caught

**Race condition 1 — Concurrent first requests:**
Two requests with the same key arrive simultaneously. Both execute `.get()`, both get `DoesNotExist`, both execute `.create()`. The second `.create()` raises an unhandled `IntegrityError` (unique constraint violation). The client receives a raw 500, not an idempotency replay.

**Race condition 2 — TOCTOU (Time of Check, Time of Use):**
There is a window between the `.get()` and the `.create()` where a third request could interleave. These are two separate database round-trips, not one atomic operation.

**Bug 3 — No response replay on IntegrityError:**
Even with a `try/except IntegrityError`, the original code just returned a bare 409 — it didn't fetch and return the already-cached response. The client has no way to know if their request actually succeeded.

### What I Replaced It With (Fix in Three Stages)

**Stage 1 — Handle IntegrityError with response replay:**
```python
except IdempotencyKey.DoesNotExist:
    try:
        idemp_record = IdempotencyKey.objects.create(
            key=idempotency_key, merchant=merchant
        )
    except IntegrityError:
        # Another process beat us — fetch and replay their response
        try:
            existing = IdempotencyKey.objects.get(
                key=idempotency_key, merchant=merchant
            )
            return Response(existing.response_body, status=existing.response_status)
        except IdempotencyKey.DoesNotExist:
            return Response({'error': 'Concurrent request'}, status=409)
```

**Stage 2 — Replace `.get()` + `.create()` with atomic Redis `SET NX`:**
```python
# Single atomic operation — no race window possible
claimed = _idem_redis.set(rkey, "__IN_FLIGHT__", ex=86400, nx=True)
if not claimed:
    val = _idem_redis.get(rkey)
    if val and val != "__IN_FLIGHT__":
        cached = json.loads(val)
        return Response(cached['body'], status=cached['status'])  # Replay
    return Response({'error': 'Concurrent request'}, status=409)
```

**Stage 3 (Production) — Two-tier L1/L2 with PostgreSQL fallback:**
The full production implementation adds a PostgreSQL `idempotency_db` as an ACID-guaranteed fallback when Redis is evicted or restarted, combining the speed of Redis with the durability of PostgreSQL.

### The Other Bug I Caught: Wrong Aggregation DB

AI-generated balance check:
```python
# Wrong — uses default DB, not the merchant's shard
current_balance = locked_merchant.ledger_entries.aggregate(
    Sum('amount_paise')
)['amount_paise__sum'] or 0
```

The `.ledger_entries` reverse relation uses Django's ORM routing, which defaults to the `default` database alias when no hint is provided. On a real two-shard system, a merchant on `shard_1` would have their ledger entries on `shard_1`, but this query reads from `default` — returning `0`. Every payout would pass the balance check.

**Fixed version:**
```python
# Correct — explicitly queries the merchant's shard
current_balance = (
    LedgerEntry.objects.using(active_shard)
    .filter(merchant=locked_merchant)
    .aggregate(total=Sum('amount_paise'))['total'] or 0
)
```

---

## 6. End-to-End Flow Summary

```
Client: POST /api/v1/payouts/
        Headers: Authorization: Bearer <jwt>
                 Idempotency-Key: <uuid>
        Body:    {"amount_paise": 5000, "bank_account_id": "ACC123"}

API Gateway:
  1. Authenticate JWT → resolve merchant
  2. L1: Redis GET → miss
  3. L2: PostgreSQL GET → miss
  4. Redis SET NX → claimed
  5. PostgreSQL INSERT IdempotencyKey (IN_FLIGHT)
  6. Determine active_shard = ShardRouter().get_shard(merchant.id)
  7. BEGIN TRANSACTION on active_shard
  8. SELECT Merchant FOR UPDATE → acquire row lock
  9. SUM(LedgerEntry on active_shard) → current_balance = 12000
  10. 12000 >= 5000 ✅
  11. INSERT Payout (PENDING)
  12. INSERT LedgerEntry (DEBIT_HOLD, -5000)
  13. Commit response to Redis + PostgreSQL idempotency stores
  14. COMMIT TRANSACTION
  15. transaction.on_commit → try process_payout.delay()
  16. Return 201 Created {payout_id, status: PENDING}

Celery Worker (within 30s):
  1. Receive process_payout task
  2. Find payout across all shards
  3. transition_to('PROCESSING')
  4. Simulate bank (random outcome)
  5a. SUCCESS (70%):
      transition_to('COMPLETED')
      INSERT LedgerEntry (DEBIT_FINAL, 0)
      COMMIT
      dispatch_payout_webhook(payout)
  5b. FAILURE (20%):
      transition_to('FAILED')
      INSERT LedgerEntry (DEBIT_RELEASE, +5000)
      COMMIT
      dispatch_payout_webhook(payout)
  5c. HANG (10%):
      Worker schedules automatic retry: apply_async(countdown=30)
      Beat task also monitors with exponential backoff
      After 3 attempts → FAILED + DEBIT_RELEASE

Webhook Engine:
  1. Find all active WebhookEndpoints for merchant
  2. Create WebhookDelivery (QUEUED)
  3. POST JSON payload + X-Playto-Signature (HMAC-SHA256)
  4. On success → SENT (terminal, idempotency guard prevents re-delivery)
  5. On failure → RETRYING → exponential backoff (30s, 60s, 120s)
  6. After 3 failures → FAILED (terminal)

Client Dashboard (polling):
  • Hosted on Vercel (playto-engine-vert.vercel.app)
  • API calls to playtopay.duckdns.org (EC2 + Nginx + Docker)
  • vercel.json rewrites handle SPA routing (no 404 on refresh)
  • Every 60s: fetch balance + payout list
  • When on Webhooks tab: fetch every 10s
  • Payout status updates: PENDING → COMPLETED/FAILED
  • Delivery history: QUEUED → PROCESSING → SENT/FAILED
```

---

## 7. Production Operations

### Clearing Stuck Payouts

If payouts are stuck in `PENDING` or `PROCESSING` due to idempotency lockout (e.g. after a Redis restart or worker crash during processing), they can be manually resolved:

```bash
# Force-complete stuck payouts on each shard
docker compose exec backend python manage.py shell --command="
from payouts.models import Payout
for shard in ['shard_0', 'shard_1']:
    count = Payout.objects.using(shard).filter(status__in=['PENDING', 'PROCESSING']).update(status='FAILED')
    print(f'Cleared {count} payouts in {shard}')
"
```

### Full Reset (Wipe All Data)

```bash
docker compose down -v     # -v removes database volumes
docker compose up -d --build
```

This destroys all data and re-seeds fresh demo merchants. Use only when starting from scratch.

### Deployment Checklist

| Step | Command / Action |
|---|---|
| Push code | `git push origin main` |
| Vercel frontend | Auto-deploys on push |
| EC2 backend | `git pull && docker compose up -d --build` |
| Verify worker | `docker compose logs -f worker` |
| Verify backend | `docker compose logs -f backend` |
| Clear browser cache | Hard refresh + clear Local Storage |
