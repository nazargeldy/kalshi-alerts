"""
Polymarket trade monitor.

Polls data-api.polymarket.com/trades every 30 seconds, scores each trade
with the same engine used for Kalshi, and sends Telegram alerts via the
shared AlertManager. No API key required — all endpoints are public.

Architecture:
  - fetch_poly_markets()     : REST call to gamma-api to discover active markets
  - fetch_recent_trades()    : REST call to data-api for latest trades
  - poly_trade_loop()        : async task — runs forever, meant to be started
                               with asyncio.create_task() from monitor.py
"""

import asyncio
import datetime
import json
import logging
import time
from typing import Any, Dict, List, Optional, Set, Tuple

import requests

from baselines import MarketBaselines
from clustering import MarketClusterTracker
from scoring import score_trade

logger = logging.getLogger("kalshi_monitor")

# ── API endpoints (all public, no auth) ─────────────────────────────────────
GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API  = "https://data-api.polymarket.com"

# ── Tuning ───────────────────────────────────────────────────────────────────
POLL_INTERVAL_SEC   = 30      # seconds between trade polls
MARKET_REFRESH_HOURS = 2.0    # how often to re-fetch the market list
TRADE_FETCH_LIMIT   = 500     # trades to fetch per poll (newest first)
MAX_SEEN_HASHES     = 10_000  # cap on dedup set; older half trimmed when hit
MARKET_PAGE_LIMIT   = 100     # markets per REST page
MARKET_PAGE_CAP     = 50      # max pages to fetch (50 × 100 = 5,000 markets)

# Categories to exclude — substring match on lowercased category string.
# This catches "Sports", "Esports", "Sports/Entertainment", etc.
EXCLUDED_CATEGORY_SUBSTRINGS: tuple = (
    "sport", "esport", "entertainment", "awards", "celebrity", "reality",
)

# Title-level keyword blocklist — any match = skip.
# Covers every sport and format we don't want.
EXCLUDED_QUESTION_KEYWORDS: tuple = (
    # ── Team sports — all major leagues ────────────────────────────────────
    # NFL
    " nfl ", "nfl ", " nfl", "nfl:", "super bowl", "touchdown", "quarterback",
    "yards passing", "yards rushing", "field goal",
    # NBA
    " nba ", "nba ", " nba", "nba:", "nba finals", "nba playoffs",
    "rebounds", "assists", "three-pointer", "slam dunk",
    # MLB
    " mlb ", "mlb ", " mlb", "mlb:", "world series", "home run",
    "strikeout", "innings", "pitcher", "batting average",
    # NHL
    " nhl ", "nhl ", " nhl", "nhl:", "stanley cup", "hat trick", "power play",
    # MLS / Soccer (all)
    " mls ", "mls ", "mls:", "premier league", "la liga", "serie a",
    "bundesliga", "ligue 1", "champions league", "europa league",
    "copa america", "fifa", "world cup", "euro 2", "concacaf",
    "o/u ", "over/under",   # match totals — almost always sports
    # College sports
    "ncaa", "march madness", "college football", "cfp", "bowl game",
    # Combat sports
    "ufc", "boxing match", " mma ", "fight night", "knockout",
    # Golf
    "pga tour", "lpga", "masters tournament", "ryder cup", "open championship",
    # Tennis (all tours)
    " atp ", " wta ", "wimbledon", "us open tennis", "french open",
    "australian open", "roland garros", "davis cup",
    "vs ", # most head-to-head match markets  ← catches "Kessler vs Starodubtseva"

    # ── Match / game result patterns ────────────────────────────────────────
    "win the match", "win the game", "win the series",
    "cover the spread", "spread:", "moneyline",
    ": o/u ", "- o/u ",          # totals markets
    "game 1 winner", "game 2 winner", "game 3 winner",
    "set 1", "set 2", "set 3",   # tennis sets

    # ── Specific leagues / tournaments by name ──────────────────────────────
    "copa colsanitas", "copa sudamericana", "copa libertadores",
    "champions cup", "carabao cup", "fa cup", "efl ",
    "spring league", "summer league",
    "esc challenger", "esl challenger",   # esports
    "lol:", "league of legends",
    "dota 2", "valorant match", "cs2:", " cs2 ",
    "rocket league",
    "credit one charleston", "miami open", "indian wells",
    "san luis potosi", "buenos aires open",

    # ── Team name patterns (catches game markets not caught by league names) ─
    # NBA teams
    "bulls vs", "knicks vs", "lakers vs", "celtics vs", "warriors vs",
    "nets vs", "hawks vs", "pacers vs", "raptors vs", "grizzlies vs",
    "jazz vs", "rockets vs", "clippers vs", "nuggets vs", "suns vs",
    "heat vs", "bucks vs", "76ers vs", "pistons vs", "timberwolves vs",
    "vs. bulls", "vs. knicks", "vs. lakers", "vs. celtics", "vs. warriors",
    "vs. nets", "vs. hawks", "vs. pacers", "vs. raptors", "vs. grizzlies",
    "vs. jazz", "vs. rockets", "vs. clippers", "vs. nuggets", "vs. suns",
    "vs. heat", "vs. bucks", "vs. 76ers", "vs. pistons", "vs. timberwolves",
    # NHL teams
    "flyers vs", "islanders vs", "rangers vs", "bruins vs", "maple leafs vs",
    "blackhawks vs", "red wings vs", "penguins vs", "capitals vs",
    # MLB teams
    "orioles vs", "pirates vs", "cubs vs", "guardians vs", "reds vs",
    "rangers vs", "yankees vs", "red sox vs", "dodgers vs", "mets vs",
    # Soccer clubs
    "san lorenzo", "estudiantes", "racing club", "independiente",
    "river plate", "boca juniors", "flamengo", "palmeiras",

    # ── Weather / temperature micro-markets ─────────────────────────────────
    "temperature", "highest temp", "°c", "°f", " celsius", " fahrenheit",
    "rainfall", "precipitation", "humidity", "max temp", "min temp",
    "hottest", "coldest",

    # ── Micro-window crypto up/down ──────────────────────────────────────────
    "up or down -",
    "15-minute", "30-minute",
)


# ── Price helpers ─────────────────────────────────────────────────────────────

def _to_cents(price_val: Any) -> int:
    """Convert Polymarket price (0–1 float or string) to integer cents (0–100)."""
    try:
        return int(round(float(price_val) * 100))
    except (TypeError, ValueError):
        return 0


def _parse_json_field(val: Any, fallback: Any) -> Any:
    """Polymarket sometimes sends list/dict fields as JSON strings."""
    if isinstance(val, str):
        try:
            return json.loads(val)
        except (json.JSONDecodeError, TypeError):
            return fallback
    return val if val is not None else fallback


def _yes_price_from_trade(trade: Dict[str, Any]) -> int:
    """
    Normalize the trade price to outcome-0 price in cents.

    Polymarket outcome tokens: index 0 = "Yes" / team A, index 1 = "No" / team B.
    `price` is the execution price of *whichever* outcome was traded.
    We always return the price of outcome 0 so the scoring pipeline is consistent.
    Using outcomeIndex is more reliable than checking the label text, since
    outcomes can be "Oilers", "Timberwolves", etc. instead of "Yes"/"No".
    """
    try:
        price = float(trade.get("price", 0.5) or 0.5)
    except (TypeError, ValueError):
        price = 0.5
    outcome_index = int(trade.get("outcomeIndex", 0) or 0)
    if outcome_index == 1:
        return int(round((1.0 - price) * 100))
    return int(round(price * 100))


# ── Market discovery ──────────────────────────────────────────────────────────

def fetch_poly_markets() -> List[Dict[str, Any]]:
    """
    Fetch all active non-sports Polymarket markets, sorted by 24h volume.
    Returns raw market dicts from the Gamma API.
    """
    all_markets: List[Dict[str, Any]] = []
    offset = 0

    for page in range(MARKET_PAGE_CAP):
        try:
            r = requests.get(
                f"{GAMMA_API}/markets",
                params={
                    "active":    "true",
                    "closed":    "false",
                    "limit":     MARKET_PAGE_LIMIT,
                    "offset":    offset,
                    "order":     "volume24hr",
                    "ascending": "false",
                },
                timeout=20,
            )
            r.raise_for_status()
            batch: List[Dict[str, Any]] = r.json()
        except Exception as e:
            logger.warning(f"Polymarket market fetch error (page={page}): {e}")
            break

        if not batch:
            break

        for m in batch:
            cat = (m.get("category") or "").lower()
            if any(ex in cat for ex in EXCLUDED_CATEGORY_SUBSTRINGS):
                continue
            # Binary markets only (2 outcomes)
            outcomes = _parse_json_field(m.get("outcomes"), [])
            if len(outcomes) != 2:
                continue
            # Question-level keyword filter
            question = (m.get("question") or m.get("title") or "").lower()
            if any(kw in question for kw in EXCLUDED_QUESTION_KEYWORDS):
                continue
            all_markets.append(m)

        if len(batch) < MARKET_PAGE_LIMIT:
            break
        offset += MARKET_PAGE_LIMIT

    logger.info(f"Polymarket: {len(all_markets)} active binary non-sports markets found.")
    return all_markets


def build_poly_maps(
    markets: List[Dict[str, Any]],
) -> Tuple[Dict, Dict, Dict, Dict, Dict]:
    """
    Build lookup maps keyed by conditionId.

    Returns:
        ticker_map       {cid: title}
        link_map         {cid: url}
        option_labels    {cid: ("✅ Yes label", "❌ No label")}
        close_time_map   {cid: datetime (UTC, tz-aware)}
        cid_to_slug      {cid: event_slug}  — for clustering
    """
    ticker_map:     Dict[str, str]                   = {}
    link_map:       Dict[str, str]                   = {}
    option_labels:  Dict[str, Tuple[str, str]]       = {}
    close_time_map: Dict[str, datetime.datetime]     = {}
    cid_to_slug:    Dict[str, str]                   = {}

    for m in markets:
        cid = m.get("conditionId") or m.get("id") or ""
        if not cid:
            continue
        cid = str(cid)

        title    = m.get("question") or m.get("title") or cid[:20]
        slug     = m.get("slug") or ""
        evt_slug = m.get("groupItemTitle") or slug  # best event-level slug available

        url = f"https://polymarket.com/event/{slug}" if slug else "https://polymarket.com"

        outcomes = _parse_json_field(m.get("outcomes"), ["Yes", "No"])
        prices   = _parse_json_field(m.get("outcomePrices"), ["0.5", "0.5"])

        yes_label = str(outcomes[0]) if len(outcomes) > 0 else "Yes"
        no_label  = str(outcomes[1]) if len(outcomes) > 1 else "No"

        ticker_map[cid]    = title
        link_map[cid]      = url
        option_labels[cid] = (f"✅ {yes_label}", f"❌ {no_label}")
        cid_to_slug[cid]   = evt_slug or cid[:12]

        end_date = (
            m.get("endDate")
            or m.get("end_date_iso")
            or m.get("endDateIso")
        )
        if end_date:
            try:
                ct = datetime.datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                close_time_map[cid] = ct
            except (ValueError, TypeError):
                pass

    return ticker_map, link_map, option_labels, close_time_map, cid_to_slug


# ── Trade polling ─────────────────────────────────────────────────────────────

def fetch_recent_trades(limit: int = TRADE_FETCH_LIMIT) -> List[Dict[str, Any]]:
    """
    Fetch the most recent Polymarket trades (newest first).
    No auth required.
    """
    try:
        r = requests.get(
            f"{DATA_API}/trades",
            params={"limit": limit},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.warning(f"Polymarket trades poll error: {e}")
        return []


# ── Main async loop ───────────────────────────────────────────────────────────

async def poly_trade_loop(alert_manager) -> None:
    """
    Async task that polls Polymarket trades and fires alerts.
    Meant to be launched with asyncio.create_task() from monitor.py.

    The alert_manager is the *same* AlertManager used for Kalshi so alerts
    from both exchanges share the same daily cap and cooldowns.
    """
    logger.info("Polymarket monitor starting — fetching markets...")

    # ── Initial market discovery ──────────────────────────────────────────
    loop = asyncio.get_event_loop()
    try:
        markets = await loop.run_in_executor(None, fetch_poly_markets)
    except Exception as e:
        logger.error(f"Polymarket initial market fetch failed: {e}")
        markets = []

    ticker_map, link_map, option_labels, close_time_map, cid_to_slug = build_poly_maps(markets)

    # Inject into the shared alert_manager (keyed by conditionId)
    alert_manager.ticker_map.update(ticker_map)
    alert_manager.link_map.update(link_map)
    alert_manager.option_labels_map.update(option_labels)

    logger.info(f"Polymarket monitor live — watching {len(ticker_map)} markets.")

    # ── Per-loop state ────────────────────────────────────────────────────
    baselines              = MarketBaselines()
    clusters               = MarketClusterTracker(window_seconds=300)
    seen_hashes: Set[str]  = set()
    last_market_refresh    = time.time()
    poll_count             = 0

    while True:
        await asyncio.sleep(POLL_INTERVAL_SEC)

        # ── Periodic market refresh ───────────────────────────────────────
        if time.time() - last_market_refresh > MARKET_REFRESH_HOURS * 3600:
            try:
                markets = await loop.run_in_executor(None, fetch_poly_markets)
                ticker_map, link_map, option_labels, close_time_map, cid_to_slug = build_poly_maps(markets)
                alert_manager.ticker_map.update(ticker_map)
                alert_manager.link_map.update(link_map)
                alert_manager.option_labels_map.update(option_labels)
                last_market_refresh = time.time()
                logger.info(f"Polymarket market maps refreshed ({len(ticker_map)} markets).")
            except Exception as e:
                logger.warning(f"Polymarket market refresh error: {e}")

        # ── Poll trades ───────────────────────────────────────────────────
        try:
            raw_trades = await loop.run_in_executor(None, fetch_recent_trades)
        except Exception as e:
            logger.warning(f"Polymarket poll #{poll_count} error: {e}")
            continue

        poll_count += 1
        new_count = 0

        for trade in raw_trades:
            tx_hash = trade.get("transactionHash", "")
            if tx_hash and tx_hash in seen_hashes:
                continue
            if tx_hash:
                seen_hashes.add(tx_hash)

            cid = trade.get("conditionId", "")
            if not cid or cid not in ticker_map:
                continue  # market not in our watched set

            yes_price_cents = _yes_price_from_trade(trade)
            try:
                contracts = int(float(trade.get("size", 0) or 0))
            except (TypeError, ValueError):
                contracts = 0

            if contracts <= 0 or yes_price_cents <= 0 or yes_price_cents >= 100:
                continue

            new_count += 1
            now_ms = int(time.time() * 1000)
            ts_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # Update baselines
            baselines.update(
                market_ticker=cid,
                ts_received_ms=now_ms,
                contracts=contracts,
                yes_price_cents=yes_price_cents,
            )

            snap         = baselines.snapshot(cid)
            volume_proxy = contracts * yes_price_cents

            # Hours to close
            hours_to_close: Optional[float] = None
            ct = close_time_map.get(cid)
            if ct:
                now_utc = datetime.datetime.now(datetime.timezone.utc)
                if now_utc >= ct:
                    continue  # market already closed
                hours_to_close = max((ct - now_utc).total_seconds() / 3600, 0)

            # Score
            score_result = score_trade(
                volume_proxy,
                snap,
                hours_to_close,
                yes_price_cents=yes_price_cents,
                contracts=contracts,
            )

            # Debug mode alert (low threshold, aggregated)
            alert_manager.process_debug_trade(
                ticker=cid,
                yes_price=yes_price_cents,
                contracts=contracts,
                volume_proxy=volume_proxy,
                score=score_result["score"],
                reasons=score_result["reasons"],
                ts_str=f"{ts_str} [Polymarket]",
            )

            # Production alert
            if score_result["score"] >= 50:
                logger.info(
                    f"POLY HIGH SCORE {score_result['score']} | "
                    f"{ticker_map.get(cid, cid[:20])} | {score_result['reasons']}"
                )

                alert_manager.process_solo_alert(
                    cid,
                    score_result["score"],
                    score_result["reasons"],
                    yes_price=yes_price_cents,
                    contracts=contracts,
                )

                # Cluster by event slug (groups related Polymarket markets)
                event_slug = cid_to_slug.get(cid, cid[:12])
                cluster_info = clusters.add_event(event_slug, cid, score_result["score"])
                if cluster_info["count"] >= 2:
                    logger.info(
                        f"POLY CLUSTER {event_slug} count={cluster_info['count']} "
                        f"max={cluster_info['max_score']}"
                    )
                    alert_manager.process_cluster_alert(
                        f"POLY:{event_slug}",
                        cluster_info["count"],
                        cluster_info["max_score"],
                        cluster_info["markets"],
                    )

        if new_count > 0:
            logger.info(f"Polymarket poll #{poll_count}: {new_count} new trades processed.")

        # Trim dedup set to prevent unbounded growth
        if len(seen_hashes) > MAX_SEEN_HASHES:
            hashes_list = list(seen_hashes)
            seen_hashes = set(hashes_list[len(hashes_list) // 2:])
            logger.debug(f"Polymarket: trimmed seen_hashes to {len(seen_hashes)}")

        if poll_count % 60 == 0:
            logger.info(
                f"Polymarket heartbeat | polls={poll_count} | "
                f"seen_hashes={len(seen_hashes)} | markets={len(ticker_map)}"
            )
