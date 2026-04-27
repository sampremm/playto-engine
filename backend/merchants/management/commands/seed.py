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

        for acc in accounts:
            email = acc['email']
            balance = acc['balance']
            try:
                with transaction.atomic():
                    merchant, created = Merchant.objects.get_or_create(
                        username=email,
                        defaults={
                            'email': email,
                            'password': make_password('demo123'),
                            'is_active': True,
                        }
                    )
                    
                    if created:
                        LedgerEntry.objects.create(
                            merchant=merchant,
                            amount_paise=balance,
                            entry_type='CREDIT',
                            description='Initial Seed Deposit'
                        )
                        self.stdout.write(self.style.SUCCESS(f'Created {email} with balance {balance/100} rupees'))
                    else:
                        # Always ensure demo password is correct
                        merchant.password = make_password('demo123')
                        merchant.save()
                        self.stdout.write(f'{email} already exists (password reset)')
            except Exception as e:
                self.stdout.write(self.style.ERROR(f'Error seeding {email}: {str(e)}'))
