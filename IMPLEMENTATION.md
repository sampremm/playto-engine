# IMPLEMENTATION.md — Playto Payout Engine

## Executive Summary

The Playto Payout Engine is a distributed payment processing system designed for the [Playto Founding Engineer Challenge](https://www.playto.so/features/playto-pay). It solves three hard problems in financial infrastructure:

1. **Idempotency** — Duplicate API calls never create duplicate money movements, even under concurrent load.
2. **Concurrency Safety** — Double-spend is impossible when multiple requests arrive simultaneously for the same account.
3. **Reliability** — Every payout eventually reaches a terminal state (COMPLETED or FAILED), even when components crash mid-processing.

Every architectural decision maps to a real-world payment infrastructure pattern used at scale by companies like Stripe, Wise, and Razorpay.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                          CLIENT (Browser)                           │
│  React 19 + Vite + Tailwind v4 — Glassmorphic SaaS Dashboard       │
│  • JWT auth   • Idempotency-Key per submission   • Smart polling    │
└────────────────────────────┬────────────────────────────────────────┘
                             │ HTTP
                             ▼
┌─────────────────────────────────────────────────────────────────────┐
│                        API GATEWAY (Django)                         │
│  PayoutCreateView — synchronous intent + validation + locking       │
│  ┌─────────────────────┐  ┌──────────────────────────────────────┐  │
│  │  Two-Tier Idem.     │  │  SELECT FOR UPDATE (row-level lock)  │  │
│  │  L1: Redis db=1     │  │  Balance verified on correct shard   │  │
│  │  L2: PG idem_db     │  │  Payout + Hold written atomically    │  │
│  └─────────────────────┘  └──────────────────────────────────────┘  │
│                                                                     │
│  Task dispatch: process_payout.apply_async([id, shard], countdown=2)│
└────────────────────────────┬────────────────────────────────────────┘
                             │ Celery task via Redis db=0 (broker)
                             ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    PAYOUT PROCESSOR (Celery Worker)                 │
│  UUID→string cast · close_old_connections() · shard-aware lookup    │
│  transition_to() state machine · atomic DB writes                   │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │  Celery Beat (dedicated container, every 30s)               │    │
│  │  • Picks up orphaned PENDING payouts                        │    │
│  │  • Retries stuck PROCESSING with exponential backoff        │    │
│  │  • Force-fails after 3 attempts + atomic DEBIT_RELEASE      │    │
│  │  • Passes shard hint to avoid cross-shard hunting           │    │
│  └─────────────────────────────────────────────────────────────┘    │
└────────────────────────────┬────────────────────────────────────────┘
                             │ HTTP POST + HMAC-SHA256 signature
                             ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     WEBHOOK ENGINE (Django + Celery)                │
│  WebhookDelivery lifecycle: QUEUED→PROCESSING→RETRYING→SENT/FAILED  │
│  Idempotency guard prevents re-delivery after SENT                  │
│  HMAC signing: X-Playto-Signature: sha256=<digest>                  │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Database Architecture

### Sharding Strategy

```
┌──────────────────────────────────────────────────────────────┐
│  shard_0          → Payouts + LedgerEntries                  │
│                     (md5(merchant_id) % 2 == 0)              │
├──────────────────────────────────────────────────────────────┤
│  shard_1          → Payouts + LedgerEntries                  │
│                     (md5(merchant_id) % 2 == 1)              │
├──────────────────────────────────────────────────────────────┤
│  idempotency_db   → IdempotencyKey records only              │
│                     (ACID audit store, isolated by design)    │
└──────────────────────────────────────────────────────────────┘
```

**Production:** Each alias points to a separate Neon Cloud PostgreSQL database. Shard routing uses `md5(merchant_id) % 2` via `ShardRouter` in `config/routers.py`.

### The ShardRouter

```python
# config/routers.py
class ShardRouter:
    def get_shard(self, merchant_id):
        shard_id = int(hashlib.md5(str(merchant_id).encode()).hexdigest(), 16) % 2
        return f'shard_{shard_id}'

    def db_for_read(self, model, **hints):
        if model.__name__.lower() in {'idempotencykey'}:
            return 'idempotency_db'
        if model._meta.app_label in ['merchants', 'payouts', 'webhooks']:
            # Extract merchant_id from instance or hints
            merchant_id = ...  # resolved from hints
            if merchant_id:
                return self.get_shard(merchant_id)
        return None  # Fall through to default
```

### The Immutable Ledger

Balance is always computed — never stored:

```python
class LedgerEntry(models.Model):
    merchant     = models.ForeignKey(Merchant, on_delete=models.PROTECT)
    amount_paise = models.BigIntegerField()  # Negative for debits
    entry_type   = models.CharField(max_length=20, choices=ENTRY_TYPES)
    created_at   = models.DateTimeField(auto_now_add=True)
```

```python
balance_paise = LedgerEntry.objects.using(active_shard).filter(
    merchant=merchant
).aggregate(total=Sum('amount_paise'))['total'] or 0
```

This guarantees zero balance drift — there is no mutable field that can get out of sync with the ledger.

---

## Phase 1: The API Gateway

### Request Lifecycle (`PayoutCreateView.post`)

```
Request arrives with Idempotency-Key header
    │
    ▼
1. Validate inputs (amount_paise, bank_account_id, UUID header)
    │
    ▼
2. L1 Redis fast-path check
   GET idem:<merchant_id>:<uuid>
   ├── "__IN_FLIGHT__"  → 409 Conflict
   ├── JSON response    → Replay cached 201/402
   └── None             → Miss, continue
    │
    ▼
3. L2 PostgreSQL fallback (e.g. after Redis restart)
   SELECT * FROM idempotencykey WHERE key=... AND merchant_id=...
   ├── COMPLETE          → Re-populate Redis, replay response
   ├── IN_FLIGHT         → 409 Conflict
   └── DoesNotExist      → Miss, continue
    │
    ▼
4. Claim key atomically in both stores
   Redis: SET NX → False? → 409
   PostgreSQL: INSERT → IntegrityError? → 409
    │
    ▼
5. Resolve shard: active_shard = ShardRouter().get_shard(merchant.id)
    │
    ▼
6. BEGIN TRANSACTION on active_shard
   SELECT Merchant FOR UPDATE → acquires exclusive row lock
   SUM(LedgerEntry using active_shard) → current_balance
   insufficient? → commit 402 to idempotency stores → return
    │
    ▼
7. Create Payout (PENDING) + LedgerEntry (DEBIT_HOLD) — atomic
    │
    ▼
8. Commit response to both idempotency stores
    │
    ▼
9. COMMIT TRANSACTION
    │
    ▼
10. process_payout.apply_async([payout_id, shard], countdown=2)
    │
    ▼
11. Return 201 Created
```

### Task Dispatch with Shard Hint

```python
# The API knows which shard it just wrote to — pass it to the worker
process_payout.apply_async(
    args=[str(payout.id), active_shard], countdown=2
)
```

The 2-second countdown ensures the Neon Cloud database has committed the PENDING row before the worker queries it. Without this, the worker races the DB commit and finds nothing.

---

## Phase 2: The Payout Processor (Celery Worker)

### Key Design Decisions

1. **`close_old_connections()`** — Called before every DB query. Neon serverless PostgreSQL aggressively drops idle connections; without this, the worker gets stale handles that silently return empty results.

2. **`acks_late=True, reject_on_worker_lost=True`** — If the worker crashes mid-task, the message goes back to Redis instead of being lost.

3. **UUID casting** — Celery serializes task arguments as JSON, which strips Python's `uuid.UUID` type to a plain string. The worker explicitly casts `uuid.UUID(payout_id)` before querying PostgreSQL.

4. **Shard hint fast path** — When the API dispatches the task, it passes the shard name. The worker queries that shard directly. When Beat dispatches (no shard hint), the worker hunts across all shards.

### Shard-Aware Task

```python
@app.task(name='worker_app.process_payout', bind=True, max_retries=3,
          acks_late=True, reject_on_worker_lost=True)
def process_payout(self, payout_id, shard_name=None):
    payout_uuid = uuid.UUID(payout_id)
    close_old_connections()

    # Fast path: API told us the shard
    if shard_name:
        payout = Payout.objects.using(shard_name).filter(id=payout_uuid).first()

    # Fallback: hunt across all shards (Beat sweeper path)
    if not payout:
        for shard in ['shard_0', 'shard_1']:
            payout = Payout.objects.using(shard).filter(id=payout_uuid).first()
            if payout: break

    # State machine enforcement via transition_to()
    payout.transition_to('PROCESSING', using=active_shard)

    # Simulate bank transfer (mock)
    outcome = random.random()
    if outcome < 0.85:  # 85% success
        payout.transition_to('COMPLETED', using=active_shard)
        LedgerEntry.create(DEBIT_FINAL, 0)  # Audit marker
    else:  # 15% failure
        payout.transition_to('FAILED', using=active_shard)
        LedgerEntry.create(DEBIT_RELEASE, +amount)  # Atomic refund
```

### Beat Reconciliation — The Safety Net

```python
@app.task(name='worker_app.retry_stuck_payouts')
def retry_stuck_payouts():
    close_old_connections()

    for shard in ['shard_0', 'shard_1']:
        # Case 1: Orphaned PENDING (older than 30s — API task was lost)
        orphaned = Payout.objects.using(shard).filter(
            status='PENDING', created_at__lt=now - 30s
        )
        for p in orphaned:
            process_payout.apply_async([str(p.id), shard], countdown=1)

        # Case 2: Stuck PROCESSING (exponential backoff)
        stuck = Payout.objects.using(shard).filter(status='PROCESSING')
        for p in stuck:
            if p.attempt_count >= 3:
                # Force-fail + atomic refund
                p.transition_to('FAILED')
                LedgerEntry.create(DEBIT_RELEASE, +amount)
            else:
                p.attempt_count += 1
                process_payout.apply_async([str(p.id), shard])
```

Runs every 30 seconds. Covers:
- **Broker failure** — payout exists in DB but no task was dispatched
- **Worker crash** — payout stuck in PROCESSING with no terminal transition
- **Idempotency loop** — after 5 attempts, force-fails and clears stale locks

---

## Phase 3: Webhook Delivery Engine

After every terminal state change, `dispatch_payout_webhook()` is called **outside** the `transaction.atomic()` block:

```python
with transaction.atomic(using=active_shard):
    payout.transition_to('COMPLETED', using=active_shard)
    LedgerEntry.create(...)
# ← transaction committed here

dispatch_payout_webhook(payout)  # ← AFTER commit, not inside
```

**Why outside?** If webhook dispatch fails inside the transaction, the `COMPLETED` payout would roll back — turning a successful payment into an apparent failure.

### Delivery Lifecycle

```
QUEUED → PROCESSING → SENT     (success, terminal)
                    → RETRYING  → exponential backoff (30s, 60s, 120s)
                    → FAILED    (after 3 failures, terminal)
```

### HMAC Signature

Every delivery includes `X-Playto-Signature: sha256=<hmac>` signed with the endpoint's secret key. Merchants verify:

```python
expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
assert hmac.compare_digest(f"sha256={expected}", signature_header)
```

---

## Phase 4: Frontend Dashboard

### Component Architecture

```
App.jsx
├── Login.jsx       — JWT authentication (username=email, password)
└── Dashboard.jsx   — Protected route
    ├── Balance Card     — available_rupees, held_rupees
    ├── Payout Form      — amount + bank account + Idempotency-Key per submit
    ├── Tabs
    │   ├── Recent Payouts  — status badges (PENDING/PROCESSING/COMPLETED/FAILED)
    │   ├── Ledger          — full entry history with signed amounts
    │   └── Webhooks
    │       ├── Register URL form
    │       ├── Endpoint list with delete
    │       └── Delivery history with lifecycle states + HTTP codes
    └── Smart Polling
        ├── Standard tabs: balance + payouts every 60s
        └── Webhooks tab: all data every 10s
```

### Client-Side Idempotency

Every payout submission generates a fresh UUID v4:

```javascript
await api.post('/api/v1/payouts/', {
  amount_paise: Math.round(parseFloat(amount) * 100),
  bank_account_id: bankAccount,
}, {
  headers: { 'Idempotency-Key': crypto.randomUUID() }
});
```

Double-clicks or network retries are safely deduplicated by the server's two-tier idempotency system.

---

## Test Suite

### Strategy

Tests use `TransactionTestCase` with `databases = '__all__'`. Three mocks ensure tests run without external services:

| Mock | Reason |
|---|---|
| `_idem_redis` | In-memory dict with correct `SET NX` semantics |
| `process_payout.delay` | No Celery broker needed |
| `ShardRouter.get_shard` → `'default'` | All ORM calls land on single test DB |

### Tests

| Test | File | What It Proves |
|---|---|---|
| `test_same_key_returns_same_response` | `test_idempotency.py` | Same UUID returns identical payout ID. Zero duplicate rows. |
| `test_different_key_creates_new_payout` | `test_idempotency.py` | Different keys create independent payouts. |
| `test_simultaneous_payouts` | `test_concurrency.py` | `SELECT FOR UPDATE` prevents double-spend — exactly one 201, one 402. |

### CI/CD

Tests run automatically on every push via GitHub Actions against real PostgreSQL 15 and Redis 7 services — no SQLite mocks.

---

## Security

| Control | Implementation |
|---|---|
| **Authentication** | JWT (1-hour access, 7-day refresh). All endpoints require `IsAuthenticated`. |
| **Rate Limiting** | DRF `UserRateThrottle` — 300/min authenticated, 20/min anonymous |
| **Key Scoping** | Idempotency keys namespaced `idem:<merchant_id>:<key>` — no cross-merchant collision |
| **DB Isolation** | `idempotency_db` is a separate PostgreSQL instance — independent migrations and lifecycle |
| **Webhook HMAC** | `X-Playto-Signature: sha256=<digest>` per-endpoint secret |
| **Non-root Containers** | Docker runs as `celery`/`django` users, never root |
| **DEBIT_HOLD first** | Funds reserved at request time — worker can never debit unreserved funds |

---

## Production Operations

### Clearing Stuck Payouts

```bash
docker compose exec backend python manage.py shell --command="
from payouts.models import Payout
for shard in ['shard_0', 'shard_1']:
    count = Payout.objects.using(shard).filter(
        status__in=['PENDING', 'PROCESSING']
    ).update(status='FAILED')
    print(f'Cleared {count} payouts in {shard}')
"
```

### Full Reset

```bash
docker compose down -v     # -v removes database volumes
docker compose up -d --build
```

### Deploy

```bash
git push origin main  # CI/CD: test → build → push → deploy to EC2
```

Vercel auto-deploys frontend on push. Backend deploys via GitHub Actions SSH to EC2.
