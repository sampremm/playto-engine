from celery import shared_task

@shared_task(name='worker_app.process_payout')
def process_payout(payout_id):
    # Empty implementation. Actual processing happens in standalone worker.
    pass
