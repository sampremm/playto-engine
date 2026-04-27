from django.urls import path
from .views import PayoutCreateView, PayoutListView

urlpatterns = [
    path('', PayoutCreateView.as_view(), name='payout_create'),
    path('list/', PayoutListView.as_view(), name='payout_list'),
]
