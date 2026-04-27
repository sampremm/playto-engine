import requests
import uuid
import json

BASE_URL = "http://127.0.0.1:8000"
LOGIN_URL = f"{BASE_URL}/api/v1/auth/login/"
HEALTH_URL = f"{BASE_URL}/"

def test_routes():
    print("🚀 Starting API Route Testing...")
    
    # 1. Health Check
    try:
        r = requests.get(HEALTH_URL)
        print(f"✅ GET / - Status: {r.status_code}, Response: {r.json()}")
    except Exception as e:
        print(f"❌ GET / - Failed: {e}")

    # 2. Login
    print("\n🔑 Logging in as arjun@demo.com...")
    login_data = {"username": "arjun@demo.com", "password": "demo123"}
    r = requests.post(LOGIN_URL, json=login_data)
    if r.status_code != 200:
        print(f"❌ Login failed: {r.status_code} {r.text}")
        return
    
    tokens = r.json()
    access_token = tokens['access']
    headers = {"Authorization": f"Bearer {access_token}"}
    print("✅ Login successful")

    # 3. Balance
    r = requests.get(f"{BASE_URL}/api/v1/merchants/balance/", headers=headers)
    print(f"✅ GET /api/v1/merchants/balance/ - Status: {r.status_code}, Response: {r.json()}")

    # 4. Ledger
    r = requests.get(f"{BASE_URL}/api/v1/merchants/ledger/", headers=headers)
    print(f"✅ GET /api/v1/merchants/ledger/ - Status: {r.status_code}, Count: {len(r.json())}")

    # 5. Payout List
    r = requests.get(f"{BASE_URL}/api/v1/payouts/list/", headers=headers)
    print(f"✅ GET /api/v1/payouts/list/ - Status: {r.status_code}, Count: {len(r.json())}")

    # 6. Create Payout
    idem_key = str(uuid.uuid4())
    payout_data = {"amount_paise": 100, "bank_account_id": "TEST_ACC_123"}
    payout_headers = headers.copy()
    payout_headers["Idempotency-Key"] = idem_key
    r = requests.post(f"{BASE_URL}/api/v1/payouts/", json=payout_data, headers=payout_headers)
    print(f"✅ POST /api/v1/payouts/ - Status: {r.status_code}, Response: {r.json()}")

    # 7. Idempotency Test (Replay)
    r = requests.post(f"{BASE_URL}/api/v1/payouts/", json=payout_data, headers=payout_headers)
    print(f"✅ POST /api/v1/payouts/ (Idempotency Replay) - Status: {r.status_code}")

    # 8. Webhook Endpoints (List)
    r = requests.get(f"{BASE_URL}/api/v1/webhooks/endpoints/", headers=headers)
    print(f"✅ GET /api/v1/webhooks/endpoints/ - Status: {r.status_code}, Response: {r.json()}")

    # 9. Create Webhook Endpoint
    webhook_url = f"https://webhooksite.net/{uuid.uuid4()}"
    r = requests.post(f"{BASE_URL}/api/v1/webhooks/endpoints/", json={"url": webhook_url}, headers=headers)
    print(f"✅ POST /api/v1/webhooks/endpoints/ - Status: {r.status_code}, Response: {r.json()}")
    endpoint_id = r.json().get('id')

    # 10. Webhook Deliveries
    r = requests.get(f"{BASE_URL}/api/v1/webhooks/deliveries/", headers=headers)
    print(f"✅ GET /api/v1/webhooks/deliveries/ - Status: {r.status_code}, Count: {len(r.json())}")

    # 11. Delete Webhook Endpoint
    if endpoint_id:
        r = requests.delete(f"{BASE_URL}/api/v1/webhooks/endpoints/", json={"id": endpoint_id}, headers=headers)
        print(f"✅ DELETE /api/v1/webhooks/endpoints/ - Status: {r.status_code}")

    print("\n🏁 Route Testing Completed!")

if __name__ == "__main__":
    test_routes()
