import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from merchants.models import Merchant
from config.routers import ShardRouter

router = ShardRouter()
print("Merchant 1 shard:", router.get_shard(1))
print("Merchant 2 shard:", router.get_shard(2))
print("Merchant 3 shard:", router.get_shard(3))
