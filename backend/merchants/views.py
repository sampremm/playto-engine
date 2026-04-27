from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from .models import LedgerEntry
from django.db.models import Sum

class BalanceView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from config.routers import ShardRouter
        active_shard = ShardRouter().get_shard(request.user.id)
        
        ledger_entries = LedgerEntry.objects.using(active_shard).filter(merchant=request.user)
        available_paise = ledger_entries.aggregate(Sum('amount_paise'))['amount_paise__sum'] or 0
        
        # Calculate held amount from active payouts
        active_payouts = request.user.payouts.using(active_shard).filter(status__in=['PENDING', 'PROCESSING'])
        held_paise = active_payouts.aggregate(Sum('amount_paise'))['amount_paise__sum'] or 0
        
        total_credited = ledger_entries.filter(entry_type='CREDIT').aggregate(Sum('amount_paise'))['amount_paise__sum'] or 0
        
        return Response({
            'available_paise': available_paise,
            'available_rupees': available_paise / 100,
            'held_paise': held_paise,
            'held_rupees': held_paise / 100,
            'total_credited': total_credited
        })

class LedgerView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from config.routers import ShardRouter
        active_shard = ShardRouter().get_shard(request.user.id)
        
        entries = LedgerEntry.objects.using(active_shard).filter(merchant=request.user).order_by('-created_at')
        return Response([{
            'id': entry.id,
            'amount_paise': entry.amount_paise,
            'entry_type': entry.entry_type,
            'description': entry.description,
            'created_at': entry.created_at
        } for entry in entries])
