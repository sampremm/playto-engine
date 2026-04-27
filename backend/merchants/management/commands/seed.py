from django.core.management.base import BaseCommand
from merchants.models import Merchant, LedgerEntry
from django.contrib.auth.hashers import make_password
from django.db import transaction

class Command(BaseCommand):
    help = 'Seeds the database with test merchants and ledger entries'

    def handle(self, *args, **kwargs):
        accounts = [
            {'email': 'arjun@demo.com', 'balance': 1275000}, # 12,750 rupees -> 12,750,000 paise
            {'email': 'priya@demo.com', 'balance': 465000},  # 4,650 rupees -> 465,000 paise
            {'email': 'rohan@demo.com', 'balance': 725000},  # 7,250 rupees -> 725,000 paise
        ]

        from config.routers import ShardRouter
        router = ShardRouter()

        for acc in accounts:
            email = acc['email']
            balance = acc['balance']
            try:
                # 1. First, we need to know the ID to determine the shard.
                # If they already exist on default, we'll find them there.
                merchant = Merchant.objects.filter(username=email).first()
                if not merchant:
                    # Create on default first to get a stable ID
                    merchant = Merchant.objects.create(
                        username=email,
                        email=email,
                        password=make_password('demo123'),
                        is_active=True
                    )
                
                # 2. Determine the correct shard based on ID
                target_shard = router.get_shard(merchant.id)
                self.stdout.write(f"Routing {email} (ID:{merchant.id}) to {target_shard}...")

                with transaction.atomic(using=target_shard):
                    # Ensure merchant exists on the target shard
                    shard_merchant, created = Merchant.objects.using(target_shard).get_or_create(
                        id=merchant.id,
                        defaults={
                            'username': email,
                            'email': email,
                            'password': make_password('demo123'),
                            'is_active': True,
                        }
                    )
                    
                    # Always ensure password is correct on shard
                    shard_merchant.password = make_password('demo123')
                    shard_merchant.save(using=target_shard)

                    # 3. Check for existing balance on this shard
                    existing_balance = LedgerEntry.objects.using(target_shard).filter(
                        merchant=shard_merchant, 
                        description='Initial Seed Deposit'
                    ).exists()

                    if not existing_balance:
                        LedgerEntry.objects.using(target_shard).create(
                            merchant=shard_merchant,
                            amount_paise=balance,
                            entry_type='CREDIT',
                            description='Initial Seed Deposit'
                        )
                        self.stdout.write(self.style.SUCCESS(f'Seed balance of {balance/100} rupees added to {target_shard}'))
                    else:
                        self.stdout.write(f'Seed balance already exists on {target_shard}')

            except Exception as e:
                self.stdout.write(self.style.ERROR(f'Error seeding {email}: {str(e)}'))
                import traceback
                traceback.print_exc()
