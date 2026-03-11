"""Debug: check why non-sports markets not matching."""
from monitor import (
    load_private_key, make_headers, fetch_non_sports_event_tickers,
    REST_BASE, PRIVATE_KEY_PATH
)
import requests

pk = load_private_key(PRIVATE_KEY_PATH)

# Get non-sports events
non_sports = fetch_non_sports_event_tickers(pk)
print(f"\nNon-sports events: {len(non_sports)}")

# Fetch page 1 of open markets and check their event_tickers
sign_path = "/trade-api/v2/markets"
headers = make_headers(pk, "GET", sign_path)

# Try different status values
for status_val in ["open", "active", ""]:
    params = f"limit=10"
    if status_val:
        params += f"&status={status_val}"
    url = f"{REST_BASE}{sign_path}?{params}"
    headers = make_headers(pk, "GET", sign_path)
    r = requests.get(url, headers=headers, timeout=20)
    data = r.json()
    markets = data.get("markets", [])
    print(f"\n--- status={status_val or '(none)'}: {len(markets)} markets ---")
    for m in markets[:5]:
        t = m.get("ticker", "?")
        et = m.get("event_ticker", "?")
        st = m.get("status", "?")
        title = (m.get("title", ""))[:60]
        in_set = et in non_sports
        print(f"  {t} | status={st} | event={et} | in_nonsports={in_set} | {title}")

# Now try fetching a known non-sports event's markets directly
print("\n--- Direct event query for crypto ---")
for et in list(non_sports)[:5]:
    if "BTC" in et or "INX" in et or "TRUMP" in et:
        headers = make_headers(pk, "GET", sign_path)
        url = f"{REST_BASE}{sign_path}?event_ticker={et}&limit=5"
        r = requests.get(url, headers=headers, timeout=10)
        data = r.json()
        markets = data.get("markets", [])
        if markets:
            m = markets[0]
            print(f"  {et} -> {len(markets)} markets, status={m.get('status')}, ticker={m.get('ticker')}")
            break
