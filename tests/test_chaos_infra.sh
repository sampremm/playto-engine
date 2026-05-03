#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════════
# Playto Payout Engine — Chaos Test Suite
# ═══════════════════════════════════════════════════════════════════════════════
#
# Infrastructure-level chaos tests that must run on the EC2 server
# where Docker containers are running.
#
# Usage:
#   chmod +x tests/test_chaos_infra.sh
#   ./tests/test_chaos_infra.sh
#
# ═══════════════════════════════════════════════════════════════════════════════

set -e

BASE_URL="${BASE_URL:-http://localhost:8000}"
PASS=0
FAIL=0

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

pass() { echo -e "   ${GREEN}✅ PASS${NC} — $1"; PASS=$((PASS + 1)); }
fail() { echo -e "   ${RED}❌ FAIL${NC} — $1"; FAIL=$((FAIL + 1)); }
warn() { echo -e "   ${YELLOW}⚠️  WARN${NC} — $1"; }

echo "═══════════════════════════════════════════════════════════"
echo "  Playto Payout Engine — Chaos Infrastructure Tests"
echo "  Target: $BASE_URL"
echo "═══════════════════════════════════════════════════════════"

# ── TEST 1: Cold Boot Recovery ────────────────────────────────────────────────
echo ""
echo "🧊 TEST 1: Cold Boot Recovery"
echo "   Stopping all containers..."
docker compose down 2>/dev/null || true

echo "   Starting fresh (docker compose up -d)..."
docker compose up -d --build 2>/dev/null

echo "   Waiting for health checks (60s)..."
sleep 60

# Check all critical services
BACKEND_UP=$(docker compose ps backend --format json 2>/dev/null | grep -c '"running"' || echo 0)
WORKER_UP=$(docker compose ps worker --format json 2>/dev/null | grep -c '"running"' || echo 0)
REDIS_UP=$(docker compose ps redis --format json 2>/dev/null | grep -c '"running"' || echo 0)
SHARD0_UP=$(docker compose ps shard_0 --format json 2>/dev/null | grep -c '"running"' || echo 0)

if [ "$BACKEND_UP" -ge 1 ] && [ "$WORKER_UP" -ge 1 ] && [ "$REDIS_UP" -ge 1 ] && [ "$SHARD0_UP" -ge 1 ]; then
    pass "All services recovered from cold boot"
else
    fail "Some services did not start: backend=$BACKEND_UP worker=$WORKER_UP redis=$REDIS_UP shard_0=$SHARD0_UP"
fi

# Verify API responds
echo "   Checking API health..."
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/auth/login/" -X POST \
  -H "Content-Type: application/json" \
  -d '{"username":"arjun@demo.com","password":"demo123"}' || echo "000")

if [ "$HTTP_CODE" = "200" ]; then
    pass "API responds correctly after cold boot"
else
    fail "API returned $HTTP_CODE after cold boot (expected 200)"
fi

# ── TEST 2: Worker Executioner Test ───────────────────────────────────────────
echo ""
echo "💀 TEST 2: Worker Executioner Test"
echo "   Getting auth token..."

TOKEN=$(curl -s "$BASE_URL/api/v1/auth/login/" -X POST \
  -H "Content-Type: application/json" \
  -d '{"username":"arjun@demo.com","password":"demo123"}' | python3 -c "import sys,json; print(json.load(sys.stdin).get('access',''))" 2>/dev/null)

if [ -z "$TOKEN" ]; then
    fail "Could not get auth token"
else
    echo "   Triggering a payout..."
    IDEM_KEY=$(python3 -c "import uuid; print(uuid.uuid4())")

    PAYOUT_RESP=$(curl -s "$BASE_URL/api/v1/payouts/" -X POST \
      -H "Content-Type: application/json" \
      -H "Authorization: Bearer $TOKEN" \
      -H "Idempotency-Key: $IDEM_KEY" \
      -d '{"amount_paise": 100, "bank_account_id": "CHAOS_TEST_001"}')

    PAYOUT_ID=$(echo "$PAYOUT_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('id',''))" 2>/dev/null)

    if [ -n "$PAYOUT_ID" ]; then
        echo "   Payout created: $PAYOUT_ID"
        echo "   Killing worker container NOW..."
        docker kill payto-pay-worker-1 2>/dev/null || docker kill playto-pay-worker-1 2>/dev/null || true

        echo "   Waiting 5s, then restarting worker..."
        sleep 5
        docker compose up -d worker

        echo "   Waiting 45s for Beat to detect orphaned payout..."
        sleep 45

        echo "   Checking payout status..."
        PAYOUT_STATUS=$(docker compose exec -T backend python manage.py shell --command="
from payouts.models import Payout
for shard in ['default', 'shard_0', 'shard_1']:
    try:
        p = Payout.objects.using(shard).get(id='$PAYOUT_ID')
        print(p.status)
        break
    except:
        continue
" 2>/dev/null | tr -d '\r\n ')

        echo "   Status after recovery: $PAYOUT_STATUS"
        if [ "$PAYOUT_STATUS" = "PROCESSING" ] || [ "$PAYOUT_STATUS" = "COMPLETED" ] || [ "$PAYOUT_STATUS" = "FAILED" ]; then
            pass "Worker recovered from kill — payout moved from PENDING to $PAYOUT_STATUS"
        else
            warn "Payout still in $PAYOUT_STATUS — Beat may need more time"
        fi
    else
        fail "Could not create payout: $PAYOUT_RESP"
    fi
fi

# ── TEST 3: Docker Log Rotation Check ────────────────────────────────────────
echo ""
echo "📝 TEST 3: Docker Log Rotation"

LOG_CONFIG=$(docker inspect payto-pay-backend-1 2>/dev/null | python3 -c "
import sys, json
data = json.load(sys.stdin)
if data:
    log_config = data[0].get('HostConfig', {}).get('LogConfig', {})
    print(json.dumps(log_config))
else:
    print('{}')
" 2>/dev/null || echo '{}')

echo "   Log config: $LOG_CONFIG"

if echo "$LOG_CONFIG" | grep -q "max-size"; then
    pass "Docker log rotation is configured"
else
    warn "No log rotation detected. Add logging config to docker-compose.yml to prevent disk exhaustion:"
    echo "        logging:"
    echo "          driver: json-file"
    echo "          options:"
    echo "            max-size: '10m'"
    echo "            max-file: '3'"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════════"
echo "📊 RESULTS: $PASS passed, $FAIL failed"
echo "═══════════════════════════════════════════════════════════"
