import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from payouts.models import Payout
from merchants.models import Merchant

merchant = Merchant.objects.create_user(username='test_payout@test.com', password='password')
p = Payout(merchant=merchant, amount_paise=1000, bank_account_id='TEST')
print("Adding before save:", p._state.adding)

try:
    payout = Payout.objects.create(merchant=merchant, amount_paise=1000, bank_account_id='TEST')
    print("Created successfully!")
except Exception as e:
    import traceback
    traceback.print_exc()

