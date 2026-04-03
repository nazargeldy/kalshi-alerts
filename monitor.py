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
from polymarket_monitor import poly_trade_loop


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
SUBSCRIBE_TICKER_LIMIT = int(os.getenv("KALSHI_SUBSCRIBE_TICKER_LIMIT", "200"))

# ── Categories that are entirely sports/entertainment — block all ──
BLOCKED_CATEGORIES = {
    "Sports", "Esports",
}

# ── Series ticker prefixes that are always sports ──
SPORTS_TICKER_PREFIXES = (
    "KXMVE",                                              # parlays/combos
    "KXNBA", "KXNFL", "KXMLB", "KXNHL", "KXMLS",
    "KXNCAA", "KXUFC", "KXBOXING", "KXPGA",
    "KXATP", "KXWTA", "KXTENNIS", "KXGOLF",
    "KXSOCCER", "KXCRICKET", "KXF1", "KXNASCAR",
    "KXSPORTS", "KXEPL", "KXFIFA", "KXCFL",
    "KXNHLJN",                                            # NHL junior
)

# ── Title keywords that identify a sports market regardless of category ──
SPORTS_TITLE_KEYWORDS = [
    # leagues / tournaments
    "nba", "nfl", "mlb", "nhl", "mls", "ncaa", "ufc",
    "pga tour", "lpga", " atp ", " wta ", "wimbledon",
    "super bowl", "world series", "stanley cup", "champions league",
    "premier league", "la liga", "serie a", "bundesliga",
    "fifa world cup", "euro 2024", "euro 2026",
    "copa america", "nba finals", "nba playoffs",
    # game-specific jargon
    "touchdown", "home run", "slam dunk", "hat trick",
    "quarterback", "pitcher", "rebounds", "assists",
    "strikeouts", "innings", "halftime", "overtime",
    "parlay", "moneyline",
    # match result patterns
    "will win the match", "win the game", "win the series",
    "cover the spread",
]


# =========================
# Title & URL helpers
# =========================

def _build_kalshi_url(market: dict) -> str:
    """Build a working Kalshi web URL.
    Confirmed format: https://kalshi.com/markets/{series_ticker}/{event_ticker}
    Both lowercase. series_ticker is injected as _series_ticker by fetch_markets_for_series.
    """
    st = market.get("_series_ticker", "")
    et = market.get("event_ticker", "")
    if st and et:
        return f"https://kalshi.com/markets/{st.lower()}/{et.lower()}"
    if et:
        return f"https://kalshi.com/markets/{et.lower()}"
    if st:
        return f"https://kalshi.com/markets/{st.lower()}"
    return "https://kalshi.com/browse"


def _is_mve_market(ticker: str) -> bool:
    """Check if this is a multi-variable event (parlay/combo) market."""
    return "KXMVE" in (ticker or "").upper()


def _clean_parlay_title(raw_title: str, max_legs: int = 3) -> str:
    """Turn 'yes Struff,yes Brooksby,yes Cristian' into '3-Leg Parlay: Struff, Brooksby, Cristian'."""
    legs = [l.strip() for l in raw_title.split(",") if l.strip()]
    clean_legs = []
    for leg in legs:
        # Remove 'yes '/'no ' prefix
        for prefix in ("yes ", "no "):
            if leg.lower().startswith(prefix):
                leg = leg[len(prefix):]
                break
        clean_legs.append(leg)

    total = len(clean_legs)
    if total == 0:
        return raw_title

    shown = clean_legs[:max_legs]
    label = f"🎲 {total}-Leg Parlay: " + ", ".join(shown)
    if total > max_legs:
        label += f" +{total - max_legs} more"
    return label


def _build_market_link(market: dict) -> str:
    """Return the best working Kalshi URL for this market dict."""
    return _build_kalshi_url(market)


def _build_market_title(market: dict) -> str:
    """Return a clean display title for the market."""
    ticker = market.get("ticker", "")
    raw_title = market.get("title") or market.get("name") or ticker

    if _is_mve_market(ticker):
        return _clean_parlay_title(raw_title)
    return raw_title


def _build_option_labels(market: dict) -> tuple:
    """Return (yes_label, no_label) for display in alerts.
    Uses yes_sub_title / no_sub_title when available, falls back to Yes/No.
    """
    yes_sub = (market.get("yes_sub_title") or "").strip()
    no_sub = (market.get("no_sub_title") or "").strip()

    # If both subtitles are identical (e.g. both "Ethan Hawke"), just use Yes/No
    if yes_sub and no_sub and yes_sub.lower() != no_sub.lower():
        return (f"✅ {yes_sub}", f"❌ {no_sub}")
    return ("✅ Yes", "❌ No")

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

def fetch_non_sports_series(private_key, max_pages: int = 50) -> List[str]:
    """Fetch events, return unique non-sports series_tickers sorted by frequency (most active first)."""
    from collections import Counter
    series_count = Counter()
    cursor = None
    sign_path = "/trade-api/v2/events"

    for page in range(max_pages):
        params = "limit=200"
        if cursor:
            params += f"&cursor={cursor}"
        url = f"{REST_BASE}{sign_path}?{params}"
        headers = make_headers(private_key, "GET", sign_path)

        try:
            r = requests.get(url, headers=headers, timeout=20)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            logger.error(f"Events fetch page {page} error: {e}")
            break

        events = data.get("events", [])
        next_cursor = data.get("cursor")

        for e in events:
            cat = e.get("category", "")
            if cat not in BLOCKED_CATEGORIES:
                st = e.get("series_ticker", "")
                if st:
                    series_count[st] += 1

        if not next_cursor or not events:
            break
        cursor = next_cursor

    # Sort by event count (most active series first)
    sorted_series = [s for s, _ in series_count.most_common()]
    logger.info(f"Events scan: {len(sorted_series)} non-sports series found.")
    return sorted_series


def fetch_markets_for_series(private_key, series_tickers: List[str]) -> List[Dict[str, Any]]:
    """Fetch active markets for each series_ticker. One API call per series."""
    all_markets = []
    sign_path = "/trade-api/v2/markets"

    for i, st in enumerate(series_tickers):
        headers = make_headers(private_key, "GET", sign_path)
        url = f"{REST_BASE}{sign_path}?series_ticker={st}&limit=200"

        try:
            r = requests.get(url, headers=headers, timeout=10)
            r.raise_for_status()
            data = r.json()
            markets = data.get("markets", [])
            # Only keep active/open markets
            active = [m for m in markets if m.get("status") in ("active", "open")]
            for m in active:
                m["_series_ticker"] = st  # tag for URL building
            all_markets.extend(active)
        except Exception as e:
            logger.warning(f"Markets fetch error for series {st}: {e}")

        if (i + 1) % 100 == 0:
            logger.info(f"Series scan: {i+1}/{len(series_tickers)} done, {len(all_markets)} markets")

        # Small delay every 10 requests to avoid rate limiting
        if (i + 1) % 10 == 0:
            time.sleep(0.1)

    logger.info(f"Fetched {len(all_markets)} active markets from {len(series_tickers)} series.")
    return all_markets


def _is_sports_market(ticker: str, title: str) -> bool:
    """Return True if this market should be excluded as sports/entertainment."""
    t_upper = ticker.upper()
    if t_upper.startswith(SPORTS_TICKER_PREFIXES):
        return True
    title_lower = title.lower()
    if any(kw in title_lower for kw in SPORTS_TITLE_KEYWORDS):
        return True
    return False


def pick_target_tickers(markets: List[Dict[str, Any]]) -> List[str]:
    """Extract unique non-sports tickers from market list."""
    seen = set()
    out = []
    for m in markets:
        ticker = m.get("ticker") or m.get("market_ticker") or m.get("symbol")
        if not ticker:
            continue
        status = (m.get("status") or "").lower()
        if status and status not in ("open", "active"):
            continue
        t = str(ticker)
        title = m.get("title") or m.get("name") or ""
        if _is_sports_market(t, title):
            continue
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

    def _to_cents(v: Any) -> int:
        """Parse websocket price fields into cents (0-100)."""
        if v is None or v == "":
            return 0
        n = float(v)
        # yes_dollars_fp/no_dollars_fp may come as dollars in [0,1]
        if 0 <= n <= 1:
            return int(round(n * 100))
        return int(round(n))

    try:
        yes_price = _to_cents(data.get("yes_price") if data.get("yes_price") is not None else data.get("yes_dollars_fp"))
        no_price = _to_cents(data.get("no_price") if data.get("no_price") is not None else data.get("no_dollars_fp"))
        count = int(data.get("count", 0))
        ts = int(data.get("ts", 0))
    except (ValueError, TypeError) as e:
        logger.warning(f"Price parse fail: {data} -> {e}")
        return None

    if not (0 <= yes_price <= 100 and 0 <= no_price <= 100):
        return None

    return TradePrint(ticker, yes_price, no_price, count, ts)


async def heartbeat(store: TradeStore, alert_manager, interval_sec: int = 60):
    """Logs a health summary every minute."""
    logger.info("Heartbeat task started.")
    while True:
        try:
            await asyncio.sleep(interval_sec)
            logger.info(
                f"❤️ HEARTBEAT | alerts_today={alert_manager.alerts_sent_today}/{alert_manager.daily_cap} "
                f"| mode={alert_manager.alert_mode}"
            )
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Heartbeat error: {e}")

async def refresh_markets_periodically(private_key, state: dict, interval_hours: float = 2.0):
    """Re-fetch open markets every N hours, updating tickers + ticker_map."""
    interval_sec = interval_hours * 3600
    logger.info(f"Market-refresh task started (every {interval_hours}h).")
    while True:
        try:
            await asyncio.sleep(interval_sec)
            logger.info("🔄 Refreshing open markets...")
            # Re-scan events for non-sports series, then fetch their markets
            series = await asyncio.get_event_loop().run_in_executor(
                None, fetch_non_sports_series, private_key
            )
            markets = await asyncio.get_event_loop().run_in_executor(
                None, fetch_markets_for_series, private_key, series
            )
            markets.sort(key=lambda m: int(m.get("volume") or 0), reverse=True)
            new_tickers = pick_target_tickers(markets)[:SUBSCRIBE_TICKER_LIMIT]
            
            new_map = {}
            new_link_map = {}
            new_option_labels = {}
            new_close_times = {}
            for m in markets:
                t = m.get("ticker") or m.get("market_ticker") or m.get("symbol")
                if not t:
                    continue
                title = _build_market_title(m)
                if title:
                    new_map[t] = title
                new_link_map[t] = _build_market_link(m)
                new_option_labels[t] = _build_option_labels(m)
                ct_str = m.get("close_time") or m.get("expiration_time") or m.get("expected_expiration_time")
                if ct_str:
                    try:
                        ct = datetime.datetime.fromisoformat(ct_str.replace("Z", "+00:00"))
                        new_close_times[t] = ct
                    except (ValueError, TypeError):
                        pass
            
            added = set(new_tickers) - set(state["tickers"])
            removed = set(state["tickers"]) - set(new_tickers)
            
            state["tickers"] = new_tickers
            state["ticker_map"].update(new_map)
            state["close_time_map"].update(new_close_times)
            state["link_map"].update(new_link_map)
            state["option_labels_map"].update(new_option_labels)
            # Also update alert_manager's map references
            state["alert_manager"].ticker_map = state["ticker_map"]
            state["alert_manager"].link_map = state["link_map"]
            state["alert_manager"].option_labels_map = state["option_labels_map"]
            
            logger.info(f"🔄 Market refresh done: {len(new_tickers)} tickers (+{len(added)} new, -{len(removed)} dropped)")
            if added:
                logger.info(f"   New tickers: {list(added)[:10]}")
            
            # If we have a WS reference, resubscribe
            ws = state.get("ws")
            if ws and not ws.closed and (added or removed):
                try:
                    sub = {
                        "id": state.get("msg_id", 999),
                        "cmd": "subscribe",
                        "params": {
                            "channels": ["trade"],
                            "market_tickers": new_tickers,
                        }
                    }
                    await ws.send(json.dumps(sub))
                    logger.info("🔄 Re-subscribed to updated ticker list.")
                except Exception as e:
                    logger.warning(f"Failed to re-subscribe after refresh: {e}")
            
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Market refresh error: {e}")


async def ws_listen_trades(private_key, market_tickers: List[str], store: TradeStore, ticker_map: Dict[str, str], close_time_map: Dict[str, Any] = None, link_map: Dict[str, str] = None, option_labels_map: Dict[str, tuple] = None) -> None:
    baselines = MarketBaselines()
    clusters = MarketClusterTracker(window_seconds=300)
    alerter = Alerter()
    alert_manager = AlertManager(alerter, daily_cap=20, ticker_map=ticker_map, link_map=link_map, option_labels_map=option_labels_map)
    close_time_map = close_time_map or {}
    
    # Shared state for the refresh task
    shared_state = {
        "tickers": list(market_tickers),
        "ticker_map": ticker_map,
        "close_time_map": close_time_map,
        "link_map": link_map or {},
        "option_labels_map": option_labels_map or {},
        "alert_manager": alert_manager,
        "ws": None,
        "msg_id": 1,
    }
    
    # Startup Alert
    start_msg = "✅ Kalshi monitor is live."
    logger.info(start_msg)
    alerter.send(start_msg)

    # Start Heartbeat
    asyncio.create_task(heartbeat(store, alert_manager))
    # Start periodic market refresh (every 2 hours)
    asyncio.create_task(refresh_markets_periodically(private_key, shared_state))
    # Start Polymarket monitor (polls every 30s, shares same alert_manager)
    asyncio.create_task(poly_trade_loop(alert_manager))
    logger.info("Polymarket monitor task started.")

    backoff = 1
    msg_id = 1
    trade_counter = 0

    while True:
        try:
            # Generate FRESH auth headers on every connection attempt
            ws_headers = make_headers(private_key, "GET", WS_SIGN_PATH)
            # websockets <14 uses extra_headers, >=14 uses additional_headers
            ws_ver = tuple(int(x) for x in websockets.__version__.split(".")[:2])
            hdr_kwarg = "additional_headers" if ws_ver >= (14, 0) else "extra_headers"
            connect_kwargs = {
                hdr_kwarg: ws_headers,
                "ping_interval": 20,
                "ping_timeout": 20,
            }
            if ws_ver >= (14, 0):
                connect_kwargs["max_queue"] = 1000
            async with websockets.connect(WS_URL, **connect_kwargs) as ws:
                shared_state["ws"] = ws
                logger.info(f"Connected WS: {WS_URL}")
                current_tickers = shared_state["tickers"]
                logger.info(f"Subscribing to {len(current_tickers)} tickers...")

                sub = {
                    "id": msg_id,
                    "cmd": "subscribe",
                    "params": {
                        "channels": ["trade"],
                        "market_tickers": current_tickers,
                    }
                }
                msg_id += 1
                shared_state["msg_id"] = msg_id
                await ws.send(json.dumps(sub))
                if backoff > 1:
                    logger.info("✅ WS reconnected successfully.")
                    alerter.send("✅ Kalshi monitor reconnected.")
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
                        
                        # Compute hours_to_close from close_time_map
                        hours_to_close = None
                        ct = shared_state["close_time_map"].get(trade.market_ticker)
                        if ct:
                            now_utc = datetime.datetime.now(datetime.timezone.utc)
                            # Hard guard: ignore already-closed markets to avoid stale alerts.
                            if now_utc >= ct:
                                continue
                            delta = ct - now_utc
                            hours_to_close = max(delta.total_seconds() / 3600, 0)
                        
                        score_result = score_trade(
                            volume_proxy, snap, hours_to_close,
                            yes_price_cents=trade.yes_price,
                            contracts=trade.count,
                        )
                        
                        # DEBUG: Process every trade (internally throttled if ALERT_MODE=debug)
                        alert_manager.process_debug_trade(
                            ticker=trade.market_ticker,
                            yes_price=trade.yes_price,
                            contracts=trade.count,
                            volume_proxy=volume_proxy,
                            score=score_result["score"],
                            reasons=score_result["reasons"],
                            ts_str=ts_str
                        )
                        
                        if score_result["score"] >= 50:
                             logger.info(f"⚠️ HIGH SCORE {score_result['score']} | {trade.market_ticker} | {score_result['reasons']}")
                             
                             # 1. Attempt Solo Alert (Requires Production Thresholds)
                             alert_manager.process_solo_alert(
                                 trade.market_ticker,
                                 score_result["score"],
                                 score_result["reasons"],
                                 yes_price=trade.yes_price,
                                 contracts=trade.count
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
            shared_state["ws"] = None
            logger.error(f"WS disconnected/error: {e!r}")
            logger.warning(f"Reconnecting in {backoff}s...")
            # Notify on Telegram if this is a persistent disconnect (backoff >= 16 means multiple failed retries)
            if backoff >= 16:
                alerter.send(f"⚠️ WS struggling to reconnect: {type(e).__name__}. Retrying...")
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

    # Phase 1: Discover non-sports series via events API
    logger.info("Scanning event categories for non-sports series...")
    series = fetch_non_sports_series(private_key)

    # Phase 2: Fetch markets for each non-sports series
    logger.info(f"Fetching markets for {len(series)} non-sports series...")
    try:
        markets = fetch_markets_for_series(private_key, series)
    except Exception as e:
        logger.critical(f"Failed to fetch markets: {e}")
        return

    if not markets:
        logger.warning("No non-sports markets found.")
        return

    markets.sort(key=lambda m: int(m.get("volume") or 0), reverse=True)
    tickers = pick_target_tickers(markets)

    logger.info(f"Found {len(tickers)} non-sports markets.")
    tickers = tickers[:SUBSCRIBE_TICKER_LIMIT]
    logger.info(f"Subscribing to {len(tickers)} tickers.")
    
    # Build title map, link map, option labels and close_time map
    ticker_map = {}         # {ticker: "Clean Title"}
    link_map = {}           # {ticker: "https://kalshi.com/markets/..."}
    option_labels_map = {}  # {ticker: ("yes_label", "no_label")}
    close_time_map = {}     # {ticker: datetime or None}
    for m in markets:
        t = m.get("ticker") or m.get("market_ticker") or m.get("symbol")
        if not t:
            continue
        # Clean title
        title = _build_market_title(m)
        if title:
            ticker_map[t] = title
        # Working URL
        link_map[t] = _build_market_link(m)
        # Option labels (e.g. "Ken Paxton" / "Not Ken Paxton")
        option_labels_map[t] = _build_option_labels(m)
        # Parse close_time / expiration_time from REST
        ct_str = m.get("close_time") or m.get("expiration_time") or m.get("expected_expiration_time")
        if ct_str:
            try:
                ct = datetime.datetime.fromisoformat(ct_str.replace("Z", "+00:00"))
                close_time_map[t] = ct
            except (ValueError, TypeError):
                pass
    logger.info(f"Close times found for {len(close_time_map)} markets.")

    store = TradeStore(db_path="kalshi_trades.db", env=ENV)

    try:
        asyncio.run(ws_listen_trades(private_key, tickers, store, ticker_map, close_time_map, link_map, option_labels_map))
    except KeyboardInterrupt:
        logger.info("Shutdown requested (SIGINT).")
    except Exception as e:
        logger.critical(f"Fatal error in main loop: {e!r}")
    finally:
        logger.info("Closing database...")
        try:
            store.commit()
            store.close()
        except Exception:
            pass
        logger.info("Goodbye.")


if __name__ == "__main__":
    main()
