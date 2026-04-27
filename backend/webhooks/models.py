import uuid
from django.db import models
from merchants.models import Merchant


class WebhookEndpoint(models.Model):
    """
    A merchant registers a URL where they want to receive payout events.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    merchant = models.ForeignKey(
        Merchant, on_delete=models.CASCADE, related_name='webhook_endpoints'
    )
    url = models.URLField(max_length=500)
    secret = models.CharField(
        max_length=64, blank=True,
        help_text="HMAC-SHA256 signing secret sent in X-Playto-Signature header"
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.merchant.username} → {self.url}"


class WebhookDelivery(models.Model):
    """
    One delivery attempt per (event, endpoint) pair.
    Lifecycle: QUEUED → PROCESSING → SENT
                                   → RETRYING → SENT
                                              → FAILED
    """
    STATUS_QUEUED     = 'QUEUED'
    STATUS_PROCESSING = 'PROCESSING'
    STATUS_RETRYING   = 'RETRYING'
    STATUS_SENT       = 'SENT'
    STATUS_FAILED     = 'FAILED'

    STATUS_CHOICES = [
        (STATUS_QUEUED,     'Queued'),
        (STATUS_PROCESSING, 'Processing'),
        (STATUS_RETRYING,   'Retrying'),
        (STATUS_SENT,       'Sent'),
        (STATUS_FAILED,     'Failed'),
    ]

    # Legal state transitions — mirrors the payout state machine pattern
    LEGAL_TRANSITIONS = {
        STATUS_QUEUED:     [STATUS_PROCESSING],
        STATUS_PROCESSING: [STATUS_SENT, STATUS_RETRYING, STATUS_FAILED],
        STATUS_RETRYING:   [STATUS_PROCESSING],
        STATUS_SENT:       [],   # terminal — idempotency guard blocks re-processing
        STATUS_FAILED:     [],   # terminal
    }

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    endpoint = models.ForeignKey(
        WebhookEndpoint, on_delete=models.CASCADE, related_name='deliveries'
    )

    # The event payload — what we are delivering
    event_type = models.CharField(max_length=50)   # e.g. "payout.completed"
    payload = models.JSONField()                    # full event body

    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default=STATUS_QUEUED, db_index=True
    )
    attempt_count = models.PositiveSmallIntegerField(default=0)
    max_attempts  = models.PositiveSmallIntegerField(default=3)

    # Response tracking
    last_http_status  = models.IntegerField(null=True, blank=True)
    last_error        = models.TextField(blank=True)

    created_at  = models.DateTimeField(auto_now_add=True)
    updated_at  = models.DateTimeField(auto_now=True)
    delivered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def transition_to(self, new_status: str, using: str = None):
        """
        Enforce the state machine and persist the change.
        Raises ValueError on illegal transition.
        """
        allowed = self.LEGAL_TRANSITIONS.get(self.status, [])
        if new_status not in allowed:
            raise ValueError(
                f"Illegal WebhookDelivery transition: {self.status} → {new_status}"
            )
        self.status = new_status
        save_kwargs = {'update_fields': ['status', 'updated_at']}
        if using:
            save_kwargs['using'] = using
        self.save(**save_kwargs)

    def __str__(self):
        return f"{self.event_type} [{self.status}] → {self.endpoint.url}"
