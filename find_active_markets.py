import os
import requests
import time
from typing import List, Dict, Any
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
import base64

# Simple config
ENV = os.getenv("KALSHI_ENV", "prod")
KEY_ID = os.getenv("KALSHI_KEY_ID", "")
try:
    with open(os.getenv("KALSHI_PRIVATE_KEY_PATH", "kalshi_priv.pem"), "rb") as f:
        PRIVATE_KEY = serialization.load_pem_private_key(f.read(), password=None)
except Exception:
    PRIVATE_KEY = None

REST_BASE = "https://api.elections.kalshi.com"

def _now_ms() -> str:
    return str(int(time.time() * 1000))

def _sign_pss_b64(private_key, text: str) -> str:
    sig = private_key.sign(
        text.encode("utf-8"),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode("utf-8")

def make_headers(method: str, path: str):
    ts = _now_ms()
    path_wo_query = path.split("?")[0]
    msg = ts + method.upper() + path_wo_query
    sig = _sign_pss_b64(PRIVATE_KEY, msg)
    return {
        "KALSHI-ACCESS-KEY": KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": sig,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "Content-Type": "application/json",
    }

def fetch_all_markets():
    all_markets = []
    cursor = None
    
    # fetch 5 pages max
    for _ in range(5):
        path = "/trade-api/v2/markets?status=open&limit=200"
        if cursor:
            path += f"&cursor={cursor}"
            
        url = REST_BASE + path
        headers = make_headers("GET", "/trade-api/v2/markets")
        
        print(f"Fetching {url} ...")
        r = requests.get(url, headers=headers)
        r.raise_for_status()
        data = r.json()
        
        batch = []
        next_cursor = None
        
        if isinstance(data, dict):
            if "markets" in data:
                batch = data["markets"]
                next_cursor = data.get("cursor")
            elif "data" in data: # some endpoints use data wrapper
                 # Kalshi V2 often puts cursor in the response body top level
                 pass 
        
        # Adjust parsing based on actual response if needed, 
        # but generally keys are "markets" and "cursor"
        
        if not batch and isinstance(data, dict) and "markets" in data:
             batch = data["markets"]
             next_cursor = data.get("cursor")

        if not batch:
             # try digging deeper or just break
             break
             
        all_markets.extend(batch)
        print(f"  Got {len(batch)} markets. Cursor={next_cursor}")
        
        cursor = next_cursor
        if not cursor:
            break
            
    return all_markets

def main():
    markets = fetch_all_markets()
    print(f"Total fetched: {len(markets)}")
    
    # Sort by volume
    # Note: 'volume' might be total volume or volume_24h. Let's check keys on one.
    if markets:
        print("Sample keys:", markets[0].keys())
        
    markets.sort(key=lambda m: int(m.get("volume") or 0), reverse=True)
    
    print("\nTOP 20 MARKETS BY VOLUME:")
    for m in markets[:20]:
        vol = m.get("volume")
        tik = m.get("ticker")
        title = m.get("title")
        print(f"{vol} | {tik} | {title}")

if __name__ == "__main__":
    main()
