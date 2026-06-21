"""
Groq-powered AI reasoning for Kalshi trade alerts.
Adds a brief analyst interpretation to unusual activity alerts.
"""

import json
import logging
import os
import time
from typing import Dict, Optional

import requests

logger = logging.getLogger("kalshi_monitor")

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_TIMEOUT = int(os.getenv("GROQ_TIMEOUT", "8"))

# Simple cache: {cache_key: (timestamp, analysis)}
_cache: Dict[str, tuple] = {}
CACHE_TTL = 1800  # 30 minutes — don't re-analyze the same market

# Spot price cache — refreshed every 5 min so AI can judge distance from target
_spot_prices: Dict[str, float] = {}
_spot_cache_ts: float = 0.0
SPOT_CACHE_TTL = 300  # seconds

ACCURACY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "accuracy_stats.json")
_accuracy_context: str = ""
_accuracy_loaded_ts: float = 0.0
ACCURACY_TTL = 3600  # re-read file at most once per hour


def _load_accuracy_context() -> str:
    """Read rolling accuracy stats written by auto_resolve.py and return
    a compact summary line to inject into the AI system prompt."""
    global _accuracy_context, _accuracy_loaded_ts
    now = time.time()
    if now - _accuracy_loaded_ts < ACCURACY_TTL and _accuracy_context:
        return _accuracy_context
    try:
        with open(ACCURACY_FILE) as f:
            data = json.load(f)
        cats = data.get("categories", {})
        parts = []
        for cat, s in cats.items():
            if s.get("total", 0) >= 5:  # only show categories with enough data
                parts.append(f"{cat} {s['rate']:.0%} ({s['total']} resolved)")
        overall = data.get("overall", {})
        if overall.get("total", 0) >= 10:
            parts.insert(0, f"overall {overall['rate']:.0%}")
        _accuracy_context = "Historical win rates (last 30d): " + ", ".join(parts) if parts else ""
        _accuracy_loaded_ts = now
    except Exception:
        _accuracy_context = ""
    return _accuracy_context


def _refresh_spot_prices() -> None:
    global _spot_prices, _spot_cache_ts
    now = time.time()
    if now - _spot_cache_ts < SPOT_CACHE_TTL:
        return
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price"
            "?ids=bitcoin,ethereum,solana&vs_currencies=usd",
            timeout=5,
        )
        r.raise_for_status()
        d = r.json()
        _spot_prices = {
            "BTC": float(d.get("bitcoin",  {}).get("usd", 0) or 0),
            "ETH": float(d.get("ethereum", {}).get("usd", 0) or 0),
            "SOL": float(d.get("solana",   {}).get("usd", 0) or 0),
        }
        _spot_cache_ts = now
    except Exception as e:
        logger.debug(f"Spot price refresh failed: {e}")


SYSTEM_PROMPT = (
    "You are a sharp prediction-market analyst advising a US-based trader. "
    "A monitoring bot flagged unusual trading activity. Your job is to FIRST decide "
    "if this is a real, actionable signal — then explain it.\n\n"

    "STEP 1 — FILTER (respond ONLY with 'SKIP: <one-line reason>' if ANY of these apply):\n"
    "  - Sports/games: any match result, team win, score, player stat "
    "(NFL, NBA, MLB, NHL, MLS, soccer, tennis, golf, UFC, esports)\n"
    "  - Social media trivia: tweet counts, post counts, follower counts, "
    "video views (MrBeast, TikTok, YouTube)\n"
    "  - Celebrity/entertainment: reality TV, award shows, who wins a show\n"
    "  - Impossible price targets: YES price ≤8% with no credible recent news\n"
    "  - Price target too far from spot: if current BTC/ETH/SOL price is shown below "
    "and the market target requires a move >15% that has NOT already happened, SKIP — "
    "e.g. BTC at $79k asking 'above $95k' requires a 20% move that hasn't occurred\n"
    "  - Market nearly resolved: YES price ≥90% means the outcome is priced as certain — "
    "no actionable edge left, SKIP\n"
    "  - Micro-windows: events resolving in <2 hours with no clear informational edge\n"
    "  - Speculative regime change: leader removal, country invasion, regime collapse "
    "within 2 weeks with YES <15%\n"
    "  - Unrealistic political ultimatums within 30 days: permanent peace deals, "
    "full nuclear disarmament, complete surrender of weapons stockpiles — "
    "SKIP unless YES price is already >70%\n\n"

    "STEP 2 — ANALYZE (only if not skipped):\n"
    "1. One plain-English sentence: what exactly is being predicted?\n"
    "2. Why this volume surge might matter — what could traders know or what "
    "news is driving it?\n"
    "3. Actionable take — worth acting on or noise?\n"
    "4. State direction: 'Leaning YES' or 'Leaning NO'.\n\n"

    "DIRECTION CALIBRATION — READ CAREFULLY:\n"
    "  - Backtest of 460 resolved alerts: when you said 'Leaning YES' the bot "
    "won 21% of the time. When you said 'Leaning NO' the bot won 58% of the "
    "time. You are systematically too willing to follow whale orders into "
    "YES longshots. Calibrate AGAINST that bias.\n"
    "  - Whale orders at YES <=25c are almost always hedges, exit liquidity, "
    "or longshot lottery tickets — NOT informed conviction. Default to "
    "'Leaning NO' at that price band unless there is a clear news catalyst "
    "or price has already moved >=8c in the right direction (momentum).\n"
    "  - At YES 30-70c the market is genuinely uncertain. Pick the side with "
    "the stronger fundamental justification, but lean NO when unsure.\n"
    "  - Break-even math: to make money buying YES at price P cents, you must "
    "win more than P/100 of the time. At YES=10c you need >10% hit rate; "
    "historical alerts at that price band have hit 7%. So a 'Leaning YES' "
    "call at extreme cheap prices is almost always EV-negative.\n"
    "  - Geopolitical longshots ('Iran X by date Y?', 'airspace closes?', "
    "'regime change?', 'ceasefire?') are already filtered out before they "
    "reach you. If you still see one, it's an edge case — Lean NO.\n\n"

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

    score_bucket = int(score // 10) * 10
    top_reason = reasons[0][:30] if reasons else ""
    cache_key = f"{title}:{yes_price}:{score_bucket}:{top_reason}"
    now = time.time()
    cached = _cache.get(cache_key)
    if cached and (now - cached[0]) < CACHE_TTL:
        return cached[1]

    # Refresh spot prices so AI can judge price-target distance
    _refresh_spot_prices()

    no_price = 100 - yes_price
    reason_text = "; ".join(reasons) if reasons else "Multiple signals"

    spot_context = ""
    if _spot_prices:
        lines = [f"  {sym}: ${price:,.0f}" for sym, price in _spot_prices.items() if price]
        if lines:
            spot_context = "\nCurrent spot prices:\n" + "\n".join(lines)

    user_msg = (
        f"Market: {title}\n"
        f"Options: 1. {yes_label} — {yes_price}%  |  2. {no_label} — {no_price}%\n"
        f"Activity: {num_trades} trades, {contracts:,} total contracts\n"
        f"Anomaly score: {score}/100\n"
        f"Flags: {reason_text}"
        f"{spot_context}"
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
                    {"role": "system", "content": SYSTEM_PROMPT + (
                        f"\n\n{acc}" if (acc := _load_accuracy_context()) else ""
                    )},
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

        _cache[cache_key] = (now, analysis)

        if len(_cache) > 500:
            cutoff = now - CACHE_TTL
            to_remove = [k for k, (ts, _) in _cache.items() if ts < cutoff]
            for k in to_remove:
                del _cache[k]

        return analysis

    except Exception as e:
        logger.warning(f"Groq AI reasoning failed: {e}")
        return None
