from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from .models import WebhookEndpoint, WebhookDelivery


class WebhookEndpointView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        """List all registered webhook endpoints for the authenticated merchant."""
        from config.routers import ShardRouter
        active_shard = ShardRouter().get_shard(request.user.id)
        
        endpoints = WebhookEndpoint.objects.using(active_shard).filter(merchant=request.user)
        return Response([{
            'id': str(e.id),
            'url': e.url,
            'is_active': e.is_active,
            'created_at': e.created_at,
        } for e in endpoints])

    def post(self, request):
        """Register a new webhook endpoint URL."""
        from config.routers import ShardRouter
        active_shard = ShardRouter().get_shard(request.user.id)
        
        url = request.data.get('url')
        secret = request.data.get('secret', '')

        if not url:
            return Response({'error': 'url is required'}, status=status.HTTP_400_BAD_REQUEST)

        endpoint = WebhookEndpoint.objects.using(active_shard).create(
            merchant=request.user,
            url=url,
            secret=secret,
        )
        return Response({
            'id': str(endpoint.id),
            'url': endpoint.url,
            'is_active': endpoint.is_active,
            'created_at': endpoint.created_at,
        }, status=status.HTTP_201_CREATED)

    def delete(self, request):
        """Permanently delete a webhook endpoint."""
        from config.routers import ShardRouter
        active_shard = ShardRouter().get_shard(request.user.id)
        
        endpoint_id = request.data.get('id')
        try:
            endpoint = WebhookEndpoint.objects.using(active_shard).get(id=endpoint_id, merchant=request.user)
            endpoint.delete(using=active_shard)
            return Response({'message': 'Endpoint deleted'})
        except WebhookEndpoint.DoesNotExist:
            return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)


class WebhookDeliveryView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        """
        List recent webhook delivery attempts for the merchant.
        Shows the full lifecycle: QUEUED → PROCESSING → RETRYING → SENT/FAILED
        """
        from config.routers import ShardRouter
        active_shard = ShardRouter().get_shard(request.user.id)
        
        deliveries = WebhookDelivery.objects.using(active_shard).filter(
            endpoint__merchant=request.user
        ).select_related('endpoint').order_by('-created_at')[:50]

        return Response([{
            'id': str(d.id),
            'event_type': d.event_type,
            'endpoint_url': d.endpoint.url,
            'status': d.status,
            'attempt_count': d.attempt_count,
            'max_attempts': d.max_attempts,
            'last_http_status': d.last_http_status,
            'last_error': d.last_error,
            'created_at': d.created_at,
            'delivered_at': d.delivered_at,
        } for d in deliveries])
