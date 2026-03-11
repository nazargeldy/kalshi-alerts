"""Explore Kalshi API to find non-sports markets and understand categories."""
from monitor import load_private_key, make_headers, REST_BASE, PRIVATE_KEY_PATH
import requests

pk = load_private_key(PRIVATE_KEY_PATH)

# 1. Try the events endpoint to see categories
print("=== TRYING EVENTS ENDPOINT ===")
for endpoint in ["/trade-api/v2/events", "/trade-api/v2/series"]:
    headers = make_headers(pk, "GET", endpoint)
    url = f"{REST_BASE}{endpoint}?limit=20"
    try:
        r = requests.get(url, headers=headers, timeout=20)
        print(f"\n{endpoint} -> status={r.status_code}")
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, dict):
                print(f"  Keys: {list(data.keys())}")
                for key in data:
                    if isinstance(data[key], list) and len(data[key]) > 0:
                        print(f"  {key}[0] keys: {list(data[key][0].keys()) if isinstance(data[key][0], dict) else type(data[key][0])}")
                        # Show first 3 items
                        for item in data[key][:3]:
                            if isinstance(item, dict):
                                cat = item.get("category", item.get("series_ticker", "?"))
                                title = item.get("title", item.get("event_ticker", ""))[:80]
                                print(f"    {cat} | {title}")
    except Exception as e:
        print(f"  Error: {e}")

# 2. Fetch ALL markets and count unique ticker prefixes
print("\n=== SCANNING ALL MARKET TICKER PREFIXES ===")
all_markets = []
cursor = None
sign_path = "/trade-api/v2/markets"
for page in range(50):
    params = f"status=open&limit=200"
    if cursor:
        params += f"&cursor={cursor}"
    url = f"{REST_BASE}{sign_path}?{params}"
    headers = make_headers(pk, "GET", sign_path)
    r = requests.get(url, headers=headers, timeout=20)
    data = r.json()
    markets = data.get("markets", [])
    all_markets.extend(markets)
    cursor = data.get("cursor")
    if not cursor or not markets:
        break

print(f"\nTotal open markets: {len(all_markets)}")

# Count prefix frequency
from collections import Counter
prefixes = Counter()
for m in all_markets:
    ticker = m.get("ticker", "")
    # Get prefix before first dash
    prefix = ticker.split("-")[0] if "-" in ticker else ticker
    prefixes[prefix] += 1

# Show all prefixes sorted by count
print(f"\nUnique prefixes: {len(prefixes)}")
for prefix, count in prefixes.most_common():
    # Show a sample title for this prefix
    sample = ""
    for m in all_markets:
        if m.get("ticker", "").startswith(prefix):
            sample = (m.get("title") or "")[:60]
            break
    is_sports = "KXMVE" in prefix or "GAME" in prefix or "MATCH" in prefix
    marker = "  [SPORTS?]" if is_sports else ""
    print(f"  {prefix}: {count} markets{marker} | {sample}")
