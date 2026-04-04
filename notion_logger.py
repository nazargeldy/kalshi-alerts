"""
Notion P&L tracker for Kalshi/Polymarket alerts.

Columns: Market, Timestamp, Source, Side to Buy, Score, AI Lean, Reasons, Link, Result

Env vars:
  NOTION_TOKEN   — Internal Integration Secret (ntn_...)
  NOTION_PAGE_ID — ID of the Notion page the integration has access to
"""

import logging
import os
import re
import time
from typing import Optional

import requests

logger = logging.getLogger("kalshi_monitor")

NOTION_API     = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
NOTION_TOKEN   = os.getenv("NOTION_TOKEN", "").strip()
NOTION_PAGE_ID = os.getenv("NOTION_PAGE_ID", "").strip()

_db_id: Optional[str] = None
DB_TITLE = "Kalshi & Polymarket Alert Tracker"


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _find_or_create_db() -> Optional[str]:
    global _db_id
    if _db_id:
        return _db_id

    if not NOTION_TOKEN or not NOTION_PAGE_ID:
        return None

    # Search for existing DB
    try:
        r = requests.post(
            f"{NOTION_API}/search",
            headers=_headers(),
            json={"query": DB_TITLE, "filter": {"value": "database", "property": "object"}},
            timeout=10,
        )
        r.raise_for_status()
        for db in r.json().get("results", []):
            title_parts = db.get("title", [])
            if title_parts and title_parts[0].get("plain_text", "") == DB_TITLE:
                _db_id = db["id"]
                logger.info(f"Notion: found existing DB {_db_id}")
                return _db_id
    except Exception as e:
        logger.warning(f"Notion search failed: {e}")

    # Create DB with simplified schema
    try:
        r = requests.post(
            f"{NOTION_API}/databases",
            headers=_headers(),
            json={
                "parent": {"type": "page_id", "page_id": NOTION_PAGE_ID},
                "title": [{"type": "text", "text": {"content": DB_TITLE}}],
                "icon": {"type": "emoji", "emoji": "📊"},
                "properties": {
                    "Market":      {"title": {}},
                    "Timestamp":   {"date": {}},
                    "Source":      {"select": {"options": [
                        {"name": "Kalshi",     "color": "blue"},
                        {"name": "Polymarket", "color": "purple"},
                    ]}},
                    "Side to Buy": {"rich_text": {}},
                    "Score /100":  {"number": {"format": "number"}},
                    "AI Lean":     {"select": {"options": [
                        {"name": "Leaning YES",  "color": "green"},
                        {"name": "Leaning NO",   "color": "red"},
                        {"name": "Leaning UP",   "color": "green"},
                        {"name": "Leaning DOWN", "color": "red"},
                    ]}},
                    "Reasons":     {"rich_text": {}},
                    "Link":        {"url": {}},
                    "Result":      {"select": {"options": [
                        {"name": "WIN",  "color": "green"},
                        {"name": "LOSS", "color": "red"},
                        {"name": "PUSH", "color": "yellow"},
                        {"name": "SKIP", "color": "gray"},
                    ]}},
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
    alert_type: str,  # kept for signature compat, not written to Notion
    side: str,
    entry_price: int,  # kept for compat
    contracts: int,    # kept for compat
    score: float,
    reasons: list,
    link: str,
    analysis: str = "",
    timestamp_str: str = "",
) -> bool:
    if not NOTION_TOKEN:
        return False

    db_id = _find_or_create_db()
    if not db_id:
        return False

    if not timestamp_str:
        timestamp_str = time.strftime("%Y-%m-%dT%H:%M:%S")
    else:
        timestamp_str = timestamp_str.replace(" ", "T").split(" [")[0]

    lean = _extract_ai_lean(analysis)

    properties: dict = {
        "Market":      {"title": [{"text": {"content": title[:200]}}]},
        "Timestamp":   {"date": {"start": timestamp_str}},
        "Source":      {"select": {"name": source}},
        "Side to Buy": {"rich_text": [{"text": {"content": side[:100]}}]},
        "Score /100":  {"number": round(score, 1)},
        "Reasons":     {"rich_text": [{"text": {"content": " | ".join(reasons)[:2000]}}]},
        "Link":        {"url": link or None},
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
        logger.warning(f"Notion log_alert failed: {e}")
        return False
