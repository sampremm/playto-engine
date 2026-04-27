import hashlib


class ShardRouter:
    """
    Routes merchant business data (Merchant, LedgerEntry, Payout, WebhookDelivery)
    across shard_0 and shard_1 based on a hash of the merchant_id.

    Routes IdempotencyKey to a dedicated 'idempotency_db' — a separate
    PostgreSQL instance that can be scaled, audited, or purged independently
    of the core business shards.
    """

    IDEMPOTENCY_MODELS = {'idempotencykey'}

    def get_shard(self, merchant_id):
        if merchant_id is None:
            return 'default'
        try:
            shard_id = int(hashlib.md5(str(merchant_id).encode()).hexdigest(), 16) % 2
            return f'shard_{shard_id}'
        except Exception:
            return 'default'

    def db_for_read(self, model, **hints):
        # 1. Idempotency keys always go to their dedicated database
        if model.__name__.lower() in self.IDEMPOTENCY_MODELS:
            return 'idempotency_db'

        # 2. Shard routing for business models
        if model._meta.app_label in ['merchants', 'payouts', 'webhooks']:
            merchant_id = None
            if 'instance' in hints:
                inst = hints['instance']
                if model.__name__ == 'Merchant':
                    merchant_id = inst.id
                elif hasattr(inst, 'merchant_id'):
                    merchant_id = inst.merchant_id
                elif hasattr(inst, 'merchant') and hasattr(inst.merchant, 'id'):
                    merchant_id = inst.merchant.id
            
            if merchant_id is None and 'merchant_id' in hints:
                merchant_id = hints['merchant_id']

            if merchant_id:
                return self.get_shard(merchant_id)
        
        return None

    def db_for_write(self, model, **hints):
        return self.db_for_read(model, **hints)

    def allow_relation(self, obj1, obj2, **hints):
        return True

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        from django.conf import settings
        idem_alias = getattr(settings, 'IDEMPOTENCY_DB_ALIAS', 'idempotency_db')

        # 1. IdempotencyKey only lives in the dedicated idempotency DB
        if model_name and model_name.lower() == 'idempotencykey':
            return db == idem_alias

        # 2. During app-level checks (model_name is None), we must allow
        # the app that contains IdempotencyKey to 'see' the idempotency DB.
        if model_name is None:
            if app_label == 'payouts':
                return db in [idem_alias, 'default', 'shard_0', 'shard_1']
            if db == idem_alias and db != 'default':
                return False  # No other apps should migrate here

        # 3. All other models must NOT migrate to the idempotency database
        # unless it is the 'default' database (e.g. during tests).
        if db == idem_alias and db != 'default':
            return False

        return True
