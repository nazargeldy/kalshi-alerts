import asyncio
import base64
import datetime
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests
import websockets
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from storage import TradeStore
from baselines import MarketBaselines
from scoring import score_trade


# =========================
# CONFIG (edit these)
# =========================

ENV = os.getenv("KALSHI_ENV", "prod").lower()  # "prod" or "demo"
KEY_ID = os.getenv("KALSHI_KEY_ID", "").strip()
PRIVATE_KEY_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH", "").strip()

# Pull up to this many markets, then subscribe to up to this many tickers.
REST_MARKET_LIMIT = int(os.getenv("KALSHI_REST_MARKET_LIMIT", "200"))
SUBSCRIBE_TICKER_LIMIT = int(os.getenv("KALSHI_SUBSCRIBE_TICKER_LIMIT", "100"))

# "US politics only" keyword filter (edit freely)
US_POLITICS_KEYWORDS = [
    "president", "trump", "biden", "house", "senate", "congress",
    "election", "primary", "gop", "republican", "democrat", "democratic",
    "supreme court", "scotus", "impeach", "cabinet", "vice president",
    "yes", "no", "nba", "nfl", "points", "wins" 
]


# =========================
# Kalshi endpoints (per docs)
# =========================
# WebSocket:
# prod: wss://api.elections.kalshi.com/trade-api/ws/v2
# demo: wss://demo-api.kalshi.co/trade-api/ws/v2
#
# REST host (Python SDK docs show host https://api.elections.kalshi.com/trade-api/v2)
# We'll use:
# prod: https://api.elections.kalshi.com
# demo: https://demo-api.kalshi.co
#
# Docs: WS URL and signing path "/trade-api/ws/v2" :contentReference[oaicite:1]{index=1}

if ENV == "demo":
    REST_BASE = "https://demo-api.kalshi.co"
    WS_URL = "wss://demo-api.kalshi.co/trade-api/ws/v2"
else:
    REST_BASE = "https://api.elections.kalshi.com"
    WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"

WS_SIGN_PATH = "/trade-api/ws/v2"


# =========================
# Auth helpers (RSA-PSS)
# =========================

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

def make_headers(private_key, method: str, path: str) -> Dict[str, str]:
    """
    Kalshi signing rule (docs): sign timestamp + method + path_without_query
    and send headers:
      KALSHI-ACCESS-KEY
      KALSHI-ACCESS-SIGNATURE
      KALSHI-ACCESS-TIMESTAMP
    :contentReference[oaicite:2]{index=2}
    """
    ts = _now_ms()
    path_wo_query = path.split("?")[0]
    msg = ts + method.upper() + path_wo_query
    sig = _sign_pss_b64(private_key, msg)
    return {
        "KALSHI-ACCESS-KEY": KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": sig,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "Content-Type": "application/json",
    }

def load_private_key(path: str):
    with open(path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


# =========================
# REST: market discovery (simple)
# =========================

def looks_us_politics(title: str) -> bool:
    t = (title or "").lower()
    return any(k in t for k in US_POLITICS_KEYWORDS)

def fetch_open_markets(private_key) -> List[Dict[str, Any]]:
    """
    Tries common markets endpoint: GET /trade-api/v2/markets?status=open&limit=...
    If your response schema differs, print it once and adjust parsing.
    """
    path = f"/trade-api/v2/markets?status=open&limit={REST_MARKET_LIMIT}"
    url = REST_BASE + path
    headers = make_headers(private_key, "GET", "/trade-api/v2/markets")  # sign without query :contentReference[oaicite:3]{index=3}

    r = requests.get(url, headers=headers, timeout=20)
    r.raise_for_status()
    data = r.json()

    # Common pattern: {"markets":[...]} but we handle a couple variants.
    if isinstance(data, dict) and "markets" in data and isinstance(data["markets"], list):
        return data["markets"]
    if isinstance(data, list):
        return data

    # Fallback: try "data" key
    if isinstance(data, dict) and "data" in data and isinstance(data["data"], list):
        return data["data"]

    raise RuntimeError(f"Unexpected markets response schema: keys={list(data.keys()) if isinstance(data, dict) else type(data)}")


def pick_us_politics_tickers(markets: List[Dict[str, Any]]) -> List[str]:
    tickers: List[str] = []

    for m in markets:
        # Kalshi commonly uses "ticker" and "title" fields.
        ticker = m.get("ticker") or m.get("market_ticker") or m.get("symbol")
        title = m.get("title") or m.get("name") or ""
        status = (m.get("status") or "").lower()

        if not ticker:
            continue
        if status and status not in ("open", "active"):
            continue
        if not looks_us_politics(title):
            continue

        tickers.append(str(ticker))

    # de-dupe preserving order
    seen = set()
    out = []
    for t in tickers:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


# =========================
# WS: subscribe + print trades
# =========================

@dataclass
class TradePrint:
    market_ticker: str
    yes_price: int
    no_price: int
    count: int
    ts: int

def parse_trade_message(msg: Dict[str, Any]) -> Optional[TradePrint]:
    """
    Parses 'trade' message type from Kalshi WS.
    Schema:
    {
      "type": "trade",
      "msg": {
        "market_ticker": "...",
        "yes_price": 32, (cents)
        "no_price": 68, (cents)
        "count": 591,   (contracts)
        "ts": 1767723264 (unix seconds)
      }
    }
    """
    mtype = msg.get("type")
    if mtype != "trade":
        return None

    data = msg.get("msg") or {}
    
    ticker = data.get("market_ticker")
    if not ticker:
        return None

    # Essential fields
    try:
        yes_price = int(data.get("yes_price", 0))
        no_price = int(data.get("no_price", 0))
        count = int(data.get("count", 0))
        ts = int(data.get("ts", 0))
    except (ValueError, TypeError):
        return None

    return TradePrint(
        market_ticker=ticker,
        yes_price=yes_price,
        no_price=no_price,
        count=count,
        ts=ts
    )

async def ws_listen_trades(private_key, market_tickers: List[str], store: TradeStore) -> None:
    ws_headers = make_headers(private_key, "GET", WS_SIGN_PATH)

    baselines = MarketBaselines()

    # Basic reconnect loop
    backoff = 1
    msg_id = 1
    trade_counter = 0

    while True:
        try:
            async with websockets.connect(
                WS_URL,
                additional_headers=ws_headers,
                ping_interval=20,
                ping_timeout=20,
                max_queue=1000,
            ) as ws:
                print(f"\n✅ Connected WS: {WS_URL}")
                print(f"📌 Subscribing to {len(market_tickers)} market tickers on channel 'trades'...\n")

                sub = {
                    "id": msg_id,
                    "cmd": "subscribe",
                    "params": {
                        "channels": ["trade"],  # Changed "trades" to "trade" based on error "Unknown channel name"
                        "market_tickers": market_tickers,
                    }
                }
                msg_id += 1
                await ws.send(json.dumps(sub))

                backoff = 1  # reset after successful connect

                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        print("WS RAW (non-json):", raw)
                        continue

                    # Print errors / subscription acks clearly
                    if msg.get("type") in ("error", "subscribed", "ok", "unsubscribed"):
                        print("WS META:", msg)
                        continue

                    trade = parse_trade_message(msg)
                    if trade:
                        # Raw message (comment out if too noisy)
                        # print("WS RAW:", msg)

                        # Clean summary:
                        # Convert ts to readable
                        dt = datetime.datetime.fromtimestamp(trade.ts)
                        ts_str = dt.strftime("%Y-%m-%d %H:%M:%S")

                        # Calc prob
                        yes_prob = trade.yes_price / 100.0
                        
                        print(
                            f"TRADE | {trade.market_ticker} | yes_price_cents={trade.yes_price} | "
                            f"yes_prob={yes_prob:.2f} | no_price_cents={trade.no_price} | "
                            f"contracts={trade.count} | ts={ts_str}"
                        )
                        
                        store.insert_trade(
                            market_ticker=trade.market_ticker,
                            yes_price_cents=trade.yes_price,
                            no_price_cents=trade.no_price,
                            contracts=trade.count,
                            ts_exchange=trade.ts,
                            raw_msg=msg,
                        )
                        # print("DB INSERT OK") # Verification debug

                        # Update baselines
                        baselines.update(
                            market_ticker=trade.market_ticker,
                            ts_received_ms=int(time.time() * 1000),
                            contracts=trade.count,
                            yes_price_cents=trade.yes_price,
                        )

                        # Score Trade (Step 4)
                        snap = baselines.snapshot(trade.market_ticker)
                        volume_proxy = trade.count * trade.yes_price # cents
                        
                        score_result = score_trade(
                            volume_proxy=volume_proxy,
                            baselines=snap,
                            hours_to_close=None
                        )
                        
                        if score_result["score"] >= 60:
                             print(f"⚠️ SCORE {score_result['score']} | {trade.market_ticker} | {score_result['reasons']}")

                        trade_counter += 1
                        
                        if trade_counter % 20 == 0:
                            snap = baselines.snapshot(trade.market_ticker)
                            print("BASELINE SNAPSHOT:", trade.market_ticker, snap)

                        # Commit every ~50 inserts to avoid disk spam
                        if (trade_counter % 50) == 0:
                            store.commit()
                    else:
                        # If you want to see what types you're getting:
                        # print("WS MSG TYPE:", msg.get("type"))
                        pass

        except Exception as e:
            print(f"\n⚠️ WS disconnected/error: {e!r}")
            print(f"Reconnecting in {backoff}s...")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)  # cap at 30s


def main():
    if not KEY_ID:
        raise SystemExit("Set env var KALSHI_KEY_ID to your API Key ID.")
    if not PRIVATE_KEY_PATH:
        raise SystemExit("Set env var KALSHI_PRIVATE_KEY_PATH to your private key .pem/.key path.")

    private_key = load_private_key(PRIVATE_KEY_PATH)

    print(f"ENV={ENV} REST_BASE={REST_BASE} WS_URL={WS_URL}")
    print("Fetching open markets...")
    markets = fetch_open_markets(private_key)
    print(f"Open markets fetched: {len(markets)}")

    # Sort by volume desc to ensure we subscribe to active markets
    markets.sort(key=lambda m: int(m.get("volume") or 0), reverse=True)
    
    print("\nTop 3 markets by volume:")
    for m in markets[:3]:
        print(f" - {m.get('ticker')}: Vol={m.get('volume')} OI={m.get('open_interest')}")

    tickers = pick_us_politics_tickers(markets)
    if not tickers:
        print("\nNo US-politics markets matched your keyword filter.")
        print("Fix: print a couple market titles from the REST response and adjust US_POLITICS_KEYWORDS.")
        # show a few random-ish market titles to help you tune:
        shown = 0
        for m in markets[:50]:
            t = m.get("ticker") or m.get("market_ticker")
            title = m.get("title") or m.get("name")
            if t and title:
                print(f" - {t}: {title}")
                shown += 1
            if shown >= 15:
                break
        return

    tickers = tickers[:SUBSCRIBE_TICKER_LIMIT]
    print(f"\nUS-politics tickers selected ({len(tickers)}):")
    for t in tickers[:20]:
        print(" -", t)
    if len(tickers) > 20:
        print(f" ... +{len(tickers)-20} more")

    store = TradeStore(db_path="kalshi_trades.db", env=ENV)

    try:
        asyncio.run(ws_listen_trades(private_key, tickers, store))
    except KeyboardInterrupt:
        print("\nShutdown requested...")
    finally:
        print("Closing database...")
        store.commit()
        store.close()


if __name__ == "__main__":
    main()
