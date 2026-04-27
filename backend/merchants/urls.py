from django.urls import path
from .views import BalanceView, LedgerView

urlpatterns = [
    path('balance/', BalanceView.as_view(), name='merchant_balance'),
    path('ledger/', LedgerView.as_view(), name='merchant_ledger'),
]
