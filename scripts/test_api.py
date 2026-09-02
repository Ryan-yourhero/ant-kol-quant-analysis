import requests, json

BASE = "http://localhost:8000/api"

# Test health
r = requests.get(f"{BASE}/health")
print(f"Health: {r.status_code} {r.json()}")

# Test today operations
r = requests.get(f"{BASE}/operations/today?page=1&page_size=5")
d = r.json()
print(f"\nToday ops: total={d['total']}, items={len(d['items'])}")
for i in d["items"]:
    print(f"  {i['kol_name']} | {i['operation_type']} | {i['fund_name']}")

# Test kols
r = requests.get(f"{BASE}/kols")
kols = r.json()
print(f"\nKOLs: {len(kols)}")
for k in kols:
    print(f"  {k['name']} ({k['operation_count']} ops)")

# Test history
r = requests.get(f"{BASE}/operations/history", params={"page": 1, "page_size": 3})
d = r.json()
print(f"\nHistory: total={d['total']}, items={len(d['items'])}")

# Test current run
r = requests.get(f"{BASE}/runs/current")
print(f"\nCurrent run: {r.json()}")

print("\nAll API tests passed!")
