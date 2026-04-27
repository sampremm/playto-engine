from django.db import models
import uuid


class Payout(models.Model):
    STATUS_CHOICES = (
        ('PENDING',    'Pending'),
        ('PROCESSING', 'Processing'),
        ('COMPLETED',  'Completed'),
        ('FAILED',     'Failed'),
    )

    # Explicit legal transition map — single source of truth for the state machine.
    # NOTE: Django's .update() queryset method bypasses save() and clean(), so
    # all status changes in the worker MUST go through transition_to() instead.
    LEGAL_TRANSITIONS = {
        'PENDING':    ['PROCESSING'],
        'PROCESSING': ['COMPLETED', 'FAILED'],
        'COMPLETED':  [],
        'FAILED':     [],
    }

    id              = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    merchant        = models.ForeignKey('merchants.Merchant', on_delete=models.PROTECT, related_name='payouts')
    amount_paise    = models.BigIntegerField()
    bank_account_id = models.CharField(max_length=100)
    status          = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING')
    created_at      = models.DateTimeField(auto_now_add=True)
    updated_at      = models.DateTimeField(auto_now=True)
    attempt_count   = models.IntegerField(default=0)

    def transition_to(self, new_status, using=None):
        """
        The ONLY correct way to change payout status.

        Unlike clean()/save(), this method cannot be skipped by .update().
        All state changes in the worker must call this instead of setting
        payout.status directly.

        Args:
            new_status: Target status string.
            using:      Database alias (for shard-aware writes).

        Raises:
            ValueError: If the transition is not permitted by LEGAL_TRANSITIONS.
        """
        allowed = self.LEGAL_TRANSITIONS.get(self.status, [])
        if new_status not in allowed:
            raise ValueError(
                f"Illegal state transition: {self.status} → {new_status}. "
                f"Allowed from {self.status!r}: {allowed}"
            )
        self.status = new_status
        save_kwargs = {'update_fields': ['status', 'updated_at']}
        if using:
            save_kwargs['using'] = using
        self.save(**save_kwargs)

    def clean(self):
        """Fallback guard for ORM-level saves (e.g. admin, shell)."""
        if self.pk:
            try:
                old_status = Payout.objects.get(pk=self.pk).status
                allowed = self.LEGAL_TRANSITIONS.get(old_status, [])
                if old_status != self.status and self.status not in allowed:
                    raise ValueError(f"Illegal state transition from {old_status} to {self.status}")
            except Payout.DoesNotExist:
                pass

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)


class IdempotencyKey(models.Model):
    """
    Stored in the dedicated 'idempotency_db' PostgreSQL instance (see routers.py).
    Provides crash-safe, auditable, ACID-guaranteed idempotency without Redis.

    Lifecycle:
        IN_FLIGHT  — key claimed, first request is currently processing
        COMPLETE   — first request finished, response_body is populated for replay
    """
    STATUS_IN_FLIGHT = 'IN_FLIGHT'
    STATUS_COMPLETE  = 'COMPLETE'
    STATUS_CHOICES   = [
        (STATUS_IN_FLIGHT, 'In Flight'),
        (STATUS_COMPLETE,  'Complete'),
    ]

    key             = models.UUIDField()
    merchant_id     = models.BigIntegerField(db_index=True)   # Denormalised — no FK cross-DB
    idem_status     = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_IN_FLIGHT)
    response_body   = models.JSONField(null=True, blank=True)
    response_status = models.IntegerField(null=True, blank=True)
    created_at      = models.DateTimeField(auto_now_add=True)

    class Meta:
        # Unique per (key, merchant) within the idempotency database
        unique_together = [['key', 'merchant_id']]
        indexes = [
            models.Index(fields=['created_at']),  # For TTL cleanup queries
        ]

    def __str__(self):
        return f"{self.key} [{self.idem_status}]"
