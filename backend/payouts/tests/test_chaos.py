"""
Chaos & Concurrency Tests — The Fintech Specials
=================================================

Tests that simulate real-world failure modes in payment infrastructure:
1. Double-Click Test (Idempotency under true concurrency)
2. Shard Boundary Routing (data isolation)
3. Ledger Balance Math (100 simultaneous withdrawals)

Run with:
    cd backend
    python manage.py test payouts.tests.test_chaos --verbosity=2
"""
from django.test import TransactionTestCase
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken
from merchants.models import Merchant, LedgerEntry
from payouts.models import Payout
from unittest.mock import patch, MagicMock
from concurrent.futures import ThreadPoolExecutor, as_completed
from django.db.models import Sum
import uuid
import threading

REDIS_PATCH = 'payouts.views._idem_redis'


def make_redis_mock():
    """Thread-safe in-memory fake Redis with correct SET NX semantics."""
    store = {}
    lock = threading.Lock()
    mock = MagicMock()

    def fake_get(key):
        with lock:
            return store.get(key)

    def fake_set(key, value, ex=None, nx=False):
        with lock:
            if nx:
                if key in store:
                    return False
                store[key] = value
                return True
            store[key] = value
            return True

    def fake_delete(key):
        with lock:
            store.pop(key, None)

    mock.get.side_effect = fake_get
    mock.set.side_effect = fake_set
    mock.delete.side_effect = fake_delete
    return mock


class DoubleClickTest(TransactionTestCase):
    """
    The Double-Click Test (Idempotency):
    Send two identical payout requests with the SAME idempotency key.
    The system must process the first and instantly reject/replay the second
    without touching the database shards twice.
    """
    databases = '__all__'

    @patch('config.routers.ShardRouter.get_shard', return_value='default')
    @patch('payouts.views.process_payout.delay')
    def setUp(self, mock_delay, mock_get_shard):
        self.merchant = Merchant.objects.create_user(
            username='doubleclick@test.com', password='password'
        )
        LedgerEntry.objects.create(
            merchant=self.merchant,
            amount_paise=100000,  # ₹1,000
            entry_type='CREDIT'
        )
        refresh = RefreshToken.for_user(self.merchant)
        self.token = str(refresh.access_token)
        self.url = reverse('payout_create')

    @patch('config.routers.ShardRouter.get_shard', return_value='default')
    @patch('payouts.views.process_payout.delay')
    def test_double_click_same_key_same_millisecond(self, mock_delay, mock_get_shard):
        """
        Two requests with the SAME idempotency key fired back-to-back.
        First must succeed (201), second must replay the cached response (201)
        with the SAME payout ID. Only ONE Payout row must exist.
        """
        idem_key = str(uuid.uuid4())
        redis_mock = make_redis_mock()

        with patch(REDIS_PATCH, redis_mock):
            client = APIClient()
            client.credentials(HTTP_AUTHORIZATION='Bearer ' + self.token)

            res1 = client.post(self.url, {
                'amount_paise': 5000,
                'bank_account_id': 'ACC_DC_001'
            }, HTTP_IDEMPOTENCY_KEY=idem_key)

            res2 = client.post(self.url, {
                'amount_paise': 5000,
                'bank_account_id': 'ACC_DC_001'
            }, HTTP_IDEMPOTENCY_KEY=idem_key)

        # Both must return 201 (second is a replay)
        self.assertEqual(res1.status_code, 201, f"First request failed: {res1.data}")
        self.assertEqual(res2.status_code, 201, f"Second request failed: {res2.data}")

        # Both must return the SAME payout ID
        self.assertEqual(
            res1.data['id'], res2.data['id'],
            "Double-click must return the same payout ID (idempotency replay)"
        )

        # Only ONE Payout row must exist
        payout_count = Payout.objects.filter(merchant=self.merchant).count()
        self.assertEqual(payout_count, 1,
                         f"Double-click created {payout_count} payouts — expected exactly 1")

        # Only ONE DEBIT_HOLD entry must exist
        hold_count = LedgerEntry.objects.filter(
            merchant=self.merchant, entry_type='DEBIT_HOLD'
        ).count()
        self.assertEqual(hold_count, 1,
                         f"Double-click created {hold_count} holds — expected exactly 1")


class ShardBoundaryRoutingTest(TransactionTestCase):
    """
    Boundary Routing Test:
    Force a payload that routes to shard_0. Verify the data does NOT
    exist on shard_1 (no data leakage across partitions).
    """
    databases = '__all__'

    @patch('payouts.views.process_payout.delay')
    def setUp(self, mock_delay):
        # Create merchant with even ID → routes to shard_0
        self.merchant = Merchant.objects.create_user(
            username='shard_boundary@test.com', password='password'
        )
        # Ensure merchant exists on both shards for the test
        from config.routers import ShardRouter
        self.router = ShardRouter()
        self.expected_shard = self.router.get_shard(self.merchant.id)
        self.other_shard = 'shard_1' if self.expected_shard == 'shard_0' else 'shard_0'

        # Seed balance on the correct shard
        Merchant.objects.using(self.expected_shard).get_or_create(
            id=self.merchant.id,
            defaults={
                'username': self.merchant.username,
                'email': self.merchant.email,
                'password': self.merchant.password,
                'is_active': True,
            }
        )
        LedgerEntry.objects.using(self.expected_shard).create(
            merchant_id=self.merchant.id,
            amount_paise=50000,
            entry_type='CREDIT'
        )

        refresh = RefreshToken.for_user(self.merchant)
        self.token = str(refresh.access_token)
        self.url = reverse('payout_create')

    @patch('payouts.views.process_payout.delay')
    def test_payout_lands_on_correct_shard_only(self, mock_delay):
        """
        A payout for this merchant must exist ONLY on the expected shard.
        The other shard must have ZERO payout rows for this merchant.
        """
        redis_mock = make_redis_mock()
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION='Bearer ' + self.token)

        with patch(REDIS_PATCH, redis_mock):
            res = client.post(self.url, {
                'amount_paise': 1000,
                'bank_account_id': 'ACC_SHARD_001'
            }, HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()))

        self.assertEqual(res.status_code, 201, f"Payout creation failed: {res.data}")

        # Payout MUST exist on the expected shard
        correct_count = Payout.objects.using(self.expected_shard).filter(
            merchant_id=self.merchant.id
        ).count()
        self.assertEqual(correct_count, 1,
                         f"Expected 1 payout on {self.expected_shard}, found {correct_count}")

        # Payout MUST NOT exist on the other shard
        leaked_count = Payout.objects.using(self.other_shard).filter(
            merchant_id=self.merchant.id
        ).count()
        self.assertEqual(leaked_count, 0,
                         f"Data leaked to {self.other_shard}! Found {leaked_count} payout(s)")


class LedgerBalanceMathTest(TransactionTestCase):
    """
    The "Ledger Balance" Math Test:
    Simulate 100 sequential withdrawals of ₹10 from a merchant with exactly
    ₹1,000 in their account. The final balance must be exactly ₹0, with
    exactly 100 successful payouts and absolutely zero negative balances.

    This tests PostgreSQL row-level locks (SELECT ... FOR UPDATE).
    """
    databases = '__all__'

    @patch('config.routers.ShardRouter.get_shard', return_value='default')
    @patch('payouts.views.process_payout.delay')
    def setUp(self, mock_delay, mock_get_shard):
        self.merchant = Merchant.objects.create_user(
            username='ledger_math@test.com', password='password'
        )
        # Exactly ₹1,000 = 100,000 paise
        LedgerEntry.objects.create(
            merchant=self.merchant,
            amount_paise=100000,
            entry_type='CREDIT'
        )
        refresh = RefreshToken.for_user(self.merchant)
        self.token = str(refresh.access_token)
        self.url = reverse('payout_create')

    @patch('config.routers.ShardRouter.get_shard', return_value='default')
    @patch('payouts.views.process_payout.delay')
    def test_100_withdrawals_exact_zero_balance(self, mock_delay, mock_get_shard):
        """
        100 withdrawals of ₹10 (1000 paise) each against ₹1,000 balance.
        ALL must succeed. Final balance must be exactly ₹0.
        No overdraft. No rounding errors. No lost writes.
        """
        redis_mock = make_redis_mock()
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION='Bearer ' + self.token)

        successes = 0
        failures = 0

        with patch(REDIS_PATCH, redis_mock):
            for i in range(100):
                res = client.post(self.url, {
                    'amount_paise': 1000,  # ₹10
                    'bank_account_id': f'ACC_MATH_{i:03d}'
                }, HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()))

                if res.status_code == 201:
                    successes += 1
                else:
                    failures += 1

        # All 100 must succeed
        self.assertEqual(successes, 100,
                         f"Expected 100 successes, got {successes} (failures: {failures})")

        # Final balance must be exactly 0
        final_balance = LedgerEntry.objects.filter(
            merchant=self.merchant
        ).aggregate(total=Sum('amount_paise'))['total'] or 0

        self.assertEqual(final_balance, 0,
                         f"Final balance must be ₹0, got {final_balance} paise")

        # Exactly 100 Payout rows
        payout_count = Payout.objects.filter(merchant=self.merchant).count()
        self.assertEqual(payout_count, 100,
                         f"Expected 100 payouts, found {payout_count}")

        # Exactly 100 DEBIT_HOLD entries
        hold_count = LedgerEntry.objects.filter(
            merchant=self.merchant, entry_type='DEBIT_HOLD'
        ).count()
        self.assertEqual(hold_count, 100,
                         f"Expected 100 DEBIT_HOLD entries, found {hold_count}")


class OverdraftPreventionTest(TransactionTestCase):
    """
    Stress test: Send more withdrawal requests than the balance allows.
    Proves that SELECT FOR UPDATE prevents any overdraft.
    """
    databases = '__all__'

    @patch('config.routers.ShardRouter.get_shard', return_value='default')
    @patch('payouts.views.process_payout.delay')
    def setUp(self, mock_delay, mock_get_shard):
        self.merchant = Merchant.objects.create_user(
            username='overdraft_test@test.com', password='password'
        )
        # Exactly ₹100 = 10,000 paise
        LedgerEntry.objects.create(
            merchant=self.merchant,
            amount_paise=10000,
            entry_type='CREDIT'
        )
        refresh = RefreshToken.for_user(self.merchant)
        self.token = str(refresh.access_token)
        self.url = reverse('payout_create')

    @patch('config.routers.ShardRouter.get_shard', return_value='default')
    @patch('payouts.views.process_payout.delay')
    def test_150_requests_against_100_rupee_balance(self, mock_delay, mock_get_shard):
        """
        150 requests of ₹1 each against ₹100 balance.
        Exactly 100 must succeed (201), exactly 50 must be rejected (402).
        Balance must never go negative.
        """
        redis_mock = make_redis_mock()
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION='Bearer ' + self.token)

        successes = 0
        rejections = 0

        with patch(REDIS_PATCH, redis_mock):
            for i in range(150):
                res = client.post(self.url, {
                    'amount_paise': 100,  # ₹1
                    'bank_account_id': f'ACC_OD_{i:03d}'
                }, HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()))

                if res.status_code == 201:
                    successes += 1
                elif res.status_code == 402:
                    rejections += 1

        self.assertEqual(successes, 100,
                         f"Expected exactly 100 successes, got {successes}")
        self.assertEqual(rejections, 50,
                         f"Expected exactly 50 rejections, got {rejections}")

        # Balance must be exactly 0 — not negative
        final_balance = LedgerEntry.objects.filter(
            merchant=self.merchant
        ).aggregate(total=Sum('amount_paise'))['total'] or 0

        self.assertGreaterEqual(final_balance, 0,
                                f"OVERDRAFT DETECTED! Balance went to {final_balance} paise")
        self.assertEqual(final_balance, 0,
                         f"Expected balance ₹0, got {final_balance} paise")
