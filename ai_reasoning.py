"""
Groq-powered AI reasoning for Kalshi trade alerts.
Adds a brief analyst interpretation to unusual activity alerts.
"""

import logging
import os
import time
from typing import Optional

import requests

logger = logging.getLogger("kalshi_monitor")

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_TIMEOUT = int(os.getenv("GROQ_TIMEOUT", "8"))

# Simple cache: {ticker: (timestamp, analysis)}
_cache = {}
CACHE_TTL = 1800  # 30 minutes — don't re-analyze the same market


SYSTEM_PROMPT = (
    "You are a sharp prediction-market analyst advising a trader. "
    "A monitoring bot detected unusual trading activity on a Kalshi prediction market. "
    "Your job:\n"
    "1. Explain the bet in one plain-English sentence (what exactly is being predicted?)\n"
    "2. Why this surge of trades might matter — what could traders know, "
    "what recent news or events could be driving this?\n"
    "3. A quick actionable take — is this a signal worth acting on or just noise?\n"
    "4. Explicitly state which direction this 'insider/anomaly' alert is leaning (e.g., 'Leaning YES' or 'Leaning NO'), based on the price and reasons provided.\n\n"
    "Keep it to 2-4 sentences. No fluff, no disclaimers. "
    "Write like a trading desk analyst briefing a colleague."
)


def analyze_trade(
    title: str,
    yes_label: str,
    no_label: str,
    yes_price: int,
    contracts: int,
    score: float,
    reasons: list,
    num_trades: int = 1,
) -> Optional[str]:
    """Call Groq LLM to get a brief analysis of the unusual trade.
    Returns the analysis string, or None if unavailable."""
    if not GROQ_API_KEY:
        return None

    # Check cache
    cache_key = title
    now = time.time()
    cached = _cache.get(cache_key)
    if cached and (now - cached[0]) < CACHE_TTL:
        return cached[1]

    no_price = 100 - yes_price
    reason_text = "; ".join(reasons) if reasons else "Multiple signals"

    user_msg = (
        f"Market: {title}\n"
        f"Options: 1. {yes_label} — {yes_price}%  |  2. {no_label} — {no_price}%\n"
        f"Activity: {num_trades} trades, {contracts:,} total contracts\n"
        f"Anomaly score: {score}/100\n"
        f"Flags: {reason_text}"
    )

    try:
        resp = requests.post(
            GROQ_URL,
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": GROQ_MODEL,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                "max_tokens": 200,
                "temperature": 0.4,
            },
            timeout=GROQ_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        analysis = data["choices"][0]["message"]["content"].strip()

        # Cache it
        _cache[cache_key] = (now, analysis)

        # Trim cache if it grows too large
        if len(_cache) > 500:
            cutoff = now - CACHE_TTL
            to_remove = [k for k, (ts, _) in _cache.items() if ts < cutoff]
            for k in to_remove:
                del _cache[k]

        return analysis

    except Exception as e:
        logger.warning(f"Groq AI reasoning failed: {e}")
        return None
