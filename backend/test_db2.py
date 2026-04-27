import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from django.test import TransactionTestCase
from unittest.mock import patch
from merchants.models import Merchant
from payouts.models import IdempotencyKey

class TestDB(TransactionTestCase):
    databases = '__all__'

    @patch('config.routers.ShardRouter.get_shard', return_value='default')
    def setUp(self):
        self.merchant = Merchant.objects.create_user(username='test@test.com', password='password')
        print(f"Merchant db: {self.merchant._state.db}")
        print(f"Merchant id: {self.merchant.id}")

    @patch('config.routers.ShardRouter.get_shard', return_value='default')
    def test_something(self, mock_get_shard):
        key = IdempotencyKey.objects.create(key='12345678-1234-5678-1234-567812345678', merchant=self.merchant)
        print(f"Key db: {key._state.db}")

