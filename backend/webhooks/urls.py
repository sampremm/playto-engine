from django.urls import path
from .views import WebhookEndpointView, WebhookDeliveryView

urlpatterns = [
    path('endpoints/', WebhookEndpointView.as_view(), name='webhook_endpoints'),
    path('deliveries/', WebhookDeliveryView.as_view(), name='webhook_deliveries'),
]
