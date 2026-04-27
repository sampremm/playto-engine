"""
Idempotency tests for the two-tier system (Redis L1 + PostgreSQL L2).

Mocking strategy:
  - Redis (_idem_redis): fully mocked with an in-memory dict.
  - PostgreSQL: IDEMPOTENCY_DB_ALIAS is set to 'default' in test settings,
    so all real ORM calls (create, get, save, delete) hit the default
    test database automatically. No complex patching needed.
"""
from django.test import TransactionTestCase
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken
from merchants.models import Merchant, LedgerEntry
from unittest.mock import patch, MagicMock
import uuid

REDIS_PATCH = 'payouts.views._idem_redis'


def make_redis_mock():
    """In-memory fake Redis with correct SET NX semantics."""
    store = {}
    mock = MagicMock()

    def fake_get(key):
        return store.get(key)

    def fake_set(key, value, ex=None, nx=False):
        if nx:
            if key in store:
                return False
            store[key] = value
            return True
        store[key] = value
        return True

    def fake_delete(key):
        store.pop(key, None)

    mock.get.side_effect = fake_get
    mock.set.side_effect = fake_set
    mock.delete.side_effect = fake_delete
    return mock


class IdempotencyTest(TransactionTestCase):
    databases = '__all__'

    @patch('config.routers.ShardRouter.get_shard', return_value='default')
    @patch('payouts.views.process_payout.delay')
    def setUp(self, mock_delay, mock_get_shard):
        self.merchant = Merchant.objects.create_user(
            username='idem_test@test.com', password='password'
        )
        LedgerEntry.objects.create(
            merchant=self.merchant,
            amount_paise=10000,
            entry_type='CREDIT'
        )
        refresh = RefreshToken.for_user(self.merchant)
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION='Bearer ' + str(refresh.access_token))
        self.url = reverse('payout_create')

    @patch('config.routers.ShardRouter.get_shard', return_value='default')
    @patch('payouts.views.process_payout.delay')
    def test_same_key_returns_same_response(self, mock_delay, mock_get_shard):
        """
        Second call with the same Idempotency-Key must return the exact
        same payout ID. Redis L1 serves the cached response on the second call.
        No duplicate Payout row should be created.
        """
        idem_key = str(uuid.uuid4())

        with patch(REDIS_PATCH, make_redis_mock()):
            res1 = self.client.post(self.url, {
                'amount_paise': 2000,
                'bank_account_id': 'TEST001'
            }, HTTP_IDEMPOTENCY_KEY=idem_key)
            self.assertEqual(res1.status_code, 201, res1.data)

            res2 = self.client.post(self.url, {
                'amount_paise': 2000,
                'bank_account_id': 'TEST001'
            }, HTTP_IDEMPOTENCY_KEY=idem_key)
            self.assertEqual(res2.status_code, 201, res2.data)
            self.assertEqual(res1.data['id'], res2.data['id'],
                             "Same idempotency key must return the same payout ID")

    @patch('config.routers.ShardRouter.get_shard', return_value='default')
    @patch('payouts.views.process_payout.delay')
    def test_different_key_creates_new_payout(self, mock_delay, mock_get_shard):
        """
        Two requests with different Idempotency-Keys must create
        independent Payout records with different IDs.
        """
        with patch(REDIS_PATCH, make_redis_mock()):
            res1 = self.client.post(self.url, {
                'amount_paise': 2000,
                'bank_account_id': 'TEST001'
            }, HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()))

            res2 = self.client.post(self.url, {
                'amount_paise': 2000,
                'bank_account_id': 'TEST001'
            }, HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()))

            self.assertEqual(res1.status_code, 201, res1.data)
            self.assertEqual(res2.status_code, 201, res2.data)
            self.assertNotEqual(res1.data['id'], res2.data['id'],
                                "Different keys must produce different payouts")
