"""Explore events endpoint to find non-sports markets with actual open markets."""
from monitor import load_private_key, make_headers, REST_BASE, PRIVATE_KEY_PATH
import requests

pk = load_private_key(PRIVATE_KEY_PATH)

# Fetch ALL events (paginated)
all_events = []
cursor = None
sign_path = "/trade-api/v2/events"
for page in range(50):
    params = "limit=200"
    if cursor:
        params += f"&cursor={cursor}"
    url = f"{REST_BASE}{sign_path}?{params}"
    headers = make_headers(pk, "GET", sign_path)
    r = requests.get(url, headers=headers, timeout=20)
    data = r.json()
    events = data.get("events", [])
    all_events.extend(events)
    cursor = data.get("cursor")
    print(f"Page {page+1}: {len(events)} events (total: {len(all_events)})")
    if not cursor or not events:
        break

# Count categories
from collections import Counter
cats = Counter(e.get("category", "Unknown") for e in all_events)
print(f"\n=== {len(all_events)} total events ===")
print("\nCategories:")
for cat, count in cats.most_common():
    print(f"  {cat}: {count}")

# Show non-sports events
SPORTS_CATS = {"Sports", "Esports"}
print("\n=== NON-SPORTS EVENTS ===")
non_sports = [e for e in all_events if e.get("category") not in SPORTS_CATS]
for e in non_sports:
    cat = e.get("category", "?")
    title = (e.get("title", ""))[:80]
    et = e.get("event_ticker", "")
    print(f"  [{cat}] {et} | {title}")

# For each non-sports event, check if it has open markets
print(f"\n=== CHECKING OPEN MARKETS FOR {len(non_sports)} NON-SPORTS EVENTS ===")
market_sign_path = "/trade-api/v2/markets"
total_open = 0
for e in non_sports[:50]:  # Check first 50
    et = e.get("event_ticker", "")
    if not et:
        continue
    headers = make_headers(pk, "GET", market_sign_path)
    url = f"{REST_BASE}{market_sign_path}?event_ticker={et}&status=open&limit=50"
    try:
        r = requests.get(url, headers=headers, timeout=10)
        data = r.json()
        markets = data.get("markets", [])
        if markets:
            total_open += len(markets)
            cat = e.get("category", "?")
            title = (e.get("title", ""))[:60]
            print(f"  [{cat}] {et} => {len(markets)} open markets | {title}")
            for m in markets[:2]:
                mt = m.get("ticker", "")
                mtitle = (m.get("title", ""))[:60]
                vol = m.get("volume", 0)
                status = m.get("status", "")
                print(f"    {mt} | vol={vol} | {status} | {mtitle}")
    except Exception as ex:
        print(f"  Error for {et}: {ex}")

print(f"\nTotal open non-sports markets found: {total_open}")
