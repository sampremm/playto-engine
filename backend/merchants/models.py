from django.db import models
from django.contrib.auth.models import AbstractUser

class Merchant(AbstractUser):
    pass

class LedgerEntry(models.Model):
    ENTRY_TYPES = (
        ('CREDIT', 'Credit'),
        ('DEBIT_HOLD', 'Debit Hold'),
        ('DEBIT_FINAL', 'Debit Final'),
        ('DEBIT_RELEASE', 'Debit Release'),
    )
    merchant = models.ForeignKey(Merchant, on_delete=models.PROTECT, related_name='ledger_entries')
    amount_paise = models.BigIntegerField()
    entry_type = models.CharField(max_length=20, choices=ENTRY_TYPES)
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
