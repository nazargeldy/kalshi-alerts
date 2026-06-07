"""
Auto-resolve Notion P&L entries by checking Polymarket market outcomes.

Run manually or via cron (e.g. every hour):
  python auto_resolve.py

Reads all PENDING Notion rows, calls Polymarket gamma-api to check if the
underlying market has resolved, then writes WIN/LOSS/SKIP to the Result field.

Also writes /tmp/accuracy_stats.json with rolling 30-day hit rates by
market category, which ai_reasoning.py reads to sharpen its SYSTEM_PROMPT.

Env vars required: NOTION_TOKEN, NOTION_DB_ID (optional, falls back to search)
"""

import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

import requests

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("auto_resolve")

NOTION_API     = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
NOTION_TOKEN   = os.getenv("NOTION_TOKEN", "").strip()
NOTION_DB_ID   = os.getenv("NOTION_DB_ID", "33e8b842-ae95-81d3-8a6e-eed814ab9f81").strip()

GAMMA_API      = "https://gamma-api.polymarket.com"
ACCURACY_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "accuracy_stats.json")
WINDOW_DAYS    = 30
REQUEST_DELAY  = 0.4   # seconds between Polymarket API calls (stay polite)

# ── Notion helpers ────────────────────────────────────────────────────────────

def _notion_headers() -> dict:
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def fetch_pending_rows() -> List[dict]:
    """Return all Notion rows whose Result is still empty (unresolved)."""
    rows, cursor = [], None
    while True:
        body: dict = {
            "filter": {"property": "Result", "select": {"is_empty": True}},
            "page_size": 100,
        }
        if cursor:
            body["start_cursor"] = cursor
        r = requests.post(
            f"{NOTION_API}/databases/{NOTION_DB_ID}/query",
            headers=_notion_headers(),
            json=body,
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        rows.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return rows


def fetch_all_resolved_rows() -> List[dict]:
    """Return all rows that already have a WIN or LOSS result (for accuracy stats)."""
    rows, cursor = [], None
    while True:
        body: dict = {
            "filter": {
                "or": [
                    {"property": "Result", "select": {"equals": "WIN"}},
                    {"property": "Result", "select": {"equals": "LOSS"}},
                ]
            },
            "page_size": 100,
        }
        if cursor:
            body["start_cursor"] = cursor
        r = requests.post(
            f"{NOTION_API}/databases/{NOTION_DB_ID}/query",
            headers=_notion_headers(),
            json=body,
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        rows.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return rows


def update_notion_result(page_id: str, result: str) -> None:
    requests.patch(
        f"{NOTION_API}/pages/{page_id}",
        headers=_notion_headers(),
        json={"properties": {"Result": {"select": {"name": result}}}},
        timeout=10,
    ).raise_for_status()


# ── Polymarket helpers ────────────────────────────────────────────────────────

_SLUG_RE = re.compile(r"polymarket\.com/event/([^/?#]+)")

def _slug_from_url(url: str) -> Optional[str]:
    m = _SLUG_RE.search(url or "")
    return m.group(1) if m else None


def check_market_resolution(slug: str) -> Optional[Tuple[bool, float]]:
    """Query gamma-api for the event. Returns (yes_won: bool, yes_price: float)
    if the market has resolved, or None if still open or API error.
    yes_price is the final settlement price of the YES outcome (1.0 = YES won, 0.0 = NO won)."""
    try:
        r = requests.get(
            f"{GAMMA_API}/events",
            params={"slug": slug, "limit": 1},
            timeout=8,
        )
        r.raise_for_status()
        events = r.json()
        if not events:
            return None
        event = events[0] if isinstance(events, list) else events
        markets = event.get("markets", [])
        if not markets:
            return None
        # Use first market in the event
        mkt = markets[0]
        if not mkt.get("closed") and not mkt.get("resolved"):
            return None
        prices_raw = mkt.get("outcomePrices", "[]")
        if isinstance(prices_raw, str):
            prices = json.loads(prices_raw)
        else:
            prices = prices_raw
        if not prices:
            return None
        yes_price = float(prices[0])
        return (yes_price >= 0.99, yes_price)
    except Exception as e:
        logger.debug(f"Polymarket API error for {slug}: {e}")
        return None


# ── Row parsing helpers ───────────────────────────────────────────────────────

def _prop_text(props: dict, key: str) -> str:
    p = props.get(key, {})
    # title type
    items = p.get("title") or p.get("rich_text") or []
    return "".join(i.get("plain_text", "") for i in items).strip()


def _prop_select(props: dict, key: str) -> str:
    sel = props.get(key, {}).get("select") or {}
    return sel.get("name", "")


def _prop_url(props: dict, key: str) -> str:
    return props.get(key, {}).get("url") or ""


def _prop_date(props: dict, key: str) -> Optional[datetime]:
    d = props.get(key, {}).get("date") or {}
    s = d.get("start")
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _side_is_yes(side: str) -> Optional[bool]:
    """Map the 'Side to Buy' field to a yes/no bool.
    Returns None if we can't determine (e.g. sports team name)."""
    s = side.lower()
    if any(k in s for k in ("yes", "up", "high", "above", "over")):
        return True
    if any(k in s for k in ("no", "down", "low", "below", "under")):
        return False
    return None  # team name or unclear — can't auto-resolve


# ── Market category tagger (for accuracy stats) ───────────────────────────────

def _market_category(title: str) -> str:
    t = title.lower()
    if "up or down" in t:
        return "crypto_direction"
    if any(k in t for k in ("bitcoin", "ethereum", "solana", "btc", "eth", "sol",
                              "xrp", "crude oil", "wti", "gold", "silver")):
        return "crypto_level"
    if any(k in t for k in ("iran", "russia", "ukraine", "israel", "china",
                              "hormuz", "hezbollah", "nato", "military", "war",
                              "ceasefire", "airspace", "kharg")):
        return "geopolitical"
    if any(k in t for k in ("fed ", "interest rate", "gdp", "inflation", "cpi",
                              "rate cut", "rate hike", "fomc", "monetary")):
        return "macro"
    if any(k in t for k in ("president", "prime minister", "election", "out by",
                              "resign", "impeach", "senate", "congress", "governor")):
        return "political"
    return "other"


# ── Accuracy stats ────────────────────────────────────────────────────────────

def compute_and_save_accuracy(resolved_rows: List[dict]) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)
    cats: Dict[str, Dict[str, int]] = {}

    for row in resolved_rows:
        props = row.get("properties", {})
        ts = _prop_date(props, "Timestamp")
        if ts and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts and ts < cutoff:
            continue  # outside window
        result = _prop_select(props, "Result")
        if result not in ("WIN", "LOSS"):
            continue
        title = _prop_text(props, "Market")
        cat = _market_category(title)
        if cat not in cats:
            cats[cat] = {"wins": 0, "losses": 0}
        if result == "WIN":
            cats[cat]["wins"] += 1
        else:
            cats[cat]["losses"] += 1

    totals = {"wins": 0, "losses": 0}
    stats: dict = {}
    for cat, c in cats.items():
        total = c["wins"] + c["losses"]
        rate = round(c["wins"] / total, 3) if total else 0.0
        stats[cat] = {"wins": c["wins"], "losses": c["losses"], "total": total, "rate": rate}
        totals["wins"]   += c["wins"]
        totals["losses"] += c["losses"]

    grand_total = totals["wins"] + totals["losses"]
    overall_rate = round(totals["wins"] / grand_total, 3) if grand_total else 0.0

    output = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "window_days": WINDOW_DAYS,
        "categories": stats,
        "overall": {**totals, "total": grand_total, "rate": overall_rate},
    }
    try:
        with open(ACCURACY_FILE, "w") as f:
            json.dump(output, f, indent=2)
        logger.info(
            f"Accuracy stats saved → {ACCURACY_FILE} | "
            f"overall {totals['wins']}W/{totals['losses']}L ({overall_rate:.0%})"
        )
        for cat, s in stats.items():
            logger.info(f"  {cat}: {s['wins']}W/{s['losses']}L ({s['rate']:.0%})")
    except Exception as e:
        logger.warning(f"Could not write accuracy file: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────

def run() -> None:
    if not NOTION_TOKEN:
        logger.error("NOTION_TOKEN not set — aborting.")
        return

    logger.info("Fetching PENDING Notion rows…")
    pending = fetch_pending_rows()
    logger.info(f"Found {len(pending)} PENDING rows.")

    wins = losses = skips = errors = 0

    for row in pending:
        page_id = row["id"]
        props   = row.get("properties", {})
        title   = _prop_text(props, "Market")
        side    = _prop_side = _prop_text(props, "Side to Buy")
        link    = _prop_url(props, "Link")

        slug = _slug_from_url(link)
        if not slug:
            logger.debug(f"No slug for: {title[:60]}")
            continue

        resolution = check_market_resolution(slug)
        time.sleep(REQUEST_DELAY)

        if resolution is None:
            continue  # market still open or API error — leave PENDING

        yes_won, yes_price = resolution

        # Determine which side Notion recommended
        recommended_yes = _side_is_yes(side)
        if recommended_yes is None:
            # Sports team name or ambiguous — mark SKIP
            try:
                update_notion_result(page_id, "SKIP")
                skips += 1
            except Exception as e:
                logger.warning(f"Notion update failed for {title[:40]}: {e}")
                errors += 1
            continue

        # Compare recommendation to outcome
        if recommended_yes == yes_won:
            result = "WIN"
            wins += 1
        else:
            result = "LOSS"
            losses += 1

        try:
            update_notion_result(page_id, result)
            logger.info(f"{result} | {title[:60]} | side={side} yes_won={yes_won}")
        except Exception as e:
            logger.warning(f"Notion update failed for {title[:40]}: {e}")
            errors += 1

    logger.info(
        f"Resolve run complete: {wins} WIN, {losses} LOSS, {skips} SKIP, {errors} errors "
        f"out of {len(pending)} pending rows."
    )

    # Recompute accuracy stats from all resolved rows
    logger.info("Computing rolling accuracy stats…")
    try:
        all_resolved = fetch_all_resolved_rows()
        compute_and_save_accuracy(all_resolved)
    except Exception as e:
        logger.warning(f"Accuracy stats failed: {e}")


if __name__ == "__main__":
    run()
