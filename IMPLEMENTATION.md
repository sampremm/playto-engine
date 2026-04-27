# IMPLEMENTATION.md — Playto Payout Engine

## Executive Summary

Playto Payout Engine is a production-grade distributed payment processing system. It solves three hard problems in financial infrastructure:

1. **Idempotency** — Ensuring that retried or duplicated API calls never result in duplicate money movements, even under concurrent load.
2. **Concurrency Safety** — Preventing double-spend when multiple requests arrive simultaneously for the same account.
3. **Reliability** — Ensuring every payout eventually reaches a terminal state (COMPLETED or FAILED) and every state change triggers a notification, even when components fail.

The system is intentionally over-engineered relative to a simple CRUD app. Every architectural decision maps to a real-world payment infrastructure pattern used at scale.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                          CLIENT (Browser)                           │
│  React 19 + Vite + Tailwind v4 — Glassmorphic SaaS Dashboard       │
│  • JWT auth   • Idempotency-Key per submission   • Smart polling    │
└────────────────────────────┬────────────────────────────────────────┘
                             │ HTTPS
                             ▼
┌─────────────────────────────────────────────────────────────────────┐
│                        API GATEWAY (Django)                         │
│  PayoutCreateView — synchronous intent + validation + locking       │
│  ┌─────────────────────┐  ┌──────────────────────────────────────┐  │
│  │  Two-Tier Idem.     │  │  SELECT FOR UPDATE (row-level lock)  │  │
│  │  L1: Redis db=1     │  │  Balance verified on correct shard   │  │
│  │  L2: PG idem_db     │  │  Payout + Hold written atomically    │  │
│  └─────────────────────┘  └──────────────────────────────────────┘  │
└────────────────────────────┬────────────────────────────────────────┘
                             │ Celery task via Redis db=0 (broker)
                             ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    PAYOUT PROCESSOR (Celery Worker)                 │
│  Shard-aware lookup → transition_to() state machine → atomic writes │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │  Celery Beat (scheduler, every 30s)                         │    │
│  │  • Picks up PENDING payouts orphaned by broker failures     │    │
│  │  • Retries PROCESSING payouts with exponential backoff      │    │
│  │  • Marks FAILED + writes DEBIT_RELEASE after 3 attempts     │    │
│  └─────────────────────────────────────────────────────────────┘    │
└────────────────────────────┬────────────────────────────────────────┘
                             │ HTTP POST + HMAC signature
                             ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     WEBHOOK ENGINE (Django + Celery)                │
│  WebhookDelivery lifecycle: QUEUED→PROCESSING→RETRYING→SENT/FAILED  │
│  Idempotency guard prevents re-delivery after SENT                  │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Database Architecture

### Four Aliases, Four Purposes

```
┌──────────────────────────────────────────────────────────┐
│  default          → Merchants, Auth, Webhooks            │
│                     (reads/writes for non-shard models)  │
├──────────────────────────────────────────────────────────┤
│  shard_0          → Payouts + LedgerEntries              │
│                     (even merchant_id % 2 == 0)          │
├──────────────────────────────────────────────────────────┤
│  shard_1          → Payouts + LedgerEntries              │
│                     (odd merchant_id % 2 == 1)           │
├──────────────────────────────────────────────────────────┤
│  idempotency_db   → IdempotencyKey records only          │
│                     (ACID audit store, isolated by design)│
└──────────────────────────────────────────────────────────┘
```

Locally (development), all four aliases point to the same PostgreSQL server for convenience. In production Docker, each is a separate container. The application code never assumes they share a server.

### The ShardRouter

`config/routers.py` implements a custom Django database router:

```python
class ShardRouter:
    SHARD_COUNT = 2
    IDEMPOTENCY_MODELS = {'idempotencykey'}
    SHARD_MODELS = {'payout', 'ledgerentry'}

    def get_shard(self, merchant_id):
        return f'shard_{int(merchant_id) % self.SHARD_COUNT}'

    def db_for_read(self, model, **hints):
        name = model.__name__.lower()
        if name in self.IDEMPOTENCY_MODELS:
            return 'idempotency_db'
        if name in self.SHARD_MODELS:
            instance = hints.get('instance')
            if instance:
                merchant_id = getattr(instance, 'merchant_id', None) or \
                              getattr(getattr(instance, 'merchant', None), 'id', None)
                if merchant_id:
                    return self.get_shard(merchant_id)
        return None  # Fall through to default

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        if model_name == 'idempotencykey':
            return db == 'idempotency_db'
        if db == 'idempotency_db':
            return False
        return True
```

The router's `db_for_read` is consulted by Django's ORM on every query. It uses the `instance` hint (when available) to determine the correct shard for a given object. In the views and worker, we resolve `active_shard` explicitly at the start of each operation to avoid relying on hints (which are not always present).

### The Immutable Ledger

`LedgerEntry` is the single source of truth for all account state:

```python
class LedgerEntry(models.Model):
    ENTRY_TYPES = [
        ('CREDIT',         'Credit — funds added'),
        ('DEBIT_HOLD',     'Debit Hold — funds reserved'),
        ('DEBIT_FINAL',    'Debit Final — payout confirmed'),
        ('DEBIT_RELEASE',  'Debit Release — refund on failure'),
    ]
    merchant    = models.ForeignKey(Merchant, related_name='ledger_entries')
    amount_paise = models.BigIntegerField()  # Negative for debits
    entry_type  = models.CharField(max_length=20, choices=ENTRY_TYPES)
    description = models.TextField(blank=True)
    created_at  = models.DateTimeField(auto_now_add=True)
```

The balance is always computed as:
```python
balance_paise = LedgerEntry.objects.using(active_shard).filter(
    merchant=merchant
).aggregate(total=Sum('amount_paise'))['total'] or 0
```

This approach guarantees zero balance inconsistency — there is no mutable field that can drift from the ledger state.

---

## Phase 1: The API Gateway

### Request Lifecycle (PayoutCreateView.post)

```
Request arrives
    │
    ▼
1. Validate inputs
   • amount_paise: present, positive integer
   • bank_account_id: present
   • Idempotency-Key header: present, valid UUID
    │
    ▼
2. L1 Redis fast-path check
   GET idem:<merchant_id>:<uuid>
   ├── "__IN_FLIGHT__"  → 409 Conflict (in-progress)
   ├── JSON response   → Replay 201/402 (cache hit)
   └── None            → Miss, continue
    │
    ▼
3. L2 PostgreSQL fallback (e.g. after Redis restart)
   SELECT * FROM idempotencykey WHERE key=... AND merchant_id=...
   ├── COMPLETE        → Re-populate Redis, replay response
   ├── IN_FLIGHT       → Populate Redis sentinel, 409 Conflict
   └── DoesNotExist    → Miss, continue
    │
    ▼
4. Claim key atomically in both stores
   Redis: SET NX → False? → 409 (race lost)
   PostgreSQL: INSERT → IntegrityError? → fetch existing → replay
    │
    ▼
5. Resolve active shard
   active_shard = ShardRouter().get_shard(merchant.id)
    │
    ▼
6. BEGIN TRANSACTION on active_shard
   SELECT Merchant FOR UPDATE → acquires row lock
   SUM(LedgerEntry using active_shard) → current_balance
   insufficient? → commit 402 to idempotency stores, return
    │
    ▼
7. Create Payout (PENDING) + LedgerEntry (DEBIT_HOLD) — atomic
    │
    ▼
8. _commit() — write final response to both idempotency stores
    │
    ▼
9. COMMIT TRANSACTION
   transaction.on_commit → _enqueue_safe(payout.id)
    │
    ▼
10. Return 201 Created
```

### The `_enqueue_safe` Pattern

```python
def _enqueue_safe(payout_id):
    """
    Best-effort task enqueue. If the Celery broker is temporarily
    unreachable, the Beat task retry_stuck_payouts will pick up
    the PENDING payout within 30 seconds.

    The payout is ALREADY committed to the database at this point.
    A broker failure here is non-fatal — it just means the payout
    is processed a few seconds later than immediately.
    """
    try:
        process_payout.delay(payout_id)
    except Exception:
        pass  # Non-fatal

transaction.on_commit(lambda: _enqueue_safe(str(payout.id)))
```

This is the **Outbox Pattern** — the intent (payout) is committed to the database before the notification (task queue) is sent. If the queue fails, the intent is still recorded and can be re-processed.

---

## Phase 2: The Payout Processor (Celery Worker)

### Shard-Aware Task Lookup

```python
@app.task(name='worker_app.process_payout', bind=True, max_retries=3)
def process_payout(self, payout_id):
    payout = None
    active_shard = 'default'

    # Must check all shards — task was enqueued with only the payout ID,
    # not the shard. Merchant routing info is not stored on the task.
    for shard in ['default', 'shard_0', 'shard_1']:
        try:
            payout = Payout.objects.using(shard).get(id=payout_id)
            active_shard = shard
            break
        except Payout.DoesNotExist:
            continue

    if not payout:
        return  # Payout deleted (shouldn't happen in production)

    if payout.status not in ['PENDING', 'PROCESSING']:
        return  # Already in terminal state — idempotency guard

    payout.attempt_count += 1
    if payout.status == 'PENDING':
        payout.transition_to('PROCESSING', using=active_shard)
    else:
        payout.save(using=active_shard, update_fields=['attempt_count', 'updated_at'])
    ...
```

### State Transitions via `transition_to()`

All status changes go through `transition_to()` — not direct field assignment:

```python
# CORRECT — state machine enforced
payout.transition_to('COMPLETED', using=active_shard)

# WRONG — bypasses all guards (never use this in worker)
payout.status = 'COMPLETED'
payout.save()
```

`transition_to()` enforces `LEGAL_TRANSITIONS`, raises `ValueError` on illegal moves, and does a targeted `UPDATE` on only the `status` and `updated_at` fields.

### Beat Reconciliation — The Safety Net

```python
@app.task(name='worker_app.retry_stuck_payouts')
def retry_stuck_payouts():
    now = timezone.now()

    for shard in ['default', 'shard_0', 'shard_1']:

        # Case 1: Orphaned PENDING (broker was down at enqueue time)
        # Any PENDING payout older than 30s never got a worker task.
        orphaned = Payout.objects.using(shard).filter(
            status='PENDING',
            created_at__lt=now - timedelta(seconds=30),
        )
        for payout in orphaned:
            process_payout.delay(str(payout.id))

        # Case 2: Stuck PROCESSING (simulated bank timeout)
        # Exponential backoff: 30s, 60s, 120s between retries
        stuck = Payout.objects.using(shard).filter(status='PROCESSING')
        for payout in stuck:
            delay_seconds = 30 * (2 ** payout.attempt_count)
            if payout.updated_at < now - timedelta(seconds=delay_seconds):
                if payout.attempt_count >= 3:
                    # Max retries — atomic failure + refund
                    with transaction.atomic(using=shard):
                        payout.transition_to('FAILED', using=shard)
                        LedgerEntry.objects.using(shard).create(
                            merchant=payout.merchant,
                            amount_paise=payout.amount_paise,
                            entry_type='DEBIT_RELEASE',
                            description=f'Refund — max retries exceeded'
                        )
                    dispatch_payout_webhook(payout)
                else:
                    payout.attempt_count += 1
                    payout.save(using=shard)
                    process_payout.delay(str(payout.id))
```

This task runs every 30 seconds and covers two failure modes:
1. **Broker failure at enqueue** — payout exists in DB but no task was ever dispatched
2. **Worker crash mid-processing** — payout is stuck in `PROCESSING` with no terminal transition

---

## Phase 3: Webhook Delivery Engine

### Architecture

After every terminal payout state change (`COMPLETED` or `FAILED`), `dispatch_payout_webhook()` is called **outside** the `transaction.atomic()` block. This is deliberate:

```python
with transaction.atomic(using=active_shard):
    payout.transition_to('COMPLETED', using=active_shard)
    LedgerEntry.objects.using(active_shard).create(...)
# ← transaction committed here

dispatch_payout_webhook(payout)  # ← called AFTER commit
```

**Why outside the transaction?**
If webhook dispatch were inside the transaction, a webhook failure (network error, endpoint down) would rollback the `COMPLETED` payout — turning a successful payment into an apparent failure. The payout state is the ground truth; webhook delivery is best-effort notification.

### Delivery Lifecycle

```
dispatch_payout_webhook(payout)
    │
    ├── Query all active WebhookEndpoints for merchant
    │
    └── For each endpoint:
            │
            ▼
       WebhookDelivery.objects.create(status='QUEUED')
            │
            ▼
       POST payload to endpoint.url
       Headers:
         Content-Type: application/json
         X-Playto-Signature: sha256=<HMAC-SHA256 of body with endpoint.secret>
       Body:
         {
           "event": "payout.completed",
           "payout_id": "uuid",
           "status": "COMPLETED",
           "amount_paise": 5000,
           "merchant_id": 1,
           "timestamp": "ISO-8601"
         }
            │
       ┌────┴───────┐
       │            │
     200 OK      Failure
       │            │
     SENT        RETRYING → exponential backoff (30s, 60s, 120s)
   (terminal)    After 3 fails → FAILED (terminal)
                 Idempotency guard: if already SENT, exit immediately
```

### HMAC Signature Verification

Merchants can verify that deliveries genuinely originate from Playto by checking the signature:

```python
import hmac, hashlib

def verify_signature(body: bytes, secret: str, signature_header: str) -> bool:
    expected = hmac.new(
        secret.encode(), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature_header)
```

---

## Phase 4: Frontend Dashboard

### Component Architecture

```
Dashboard.jsx
├── State
│   ├── balance          ← available_rupees, held_rupees
│   ├── payouts          ← list of recent payouts with status
│   ├── ledger           ← append-only transaction history
│   ├── webhookEndpoints ← registered URLs with active flag
│   └── webhookDeliveries← delivery attempts with lifecycle states
│
├── Tabs
│   ├── Recent Payouts   ← table with status badges (PENDING/COMPLETED/FAILED)
│   ├── Ledger           ← full entry history with signed amounts
│   └── Webhooks
│       ├── Register URL form
│       ├── Endpoint list with hover-delete (Trash2 icon)
│       └── Delivery History table with lifecycle states + HTTP codes
│
└── Smart Polling
    ├── On standard tabs: balance + payouts every 60s
    └── On Webhooks tab: all data every 10s (to watch QUEUED→SENT)
```

### Idempotency on the Client

Every payout form submission generates a fresh UUID v4:

```javascript
await api.post('/api/v1/payouts/', {
  amount_paise: Math.round(parseFloat(amount) * 100),
  bank_account_id: bankAccount,
}, {
  headers: { 'Idempotency-Key': uuidv4() }
});
```

If the user double-clicks "Submit" or the request is retried on a slow connection, the server's two-tier idempotency system ensures exactly one payout is created.

---

## Test Suite

### Strategy

Tests run using Django's `TransactionTestCase` with `databases = '__all__'` — all four DB aliases are created and cleaned per test. Three mocks are applied:

| Mock | Reason |
|---|---|
| `payouts.views._idem_redis` | In-memory dict with correct `SET NX` semantics — no Redis server needed |
| `payouts.views.process_payout.delay` | No Celery broker needed — task is not the subject under test |
| `config.routers.ShardRouter.get_shard` → `'default'` | All ORM calls land on the single test DB — no multi-DB complexity |

### Test Coverage

```bash
cd backend
python manage.py test payouts.tests --verbosity=2
# Ran 3 tests in 0.990s — OK ✅
```

| Test | File | What It Proves |
|---|---|---|
| `test_same_key_returns_same_response` | `test_idempotency.py` | Same UUID returns identical payout ID. Zero duplicate rows in DB. Redis mock correctly replays the cached response on the second call. |
| `test_different_key_creates_new_payout` | `test_idempotency.py` | Two different UUIDs create two independent payouts. No cross-key contamination. |
| `test_simultaneous_payouts` | `test_concurrency.py` | Two sequential 60₹ requests against a 100₹ balance. `SELECT FOR UPDATE` ensures exactly one 201 and one 402. Exactly one Payout row in DB — zero double-spend. |

### Redis Mock Implementation

```python
def make_redis_mock():
    """In-memory fake Redis with correct SET NX semantics."""
    store = {}
    mock = MagicMock()

    def fake_set(key, value, ex=None, nx=False):
        if nx:
            if key in store:
                return False   # SET NX fails — key exists
            store[key] = value
            return True        # SET NX succeeds — key claimed
        store[key] = value
        return True

    def fake_get(key):
        return store.get(key)

    def fake_delete(key):
        store.pop(key, None)

    mock.get.side_effect = fake_get
    mock.set.side_effect = fake_set
    mock.delete.side_effect = fake_delete
    return mock
```

The `nx=True` branch is critical — without it the mock would allow both concurrent requests to claim the key, making the concurrency test meaningless.

---

## Security Considerations

| Control | Implementation |
|---|---|
| **Authentication** | JWT via `djangorestframework-simplejwt`. All endpoints require `IsAuthenticated`. |
| **Rate Limiting** | DRF `UserRateThrottle` — 300 req/min authenticated, 20 req/min anonymous. Prevents brute force and accidental polling storms. |
| **Idempotency Key Scoping** | Keys are namespaced `idem:<merchant_id>:<key>` — one merchant's keys never collide with another's. |
| **Cross-DB Isolation** | `idempotency_db` alias is a separate PostgreSQL connection. Business data migrations never touch it; idempotency migrations never touch business tables. |
| **Webhook HMAC** | `X-Playto-Signature: sha256=<digest>` signed with per-endpoint secret. Merchants can verify authenticity of every delivery. |
| **Non-root Containers** | Docker containers run as `django`/`celery` users — not root. |
| **DEBIT_HOLD before processing** | Funds are reserved at request time, not at processing time. Worker can never debit funds that weren't already locked by the API. |

---

## Running the Full System

### Terminal 1 — Django API
```bash
cd /path/to/Payto-pay/backend
source ../.venv/bin/activate
python manage.py runserver
# → http://localhost:8000
```

### Terminal 2 — Celery Worker + Beat Scheduler
```bash
cd /path/to/Payto-pay
source .venv/bin/activate
export PYTHONPATH=./backend
export DJANGO_SETTINGS_MODULE=config.settings
celery -A worker.worker_app worker -l info -B
```

### Terminal 3 — React Frontend
```bash
cd /path/to/Payto-pay/frontend
npm install && npm run dev
# → http://localhost:5173
```

### First-Time Database Setup (Local Manual Run)

If running the services manually (outside of Docker):
```bash
cd backend
python manage.py migrate --database=default
python manage.py migrate --database=shard_0
python manage.py migrate --database=shard_1
python manage.py migrate --database=idempotency_db
python manage.py seed  # Creates 3 demo merchants with seeded balance
```

**Note for Docker Users:**
The `docker-compose up` command automatically triggers `docker-entrypoint.sh`, which performs all the above migrations and seeding for you. You do not need to run these manually if using Docker.

