"""
Polymarket wallet reputation tracker.

Tracks per-wallet statistics in a local SQLite database and calls the
Polymarket positions API to compute historical win rates. A wallet that
repeatedly wins across diverse markets is a meaningful signal.

Why wallets are trackable:
  - Polymarket proxy wallets are account-bound (require signup/KYC)
  - Most serious traders keep the same wallet for months
  - A wallet with >65% win rate on 10+ markets: p < 0.001 by chance alone

Score output (added to anomaly score in scoring.py):
  - Fresh whale  (first 3 trades seen, avg trade > $200): +15
  - Known winner (≥10 resolved, win_rate ≥ 0.70):        +25
  - Strong winner(≥5 resolved,  win_rate ≥ 0.62):        +15
  - Decent edge  (≥3 resolved,  win_rate ≥ 0.58):        +8
"""

import logging
import sqlite3
import time
from collections import defaultdict
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger("kalshi_monitor")

DB_PATH = "wallet_tracker.db"

# Cache wallet stats to avoid querying the API too often
_stats_cache: Dict[str, tuple] = {}  # wallet -> (ts, stats_dict)
CACHE_TTL = 4 * 3600  # re-fetch positions every 4 hours per wallet

# Only call the positions API for wallets whose single trade exceeded this USD
POSITIONS_API_MIN_USD = 150


# ── DB init ───────────────────────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def init_db() -> None:
    with _conn() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS wallets (
                wallet        TEXT PRIMARY KEY,
                first_seen    INTEGER NOT NULL,
                last_seen     INTEGER NOT NULL,
                trades_seen   INTEGER DEFAULT 0,
                total_usd     REAL    DEFAULT 0,
                resolved_bets INTEGER DEFAULT 0,
                wins          INTEGER DEFAULT 0,
                losses        INTEGER DEFAULT 0,
                total_pnl     REAL    DEFAULT 0,
                last_api_check INTEGER DEFAULT 0
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS wallet_bets (
                wallet       TEXT    NOT NULL,
                condition_id TEXT    NOT NULL,
                side         TEXT    NOT NULL,
                outcome_idx  INTEGER NOT NULL,
                size_usd     REAL    NOT NULL,
                price        REAL    NOT NULL,
                ts           INTEGER NOT NULL,
                PRIMARY KEY (wallet, condition_id, ts)
            )
        """)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _fetch_positions_stats(wallet: str) -> Dict[str, Any]:
    """Call Polymarket positions API and return win-rate stats."""
    try:
        r = requests.get(
            "https://data-api.polymarket.com/positions",
            params={"user": wallet, "limit": 100, "sizeThreshold": "0.01"},
            timeout=10,
        )
        r.raise_for_status()
        positions = r.json()
    except Exception as e:
        logger.debug(f"Wallet API error ({wallet[:10]}…): {e}")
        return {}

    resolved = [
        p for p in positions
        if float(p.get("realizedPnl") or 0) != 0
    ]
    if not resolved:
        return {"resolved_bets": 0, "wins": 0, "losses": 0, "total_pnl": 0.0}

    wins   = sum(1 for p in resolved if float(p.get("realizedPnl") or 0) > 0)
    losses = sum(1 for p in resolved if float(p.get("realizedPnl") or 0) < 0)
    pnl    = sum(float(p.get("realizedPnl") or 0) for p in resolved)

    return {
        "resolved_bets": wins + losses,
        "wins":          wins,
        "losses":        losses,
        "total_pnl":     pnl,
    }


def _refresh_wallet_api(wallet: str, db: sqlite3.Connection, force: bool = False) -> None:
    """Refresh positions API data for wallet if cache expired."""
    row = db.execute("SELECT last_api_check FROM wallets WHERE wallet=?", (wallet,)).fetchone()
    if not row:
        return
    now = int(time.time())
    if not force and (now - (row["last_api_check"] or 0)) < CACHE_TTL:
        return

    stats = _fetch_positions_stats(wallet)
    if not stats:
        db.execute("UPDATE wallets SET last_api_check=? WHERE wallet=?", (now, wallet))
        return

    db.execute("""
        UPDATE wallets SET
            resolved_bets = ?,
            wins          = ?,
            losses        = ?,
            total_pnl     = ?,
            last_api_check= ?
        WHERE wallet = ?
    """, (stats["resolved_bets"], stats["wins"], stats["losses"],
          stats["total_pnl"], now, wallet))
    logger.info(
        f"Wallet {wallet[:10]}… | resolved={stats['resolved_bets']} "
        f"wins={stats['wins']} PnL={stats['total_pnl']:.0f}"
    )


# ── Public API ────────────────────────────────────────────────────────────────

def record_trade(
    wallet: str,
    condition_id: str,
    side: str,
    outcome_idx: int,
    size_usd: float,
    price: float,
) -> Dict[str, Any]:
    """
    Record a trade from this wallet and return its current stats dict.
    Call this for every Polymarket trade we process.
    """
    now = int(time.time())
    with _conn() as db:
        db.execute("""
            INSERT INTO wallets (wallet, first_seen, last_seen, trades_seen, total_usd)
            VALUES (?, ?, ?, 1, ?)
            ON CONFLICT(wallet) DO UPDATE SET
                last_seen   = excluded.last_seen,
                trades_seen = trades_seen + 1,
                total_usd   = total_usd + excluded.total_usd
        """, (wallet, now, now, size_usd))

        db.execute("""
            INSERT OR IGNORE INTO wallet_bets
                (wallet, condition_id, side, outcome_idx, size_usd, price, ts)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (wallet, condition_id, side, outcome_idx, size_usd, price, now))

        if size_usd >= POSITIONS_API_MIN_USD:
            _refresh_wallet_api(wallet, db)

        row = db.execute("SELECT * FROM wallets WHERE wallet=?", (wallet,)).fetchone()

    resolved   = row["resolved_bets"] or 0
    wins       = row["wins"]          or 0
    win_rate   = wins / max(resolved, 1)
    avg_usd    = (row["total_usd"] or 0) / max(row["trades_seen"] or 1, 1)
    is_fresh   = (row["trades_seen"] or 0) <= 3

    return {
        "wallet":       wallet,
        "is_fresh":     is_fresh,
        "trades_seen":  row["trades_seen"] or 0,
        "avg_usd":      avg_usd,
        "resolved_bets": resolved,
        "win_rate":     win_rate,
        "total_pnl":    row["total_pnl"] or 0.0,
    }


def get_wallet_score(stats: Dict[str, Any]) -> tuple[int, str]:
    """
    Return (extra_points, reason_string) for a wallet.
    Points are added to the anomaly score in scoring.py.
    """
    pts    = 0
    reason = ""

    is_fresh   = stats.get("is_fresh", False)
    avg_usd    = stats.get("avg_usd", 0)
    resolved   = stats.get("resolved_bets", 0)
    win_rate   = stats.get("win_rate", 0.5)
    pnl        = stats.get("total_pnl", 0)

    if is_fresh and avg_usd >= 500:
        pts    = 15
        reason = f"Fresh whale wallet (avg ${avg_usd:.0f}/trade, first 3 bets)"
    elif is_fresh and avg_usd >= 100:
        pts    = 8
        reason = f"New wallet, large position (avg ${avg_usd:.0f}/trade)"

    if resolved >= 10 and win_rate >= 0.70:
        pts    = max(pts, 25)
        reason = f"Elite predictor: {win_rate*100:.0f}% win rate ({resolved} bets, PnL ${pnl:+.0f})"
    elif resolved >= 5 and win_rate >= 0.62:
        pts    = max(pts, 15)
        reason = f"Strong track record: {win_rate*100:.0f}% wins ({resolved} bets)"
    elif resolved >= 3 and win_rate >= 0.58:
        pts    = max(pts, 8)
        reason = f"Profitable wallet: {win_rate*100:.0f}% wins ({resolved} bets)"

    return pts, reason
