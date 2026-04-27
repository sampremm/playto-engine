from django.contrib import admin
from django.urls import path, include
from django.http import JsonResponse

def health_check(request):
    return JsonResponse({'status': 'ok', 'message': 'Playto Payout Engine is running!'})

urlpatterns = [
    path('', health_check),
    path('health/', health_check),
    path('admin/', admin.site.urls),
    path('api/v1/auth/', include('merchants.auth_urls')),
    path('api/v1/merchants/', include('merchants.urls')),
    path('api/v1/payouts/', include('payouts.urls')),
    path('api/v1/webhooks/', include('webhooks.urls')),
]
