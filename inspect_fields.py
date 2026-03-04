"""Quick script to inspect option labels from Kalshi API."""
import json, os, sys, requests
sys.path.insert(0, os.path.dirname(__file__))
from dotenv import load_dotenv
load_dotenv()
from monitor import load_private_key, fetch_open_markets, make_headers

pk = load_private_key(os.getenv("KALSHI_PRIVATE_KEY_PATH"))

# Check a regular (non-MVE) market for option labels
print("=== NON-MVE MARKET OPTION LABELS ===")
for series in ["KXSENATETXR", "KXATPMATCH", "KXNCAAMBGAME"]:
    path = f"/trade-api/v2/markets?status=open&limit=3&series_ticker={series}"
    url = "https://api.elections.kalshi.com" + path
    headers = make_headers(pk, "GET", "/trade-api/v2/markets")
    try:
        r = requests.get(url, headers=headers, timeout=20)
        if r.status_code == 200:
            data = r.json()
            mlist = data.get("markets", data) if isinstance(data, dict) else data
            if isinstance(mlist, list):
                for m in mlist[:2]:
                    print(f"\n  ticker: {m.get('ticker')}")
                    print(f"  title: {m.get('title')}")
                    print(f"  yes_sub_title: {m.get('yes_sub_title')}")
                    print(f"  no_sub_title: {m.get('no_sub_title')}")
                    print(f"  subtitle: {m.get('subtitle')}")
    except Exception as e:
        print(f"  Error for {series}: {e}")

# Check MVE parlay markets
markets = fetch_open_markets(pk)
print("\n=== MVE PARLAY OPTION LABELS (first 3) ===")
for m in markets[:3]:
    print(f"\n  ticker: {m.get('ticker')}")
    print(f"  title: {m.get('title')[:80]}")
    print(f"  yes_sub_title: {(m.get('yes_sub_title') or '')[:80]}")
    print(f"  no_sub_title: {(m.get('no_sub_title') or '')[:80]}")
    legs = m.get('mve_selected_legs', [])
    if legs:
        print(f"  legs ({len(legs)}):")
        for leg in legs[:3]:
            print(f"    {leg.get('side')} | {leg.get('market_ticker')} | evt: {leg.get('event_ticker')}")
