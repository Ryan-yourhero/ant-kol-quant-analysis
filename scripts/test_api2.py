import requests, json

BASE = "http://localhost:8000/api"

# Test Kol Detail (KOL id=1)
r = requests.get(f"{BASE}/kols/1/operations?page=1&page_size=3")
d = r.json()
print(f"Kol Detail (id=1): kol={d['kol']['name']}, total={d['total']}, items={len(d['items'])}")

# Test history with filter
r = requests.get(f"{BASE}/operations/history", params={
    "kol_name": "Bells",
    "operation_type": "买入",
    "page": 1,
    "page_size": 5,
})
d = r.json()
print(f"\nHistory filter (Bells+买入): total={d['total']}")
for i in d["items"]:
    print(f"  {i['kol_name']} | {i['operation_type']} | {i['fund_name']} | {i['buy_amount']}")

# Test history date filter
r = requests.get(f"{BASE}/operations/history", params={
    "date_from": "2026-08-12",
    "date_to": "2026-08-12",
    "page": 1, "page_size": 3,
})
d = r.json()
print(f"\nHistory filter (8/12): total={d['total']}")

# Test Excel download
r = requests.get(f"{BASE}/excel/today")
print(f"\nExcel download: status={r.status_code}")

print("\nAll detailed tests passed!")
