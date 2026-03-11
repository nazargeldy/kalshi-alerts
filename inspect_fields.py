"""Inspect all fields returned by Kalshi markets API for sample markets."""
import json, os, sys, requests
sys.path.insert(0, os.path.dirname(__file__))
from dotenv import load_dotenv
load_dotenv()
from monitor import load_private_key, make_headers, REST_BASE

pk = load_private_key(os.getenv("KALSHI_PRIVATE_KEY_PATH"))
sign_path = "/trade-api/v2/markets"

# Check a few different series for variety
for series in ["KXOSCARACTO", "KXFEDDECISION", "CONTROLS"]:
    headers = make_headers(pk, "GET", sign_path)
    r = requests.get(f"{REST_BASE}{sign_path}?series_ticker={series}&limit=2", headers=headers, timeout=10)
    markets = r.json().get("markets", [])
    if markets:
        m = markets[0]
        print(f"\n=== {series} / {m.get('ticker')} ===")
        for k, v in sorted(m.items()):
            print(f"  {k}: {v}")
        print()
