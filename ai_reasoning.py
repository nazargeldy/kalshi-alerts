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
    "You are a sharp prediction-market analyst advising a US-based trader. "
    "A monitoring bot flagged unusual trading activity. Your job is to FIRST decide "
    "if this is a real, actionable signal — then explain it.\n\n"

    "STEP 1 — FILTER (respond ONLY with 'SKIP: <one-line reason>' if ANY of these apply):\n"
    "  - Sports/games: any match result, team win, score, player stat (NFL, NBA, MLB, NHL, MLS, soccer, tennis, golf, UFC, esports)\n"
    "  - Social media trivia: tweet counts, post counts, follower counts, video views (MrBeast, TikTok, YouTube)\n"
    "  - Celebrity/entertainment: reality TV, award shows, who wins a show\n"
    "  - Impossible price targets: the YES price is ≤8% AND there is no recent credible news that makes this plausible\n"
    "  - Micro-windows: events resolving in <2 hours with no clear informational edge\n"
    "  - Highly speculative regime change: leader removal, country invasion, regime collapse within 2 weeks with YES <15%\n\n"

    "STEP 2 — ANALYZE (only if not skipped):\n"
    "1. One plain-English sentence: what exactly is being predicted?\n"
    "2. Why this volume surge might matter — what could traders know or what news is driving it?\n"
    "3. Actionable take — worth acting on or noise?\n"
    "4. State direction: 'Leaning YES' or 'Leaning NO' based on which side the smart money is buying.\n\n"

    "Keep analysis to 2-4 sentences. No disclaimers. Write like a trading desk analyst."
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

    # Check cache — include score bucket and top reason so different alerts on
    # the same market at the same price don't reuse a stale analysis.
    score_bucket = int(score // 10) * 10  # round to nearest 10
    top_reason = reasons[0][:30] if reasons else ""
    cache_key = f"{title}:{yes_price}:{score_bucket}:{top_reason}"
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
