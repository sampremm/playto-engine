#!/usr/bin/env python3
"""
Infrastructure & Security Tests — Production Readiness
=======================================================

Standalone scripts that test the LIVE Docker deployment.
These run OUTSIDE the Django test runner, against the actual API.

Usage:
    # From the project root, against your live server:
    python tests/test_infrastructure.py --base-url https://playtopay.duckdns.org

    # Or against local Docker:
    python tests/test_infrastructure.py --base-url http://localhost:8000
"""
import argparse
import requests
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed


# ── Test Configuration ────────────────────────────────────────────────────────
DEMO_EMAIL = 'arjun@demo.com'
DEMO_PASSWORD = 'demo123'


def get_token(base_url):
    """Authenticate and return a JWT access token."""
    resp = requests.post(f'{base_url}/api/v1/auth/login/', json={
        'username': DEMO_EMAIL,
        'password': DEMO_PASSWORD,
    }, timeout=10)
    if resp.status_code != 200:
        print(f"❌ LOGIN FAILED: {resp.status_code} — {resp.text}")
        sys.exit(1)
    return resp.json()['access']


def header(token):
    return {'Authorization': f'Bearer {token}'}


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 1: CORS Origin Spoof
# ═══════════════════════════════════════════════════════════════════════════════
def test_cors_origin_spoof(base_url, token):
    """
    Attempt to send a request from an unauthorized origin.
    Django must NOT include Access-Control-Allow-Origin for spoofed domains.
    """
    print("\n🔒 TEST: CORS Origin Spoof")
    print("   Sending request with Origin: http://evil-site.com")

    resp = requests.options(
        f'{base_url}/api/v1/merchants/balance/',
        headers={
            **header(token),
            'Origin': 'http://evil-site.com',
            'Access-Control-Request-Method': 'GET',
        },
        timeout=10,
    )

    cors_header = resp.headers.get('Access-Control-Allow-Origin', '')

    if 'evil-site.com' in cors_header or cors_header == '*':
        print(f"   ❌ FAIL — Server allowed spoofed origin! Header: {cors_header}")
        return False
    else:
        print(f"   ✅ PASS — Spoofed origin correctly blocked. Header: '{cors_header}'")
        return True


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 2: Rate Limiting (Login Flood)
# ═══════════════════════════════════════════════════════════════════════════════
def test_rate_limiting(base_url):
    """
    Send 30 rapid login requests. DRF throttle (20/min for anon) should
    kick in and return 429 Too Many Requests.
    """
    print("\n🚦 TEST: Rate Limiting (Login Flood)")
    print("   Sending 30 rapid login requests...")

    results = {'success': 0, 'throttled': 0, 'error': 0}

    for i in range(30):
        try:
            resp = requests.post(f'{base_url}/api/v1/auth/login/', json={
                'username': DEMO_EMAIL,
                'password': DEMO_PASSWORD,
            }, timeout=5)

            if resp.status_code == 200:
                results['success'] += 1
            elif resp.status_code == 429:
                results['throttled'] += 1
            else:
                results['error'] += 1
        except Exception:
            results['error'] += 1

    print(f"   Results: {results['success']} success, {results['throttled']} throttled, {results['error']} errors")

    if results['throttled'] > 0:
        print("   ✅ PASS — Rate limiting is active")
        return True
    else:
        print("   ⚠️  WARN — No throttling detected. Check DEFAULT_THROTTLE_RATES in settings.py")
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 3: Connection Exhaustion (Concurrent Reads)
# ═══════════════════════════════════════════════════════════════════════════════
def test_connection_exhaustion(base_url, token, concurrent_users=50):
    """
    Hit /api/v1/payouts/list/ with N concurrent users.
    If PostgreSQL crashes with "Too many connections", you need PgBouncer.
    """
    print(f"\n⚡ TEST: Connection Exhaustion ({concurrent_users} concurrent users)")
    print(f"   Hitting /api/v1/payouts/list/ with {concurrent_users} threads...")

    results = {'success': 0, 'error': 0, 'status_codes': {}}

    def make_request(_):
        try:
            resp = requests.get(
                f'{base_url}/api/v1/payouts/list/',
                headers=header(token),
                timeout=15,
            )
            return resp.status_code
        except Exception as e:
            return f'ERR:{e}'

    with ThreadPoolExecutor(max_workers=concurrent_users) as pool:
        futures = [pool.submit(make_request, i) for i in range(concurrent_users)]
        for f in as_completed(futures):
            result = f.result()
            if isinstance(result, int):
                results['status_codes'][result] = results['status_codes'].get(result, 0) + 1
                if result == 200:
                    results['success'] += 1
                else:
                    results['error'] += 1
            else:
                results['error'] += 1
                results['status_codes'][result] = results['status_codes'].get(result, 0) + 1

    print(f"   Status codes: {results['status_codes']}")
    print(f"   {results['success']}/{concurrent_users} succeeded")

    if results['success'] == concurrent_users:
        print("   ✅ PASS — All connections handled gracefully")
        return True
    elif results['error'] > concurrent_users * 0.5:
        print("   ❌ FAIL — More than 50% failed. Consider PgBouncer or CONN_MAX_AGE tuning.")
        return False
    else:
        print("   ⚠️  WARN — Some failures under load. Monitor PostgreSQL connection limits.")
        return True


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 4: Unauthenticated Access
# ═══════════════════════════════════════════════════════════════════════════════
def test_unauthenticated_access(base_url):
    """
    All protected endpoints must return 401 without a token.
    """
    print("\n🔐 TEST: Unauthenticated Access")

    endpoints = [
        ('GET', '/api/v1/merchants/balance/'),
        ('GET', '/api/v1/merchants/ledger/'),
        ('GET', '/api/v1/payouts/list/'),
        ('GET', '/api/v1/webhooks/endpoints/'),
        ('GET', '/api/v1/webhooks/deliveries/'),
        ('POST', '/api/v1/payouts/'),
    ]

    all_pass = True
    for method, path in endpoints:
        try:
            if method == 'GET':
                resp = requests.get(f'{base_url}{path}', timeout=5)
            else:
                resp = requests.post(f'{base_url}{path}', json={}, timeout=5)

            if resp.status_code == 401:
                print(f"   ✅ {method} {path} → 401 (correctly blocked)")
            else:
                print(f"   ❌ {method} {path} → {resp.status_code} (should be 401!)")
                all_pass = False
        except Exception as e:
            print(f"   ❌ {method} {path} → ERROR: {e}")
            all_pass = False

    return all_pass


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 5: Idempotency Under Load (Live API)
# ═══════════════════════════════════════════════════════════════════════════════
def test_idempotency_live(base_url, token):
    """
    Send 10 requests with the SAME idempotency key to the live API.
    All 10 must return the same payout ID.
    """
    print("\n🔁 TEST: Idempotency Under Load (10 duplicate requests)")

    import uuid
    idem_key = str(uuid.uuid4())
    payout_ids = set()
    status_codes = []

    for i in range(10):
        try:
            resp = requests.post(
                f'{base_url}/api/v1/payouts/',
                json={'amount_paise': 100, 'bank_account_id': 'TEST_IDEM_LIVE'},
                headers={
                    **header(token),
                    'Idempotency-Key': idem_key,
                },
                timeout=10,
            )
            status_codes.append(resp.status_code)
            if resp.status_code in (200, 201):
                payout_ids.add(resp.json().get('id'))
        except Exception as e:
            status_codes.append(f'ERR:{e}')

    print(f"   Status codes: {status_codes}")
    print(f"   Unique payout IDs: {len(payout_ids)}")

    if len(payout_ids) == 1:
        print("   ✅ PASS — All 10 requests returned the same payout ID (idempotent)")
        return True
    elif len(payout_ids) == 0:
        print("   ⚠️  WARN — No successful payouts (check balance or auth)")
        return False
    else:
        print(f"   ❌ FAIL — Got {len(payout_ids)} different payout IDs! Idempotency is broken!")
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(description='Playto Payout Engine — Infrastructure Tests')
    parser.add_argument('--base-url', default='http://localhost:8000',
                        help='Base URL of the API (default: http://localhost:8000)')
    parser.add_argument('--concurrent-users', type=int, default=50,
                        help='Number of concurrent users for load test (default: 50)')
    args = parser.parse_args()

    base_url = args.base_url.rstrip('/')
    print(f"🏗️  Playto Payout Engine — Production Test Suite")
    print(f"   Target: {base_url}")
    print("=" * 60)

    # Authenticate
    print("\n🔑 Authenticating...")
    token = get_token(base_url)
    print(f"   Token acquired: {token[:20]}...")

    # Run all tests
    results = {}
    results['CORS Origin Spoof'] = test_cors_origin_spoof(base_url, token)
    results['Unauthenticated Access'] = test_unauthenticated_access(base_url)
    results['Rate Limiting'] = test_rate_limiting(base_url)
    results['Connection Exhaustion'] = test_connection_exhaustion(base_url, token, args.concurrent_users)
    results['Idempotency (Live)'] = test_idempotency_live(base_url, token)

    # Summary
    print("\n" + "=" * 60)
    print("📊 RESULTS SUMMARY")
    print("=" * 60)
    passed = 0
    failed = 0
    for name, result in results.items():
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"   {status}  {name}")
        if result:
            passed += 1
        else:
            failed += 1

    print(f"\n   Total: {passed} passed, {failed} failed out of {len(results)}")
    sys.exit(0 if failed == 0 else 1)


if __name__ == '__main__':
    main()
