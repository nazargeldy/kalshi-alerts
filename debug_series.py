"""Test series-based market fetching for non-sports markets."""
from monitor import (
    load_private_key, make_headers, REST_BASE, PRIVATE_KEY_PATH
)
import requests

pk = load_private_key(PRIVATE_KEY_PATH)

# Step 1: Fetch all events, collect non-sports series_tickers
SPORTS_CATS = {"Sports", "Esports"}
series_by_cat = {}  # {cat: set of series_tickers}
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
    for e in events:
        cat = e.get("category", "")
        st = e.get("series_ticker", "")
        if cat not in SPORTS_CATS and st:
            series_by_cat.setdefault(cat, set()).add(st)
    cursor = data.get("cursor")
    if not cursor or not events:
        break

print("=== NON-SPORTS SERIES BY CATEGORY ===")
total_series = 0
for cat, series in sorted(series_by_cat.items()):
    total_series += len(series)
    print(f"\n{cat} ({len(series)} series):")
    for s in sorted(series)[:10]:
        print(f"  {s}")
    if len(series) > 10:
        print(f"  ... +{len(series) - 10} more")

print(f"\nTotal unique non-sports series: {total_series}")

# Step 2: For some series, try fetching their markets
print("\n=== FETCHING MARKETS FOR SAMPLE SERIES ===")
market_path = "/trade-api/v2/markets"
sample_series = []
for cat, series in series_by_cat.items():
    for s in sorted(series)[:2]:
        sample_series.append((cat, s))

for cat, st in sample_series[:15]:
    headers = make_headers(pk, "GET", market_path)
    url = f"{REST_BASE}{market_path}?series_ticker={st}&limit=5"
    r = requests.get(url, headers=headers, timeout=10)
    data = r.json()
    markets = data.get("markets", [])
    if markets:
        m = markets[0]
        status = m.get("status", "?")
        title = (m.get("title", ""))[:60]
        print(f"  [{cat}] {st} => {len(markets)} markets, status={status} | {title}")
    else:
        print(f"  [{cat}] {st} => 0 markets")
