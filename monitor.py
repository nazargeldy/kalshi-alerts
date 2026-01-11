import asyncio
import base64
import datetime
import json
import logging
import os
import time
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List, Optional

# Load .env if present
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import requests
import websockets
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from storage import TradeStore
from baselines import MarketBaselines
from scoring import score_trade
from clustering import MarketClusterTracker
from cluster_utils import infer_cluster_key
from alerter import Alerter
from alert_manager import AlertManager


# =========================
# LOGGING SETUP
# =========================
os.makedirs("logs", exist_ok=True)
logger = logging.getLogger("kalshi_monitor")
logger.setLevel(logging.INFO)

# 10MB x 5 rotation
fh = RotatingFileHandler("logs/monitor.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding='utf-8')
ch = logging.StreamHandler()

formatter = logging.Formatter('%(asctime)s | %(levelname)s | %(message)s')
fh.setFormatter(formatter)
ch.setFormatter(formatter)

logger.addHandler(fh)
logger.addHandler(ch)


# =========================
# CONFIG (edit these)
# =========================

ENV = os.getenv("KALSHI_ENV", "prod").lower()
KEY_ID = os.getenv("KALSHI_KEY_ID", "").strip()
PRIVATE_KEY_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH", "").strip()

REST_MARKET_LIMIT = int(os.getenv("KALSHI_REST_MARKET_LIMIT", "200"))
SUBSCRIBE_TICKER_LIMIT = int(os.getenv("KALSHI_SUBSCRIBE_TICKER_LIMIT", "100"))

US_POLITICS_KEYWORDS = [
    "president", "trump", "biden", "house", "senate", "congress",
    "election", "primary", "gop", "republican", "democrat", "democratic",
    "supreme court", "scotus", "impeach", "cabinet", "vice president",
    "yes", "no", "nba", "nfl", "points", "wins"
]

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
# REST: market discovery
# =========================

def looks_us_politics(title: str) -> bool:
    t = (title or "").lower()
    return any(k in t for k in US_POLITICS_KEYWORDS)

def fetch_open_markets(private_key) -> List[Dict[str, Any]]:
    path = f"/trade-api/v2/markets?status=open&limit={REST_MARKET_LIMIT}"
    url = REST_BASE + path
    headers = make_headers(private_key, "GET", "/trade-api/v2/markets")

    try:
        r = requests.get(url, headers=headers, timeout=20)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.error(f"Failed to fetch markets: {e}")
        raise

    if isinstance(data, dict) and "markets" in data and isinstance(data["markets"], list):
        return data["markets"]
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "data" in data and isinstance(data["data"], list):
        return data["data"]
    
    raise RuntimeError(f"Unexpected markets response schema: {type(data)}")

def pick_us_politics_tickers(markets: List[Dict[str, Any]]) -> List[str]:
    tickers: List[str] = []
    for m in markets:
        ticker = m.get("ticker") or m.get("market_ticker") or m.get("symbol")
        title = m.get("title") or m.get("name") or ""
        status = (m.get("status") or "").lower()

        if not ticker: continue
        if status and status not in ("open", "active"): continue
        if not looks_us_politics(title): continue
        tickers.append(str(ticker))

    # de-dupe
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
    mtype = msg.get("type")
    if mtype != "trade":
        return None
    data = msg.get("msg") or {}
    ticker = data.get("market_ticker")
    if not ticker:
        return None
    try:
        yes_price = int(data.get("yes_price", 0))
        no_price = int(data.get("no_price", 0))
        count = int(data.get("count", 0))
        ts = int(data.get("ts", 0))
    except (ValueError, TypeError):
        return None
    return TradePrint(ticker, yes_price, no_price, count, ts)


async def heartbeat(store: TradeStore, interval_sec: int = 60):
    """Logs a health summary every minute."""
    logger.info("Heartbeat task started.")
    while True:
        try:
            await asyncio.sleep(interval_sec)
            logger.info("❤️ HEARTBEAT: Monitor is running. DB active.")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Heartbeat error: {e}")

async def ws_listen_trades(private_key, market_tickers: List[str], store: TradeStore) -> None:
    ws_headers = make_headers(private_key, "GET", WS_SIGN_PATH)
    baselines = MarketBaselines()
    clusters = MarketClusterTracker(window_seconds=300)
    alerter = Alerter()
    alert_manager = AlertManager(alerter, daily_cap=20)
    
    # Startup Alert
    start_msg = "✅ Kalshi monitor is live."
    logger.info(start_msg)
    alerter.send(start_msg)

    # Start Heartbeat
    asyncio.create_task(heartbeat(store))

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
                logger.info(f"Connected WS: {WS_URL}")
                logger.info(f"Subscribing to {len(market_tickers)} tickers...")

                sub = {
                    "id": msg_id,
                    "cmd": "subscribe",
                    "params": {
                        "channels": ["trade"],
                        "market_tickers": market_tickers,
                    }
                }
                msg_id += 1
                await ws.send(json.dumps(sub))
                backoff = 1  # reset

                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        logger.warning(f"WS RAW non-json: {raw}")
                        continue

                    if msg.get("type") in ("error", "subscribed", "ok", "unsubscribed"):
                        logger.info(f"WS META: {msg}")
                        if msg.get("type") == "subscribed":
                            logger.info("Watching for trades...")
                        continue

                    trade = parse_trade_message(msg)
                    if trade:
                        dt = datetime.datetime.fromtimestamp(trade.ts)
                        ts_str = dt.strftime("%Y-%m-%d %H:%M:%S")
                        yes_prob = trade.yes_price / 100.0
                        
                        log_line = (f"TRADE | {trade.market_ticker} | yes={trade.yes_price} | "
                                    f"prob={yes_prob:.2f} | k={trade.count} | ts={ts_str}")
                        logger.info(log_line)
                        
                        store.insert_trade(
                            market_ticker=trade.market_ticker,
                            yes_price_cents=trade.yes_price,
                            no_price_cents=trade.no_price,
                            contracts=trade.count,
                            ts_exchange=trade.ts,
                            raw_msg=msg,
                        )

                        baselines.update(
                            market_ticker=trade.market_ticker,
                            ts_received_ms=int(time.time() * 1000),
                            contracts=trade.count,
                            yes_price_cents=trade.yes_price,
                        )

                        snap = baselines.snapshot(trade.market_ticker)
                        volume_proxy = trade.count * trade.yes_price # cents
                        
                        score_result = score_trade(volume_proxy, snap, None)
                        
                        if score_result["score"] >= 1:
                             logger.info(f"⚠️ HIGH SCORE {score_result['score']} | {trade.market_ticker} | {score_result['reasons']}")
                             
                             # 1. Attempt Solo Alert (TEMPORARY TEST: Score >= 1)
                             alert_manager.process_solo_alert(
                                 trade.market_ticker,
                                 score_result["score"],
                                 score_result["reasons"]
                             )
                             
                             # 2. Update Clusters
                             cluster_key = infer_cluster_key(trade.market_ticker)
                             cluster_info = clusters.add_event(
                                 cluster_key, 
                                 trade.market_ticker, 
                                 score_result["score"]
                             )
                             
                             if cluster_info["count"] >= 2:
                                 logger.info(f"🔥 CLUSTER {cluster_key} count={cluster_info['count']} max={cluster_info['max_score']}")
                                 alert_manager.process_cluster_alert(
                                     cluster_key,
                                     cluster_info["count"],
                                     cluster_info["max_score"],
                                     cluster_info["markets"]
                                 )

                        trade_counter += 1
                        if (trade_counter % 50) == 0:
                            store.commit()
                            logger.info("DB Committed batch.")
                    else:
                        pass # Ignore heartbeat checks or other msgs

        except Exception as e:
            logger.error(f"WS disconnected/error: {e!r}")
            logger.info(f"Reconnecting in {backoff}s...")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


def main():
    if not KEY_ID:
        logger.error("Set env var KALSHI_KEY_ID.")
        return
    if not PRIVATE_KEY_PATH:
        logger.error("Set env var KALSHI_PRIVATE_KEY_PATH.")
        return

    private_key = load_private_key(PRIVATE_KEY_PATH)

    logger.info(f"Starting Monitor. ENV={ENV} REST_BASE={REST_BASE}")
    logger.info("Fetching open markets...")
    try:
        markets = fetch_open_markets(private_key)
    except Exception:
        logger.critical("Failed to fetch markets. Exiting.")
        return

    logger.info(f"Open markets fetched: {len(markets)}")
    markets.sort(key=lambda m: int(m.get("volume") or 0), reverse=True)
    
    tickers = pick_us_politics_tickers(markets)
    if not tickers:
        logger.warning("No US-politics markets found with current filter.")
        return

    tickers = tickers[:SUBSCRIBE_TICKER_LIMIT]
    logger.info(f"Selected {len(tickers)} tickers for monitoring.")

    store = TradeStore(db_path="kalshi_trades.db", env=ENV)

    try:
        asyncio.run(ws_listen_trades(private_key, tickers, store))
    except KeyboardInterrupt:
        logger.info("Shutdown requested (SIGINT).")
    except Exception as e:
        logger.critical(f"Fatal error in main loop: {e!r}")
    finally:
        logger.info("Closing database...")
        store.commit()
        store.close()
        logger.info("Goodbye.")


if __name__ == "__main__":
    main()
