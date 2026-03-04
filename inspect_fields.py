"""Quick script to inspect what fields Kalshi API returns for markets."""
import json, os, sys, requests
sys.path.insert(0, os.path.dirname(__file__))
from dotenv import load_dotenv
load_dotenv()
from monitor import load_private_key, fetch_open_markets, make_headers

pk = load_private_key(os.getenv("KALSHI_PRIVATE_KEY_PATH"))
markets = fetch_open_markets(pk)

# Check MVE-specific fields
m = markets[0]
print("=== MVE FIELDS (first market) ===")
print(f"ticker: {m.get('ticker')}")
print(f"event_ticker: {m.get('event_ticker')}")
print(f"title: {m.get('title')}")
print(f"mve_collection_ticker: {m.get('mve_collection_ticker')}")
print(f"mve_selected_legs: {json.dumps(m.get('mve_selected_legs'), indent=2)}")
print(f"market_type: {m.get('market_type')}")

# Check if there's a category for non-MVE markets
# Try fetching more markets with different filters
print("\n=== Trying non-MVE markets (category=politics) ===")
path = "/trade-api/v2/markets?status=open&limit=10&series_ticker=KXSENATETXR"
url = "https://api.elections.kalshi.com" + path
headers = make_headers(pk, "GET", "/trade-api/v2/markets")
try:
    r = requests.get(url, headers=headers, timeout=20)
    if r.status_code == 200:
        data = r.json()
        pol_markets = data.get("markets", data) if isinstance(data, dict) else data
        if isinstance(pol_markets, list):
            for pm in pol_markets[:3]:
                print(json.dumps({k: pm.get(k) for k in ['ticker','event_ticker','title','subtitle','market_type']}, indent=2))
                print("---")
        else:
            print(f"Unexpected: {type(pol_markets)}")
    else:
        print(f"Status: {r.status_code}")
except Exception as e:
    print(f"Error: {e}")

# Also try fetching by event_ticker 
print("\n=== Trying event_ticker filter ===")
path2 = "/trade-api/v2/markets?status=open&limit=5&event_ticker=KXNCAAMBGAME"
url2 = "https://api.elections.kalshi.com" + path2
headers2 = make_headers(pk, "GET", "/trade-api/v2/markets")
try:
    r2 = requests.get(url2, headers=headers2, timeout=20)
    if r2.status_code == 200:
        data2 = r2.json()
        ev_markets = data2.get("markets", data2) if isinstance(data2, dict) else data2
        if isinstance(ev_markets, list):
            print(f"Got {len(ev_markets)} markets")
            for em in ev_markets[:3]:
                print(json.dumps({k: em.get(k) for k in ['ticker','event_ticker','title','subtitle','market_type']}, indent=2))
                print("---")
        else:
            print(f"Unexpected: {type(ev_markets)}")
    else:
        print(f"Status: {r2.status_code}")
except Exception as e:
    print(f"Error: {e}")

print(f"\nTotal markets fetched: {len(markets)}")
