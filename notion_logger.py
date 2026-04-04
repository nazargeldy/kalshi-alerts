"""
Notion P&L tracker for Kalshi/Polymarket alerts.

Creates one row per alert in a Notion database.
The database is auto-created inside the parent page on first run.

Env vars:
  NOTION_TOKEN      — Internal Integration Secret (ntn_...)
  NOTION_PAGE_ID    — ID of the Notion page the integration has access to
"""

import logging
import os
import re
import time
from typing import Optional

import requests

logger = logging.getLogger("kalshi_monitor")

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
NOTION_TOKEN = os.getenv("NOTION_TOKEN", "").strip()
NOTION_PAGE_ID = os.getenv("NOTION_PAGE_ID", "").strip()

# Cached database ID so we don't search on every alert
_db_id: Optional[str] = None
DB_TITLE = "Kalshi & Polymarket Alert Tracker"


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _find_or_create_db() -> Optional[str]:
    """Find existing tracker DB under the parent page, or create it."""
    global _db_id
    if _db_id:
        return _db_id

    if not NOTION_TOKEN or not NOTION_PAGE_ID:
        return None

    # Search for existing DB with our title
    try:
        r = requests.post(
            f"{NOTION_API}/search",
            headers=_headers(),
            json={"query": DB_TITLE, "filter": {"value": "database", "property": "object"}},
            timeout=10,
        )
        r.raise_for_status()
        results = r.json().get("results", [])
        for db in results:
            if db.get("title", [{}])[0].get("plain_text", "") == DB_TITLE:
                _db_id = db["id"]
                logger.info(f"Notion: found existing DB {_db_id}")
                return _db_id
    except Exception as e:
        logger.warning(f"Notion search failed: {e}")

    # Create new database
    try:
        r = requests.post(
            f"{NOTION_API}/databases",
            headers=_headers(),
            json={
                "parent": {"type": "page_id", "page_id": NOTION_PAGE_ID},
                "title": [{"type": "text", "text": {"content": DB_TITLE}}],
                "icon": {"type": "emoji", "emoji": "📊"},
                "properties": {
                    # Title column (required by Notion)
                    "Market": {"title": {}},
                    "Timestamp":        {"date": {}},
                    "Source":           {"select": {"options": [
                        {"name": "Kalshi",     "color": "blue"},
                        {"name": "Polymarket", "color": "purple"},
                    ]}},
                    "Alert Type":       {"select": {"options": [
                        {"name": "SOLO",    "color": "red"},
                        {"name": "CLUSTER", "color": "orange"},
                        {"name": "DEBUG",   "color": "gray"},
                    ]}},
                    "Side to Buy":      {"rich_text": {}},
                    "Entry Price (¢)":  {"number": {"format": "number"}},
                    "Contracts":        {"number": {"format": "number"}},
                    "Score /100":       {"number": {"format": "number"}},
                    "AI Lean":          {"select": {"options": [
                        {"name": "Leaning YES", "color": "green"},
                        {"name": "Leaning NO",  "color": "red"},
                        {"name": "Leaning UP",  "color": "green"},
                        {"name": "Leaning DOWN","color": "red"},
                    ]}},
                    "Reasons":          {"rich_text": {}},
                    "Link":             {"url": {}},
                    # Columns you fill in after resolution
                    "Result":           {"select": {"options": [
                        {"name": "WIN",  "color": "green"},
                        {"name": "LOSS", "color": "red"},
                        {"name": "PUSH", "color": "yellow"},
                        {"name": "SKIP", "color": "gray"},
                    ]}},
                    "Exit Price (¢)":   {"number": {"format": "number"}},
                    "P&L per contract": {"number": {"format": "number"}},
                    "Notes":            {"rich_text": {}},
                },
            },
            timeout=15,
        )
        r.raise_for_status()
        _db_id = r.json()["id"]
        logger.info(f"Notion: created DB {_db_id}")
        return _db_id
    except Exception as e:
        logger.warning(f"Notion DB create failed: {e}")
        return None


def _extract_ai_lean(analysis: str) -> Optional[str]:
    if not analysis:
        return None
    m = re.search(r"[Ll]eaning\s+(YES|NO|Up|Down|yes|no)", analysis)
    if m:
        return f"Leaning {m.group(1).upper()}"
    lower = analysis.lower()
    if "leaning yes" in lower:
        return "Leaning YES"
    if "leaning no" in lower:
        return "Leaning NO"
    return None


def log_alert(
    ticker: str,
    title: str,
    source: str,
    alert_type: str,
    side: str,
    entry_price: int,
    contracts: int,
    score: float,
    reasons: list,
    link: str,
    analysis: str = "",
    timestamp_str: str = "",
) -> bool:
    """
    Write one row to the Notion tracker database.
    Returns True on success, False on failure.
    """
    if not NOTION_TOKEN:
        return False

    db_id = _find_or_create_db()
    if not db_id:
        return False

    # Build ISO timestamp
    if not timestamp_str:
        timestamp_str = time.strftime("%Y-%m-%dT%H:%M:%S")
    else:
        # Convert "2026-04-03 22:18:43" → "2026-04-03T22:18:43"
        timestamp_str = timestamp_str.replace(" ", "T")
        # Strip " [Polymarket]" suffix if present
        timestamp_str = timestamp_str.split(" [")[0]

    lean = _extract_ai_lean(analysis)
    reasons_text = " | ".join(reasons)

    properties: dict = {
        "Market":           {"title": [{"text": {"content": title[:200]}}]},
        "Timestamp":        {"date": {"start": timestamp_str}},
        "Source":           {"select": {"name": source}},
        "Alert Type":       {"select": {"name": alert_type}},
        "Side to Buy":      {"rich_text": [{"text": {"content": side[:100]}}]},
        "Entry Price (¢)":  {"number": entry_price},
        "Contracts":        {"number": contracts},
        "Score /100":       {"number": round(score, 1)},
        "Reasons":          {"rich_text": [{"text": {"content": reasons_text[:2000]}}]},
        "Link":             {"url": link or None},
    }

    if lean:
        properties["AI Lean"] = {"select": {"name": lean}}

    try:
        r = requests.post(
            f"{NOTION_API}/pages",
            headers=_headers(),
            json={"parent": {"database_id": db_id}, "properties": properties},
            timeout=10,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        logger.warning(f"Notion log_alert failed: {e} — {getattr(e, 'response', None) and e.response.text[:200]}")
        return False
