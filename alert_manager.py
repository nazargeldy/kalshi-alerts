import csv
import datetime
import os
import re
import time
from collections import defaultdict
from ai_reasoning import analyze_trade
from notion_logger import log_alert as notion_log

CSV_FILE = "alerts_history.csv"
CSV_HEADERS = [
    "Timestamp",        # when the alert fired
    "Source",           # Kalshi | Polymarket
    "Alert Type",       # SOLO | CLUSTER | DEBUG
    "Market",           # clean title
    "Side to Buy",      # which outcome to trade (YES label or NO label)
    "Entry Price (¢)",  # price in cents at alert time
    "Contracts Seen",   # how many contracts triggered the alert
    "Score /100",       # anomaly score
    "AI Lean",          # Leaning YES / Leaning NO / N/A (extracted from AI text)
    "Reasons",          # pipe-separated list of why it was flagged
    "Link",             # direct link to the market
    # ── columns you fill in after the market resolves ──
    "Result",           # WIN | LOSS | PUSH | SKIP (fill manually)
    "Exit Price (¢)",   # what it closed at (fill manually)
    "P&L per contract", # = Exit - Entry if WIN, = -Entry if LOSS (fill manually)
    "Notes",            # anything you want to remember
]


def _extract_ai_lean(analysis: str) -> str:
    """Pull 'Leaning YES' / 'Leaning NO' out of the AI analysis string."""
    if not analysis:
        return ""
    m = re.search(r"[Ll]eaning\s+(YES|NO|Up|Down|yes|no)", analysis)
    if m:
        word = m.group(1).upper()
        return f"Leaning {word}"
    # Fallback: look for explicit direction words near the end
    lower = analysis.lower()
    if "leaning yes" in lower or "lean yes" in lower:
        return "Leaning YES"
    if "leaning no" in lower or "lean no" in lower:
        return "Leaning NO"
    return ""


def _is_polymarket_ticker(ticker: str) -> bool:
    """Polymarket condition IDs are 0x-prefixed 66-char hex strings."""
    return ticker.startswith("0x") and len(ticker) > 20


class AlertManager:
    def __init__(self, alerter, daily_cap=20, ticker_map=None, link_map=None, option_labels_map=None):
        self.alerter = alerter
        self.daily_cap = daily_cap
        self.ticker_map = ticker_map or {}
        self.link_map = link_map or {}
        self.option_labels_map = option_labels_map or {}
        self.alerts_sent_today = 0
        self._cap_date = datetime.date.today()

        # Cooldown tracking (shared across prod + debug)
        self.market_last_alert = defaultdict(float)
        self.cluster_last_alert = defaultdict(float)

        # Production Config
        self.MARKET_COOLDOWN = 600   # 10 min between alerts for the same market
        self.CLUSTER_COOLDOWN = 300  # 5 min between alerts for the same cluster

        # Debug Config
        self.alert_mode = os.getenv("ALERT_MODE", "prod").lower()
        self.debug_max_per_min = int(os.getenv("DEBUG_MAX_PER_MIN", "2"))
        self.debug_min_contracts = int(os.getenv("DEBUG_MIN_CONTRACTS", "100"))
        self.debug_min_score = float(os.getenv("DEBUG_MIN_SCORE", "45"))
        self.debug_daily_cap = int(os.getenv("DEBUG_DAILY_CAP", "25"))
        self.debug_min_gap_sec = int(os.getenv("DEBUG_MIN_GAP_SEC", "120"))
        self.debug_market_cooldown = int(os.getenv("DEBUG_MARKET_COOLDOWN", "1800"))  # 30 min per market

        # Debug State
        self.debug_sent_last_min = 0
        self.debug_window_start = 0.0
        self.debug_alerts_sent_today = 0
        self.debug_last_sent_ts = 0.0

        # Trade aggregation
        self.AGG_WINDOW = int(os.getenv("DEBUG_AGG_WINDOW", "120"))
        self.AGG_MIN_TRADES = int(os.getenv("DEBUG_AGG_MIN_TRADES", "3"))
        self.AGG_MIN_CONTRACTS = int(os.getenv("DEBUG_AGG_MIN_CONTRACTS", "100"))
        self._agg_buckets = {}

    # ── Daily cap helpers ─────────────────────────────────────────────────────

    def _maybe_reset_daily_cap(self):
        today = datetime.date.today()
        if today != self._cap_date:
            self._cap_date = today
            self.alerts_sent_today = 0
            self.debug_alerts_sent_today = 0

    def can_send(self) -> bool:
        self._maybe_reset_daily_cap()
        return self.alerts_sent_today < self.daily_cap

    def can_send_debug(self) -> bool:
        self._maybe_reset_daily_cap()
        return self.debug_alerts_sent_today < self.debug_daily_cap

    # ── Send helpers ──────────────────────────────────────────────────────────

    def _send_internal(self, msg: str) -> bool:
        if not self.can_send():
            print("⚠️ Daily alert cap reached.")
            return False
        success = self.alerter.send(msg)
        if success:
            self.alerts_sent_today += 1
        else:
            print(f"ALERT FAILED: {msg[:80]}")
        return success

    def _send_debug_internal(self, msg: str) -> bool:
        if not self.can_send_debug():
            print("⚠️ Debug daily cap reached.")
            return False
        success = self.alerter.send(msg)
        if success:
            self.debug_alerts_sent_today += 1
        else:
            print(f"DEBUG ALERT FAILED: {msg[:80]}")
        return success

    # ── CSV logging ───────────────────────────────────────────────────────────

    def _log(
        self,
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
        ts_str: str = "",
    ):
        """Write to both CSV (local backup) and Notion."""
        now_str = ts_str or datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # ── CSV backup ────────────────────────────────────────────────────────
        file_exists = os.path.isfile(CSV_FILE)
        try:
            with open(CSV_FILE, mode="a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                if not file_exists:
                    writer.writerow(CSV_HEADERS)
                writer.writerow([
                    now_str, source, alert_type, title, side,
                    f"{entry_price}¢", contracts, round(score, 1),
                    _extract_ai_lean(analysis), " | ".join(reasons), link,
                    "", "", "", "",
                ])
        except Exception as e:
            print(f"⚠️ CSV log failed: {e}")

        # ── Notion ────────────────────────────────────────────────────────────
        notion_log(
            ticker=ticker, title=title, source=source, alert_type=alert_type,
            side=side, entry_price=entry_price, contracts=contracts,
            score=score, reasons=reasons, link=link, analysis=analysis,
            timestamp_str=now_str,
        )

    # ── Footer ────────────────────────────────────────────────────────────────

    def _build_footer(self, ticker: str, link: str) -> str:
        if _is_polymarket_ticker(ticker):
            return f"🔗 <a href='{link}'>Trade on Polymarket</a>"
        return f"🔗 <a href='{link}'>Trade on Kalshi</a>"

    # ── Alerts ────────────────────────────────────────────────────────────────

    def process_solo_alert(self, ticker: str, score: float, reasons: list, yes_price: int = 50, contracts: int = 0):
        if score < 50:
            return

        now = time.time()
        if now - self.market_last_alert[ticker] < self.MARKET_COOLDOWN:
            return

        title     = self.ticker_map.get(ticker, ticker[:40])
        no_price  = 100 - yes_price
        link      = self.link_map.get(ticker, "https://kalshi.com/browse")
        yes_label, no_label = self.option_labels_map.get(ticker, ("✅ Yes", "❌ No"))
        source    = "Polymarket" if _is_polymarket_ticker(ticker) else "Kalshi"

        reason_lines = "".join(f"  • {r}\n" for r in reasons)
        analysis = analyze_trade(title, yes_label, no_label, yes_price, contracts, score, reasons, num_trades=1) or ""
        ai_block = f"\n🧠 <b>AI Analysis:</b>\n{analysis}\n" if analysis else ""

        msg = (
            f"🚨 <b>Unusual Activity — {source}</b>\n"
            f"\n"
            f"❓ <b>{title}</b>\n"
            f"\n"
            f"1. {yes_label} — {yes_price}%\n"
            f"2. {no_label} — {no_price}%\n"
            f"\n"
            f"💰 {contracts:,} contracts · ⚡ Score: {score}/100\n"
            f"\n"
            f"📈 <b>Why flagged:</b>\n"
            f"{reason_lines}"
            f"{ai_block}"
            f"\n"
            f"{self._build_footer(ticker, link)}"
        )
        print(f"🚨 SOLO [{source}] {ticker[:20]} score={score}")

        success = self._send_internal(msg)
        if success:
            # Side = the surprising/underdog side (cheaper one is where the anomaly likely is)
            if yes_price <= 50:
                side, entry = yes_label, yes_price
            else:
                side, entry = no_label, no_price
            self._log(ticker, title, source, "SOLO", side, entry,
                             contracts, score, reasons, link, analysis)

        self.market_last_alert[ticker] = now

    def process_cluster_alert(self, cluster_key: str, count: int, max_score: float, markets: list):
        is_tier_1 = (max_score >= 65 and count >= 2)
        is_tier_2 = (max_score >= 50 and count >= 3)
        if not (is_tier_1 or is_tier_2):
            return

        now = time.time()
        if now - self.cluster_last_alert[cluster_key] < self.CLUSTER_COOLDOWN:
            return

        tier_label = "HIGH" if is_tier_1 else "MODERATE"
        source = "Polymarket" if cluster_key.startswith("POLY:") else "Kalshi"

        market_lines = ""
        for i, t in enumerate(markets, 1):
            t_title = self.ticker_map.get(t, t[:40])
            t_link  = self.link_map.get(t, "https://kalshi.com/browse")
            market_lines += f"{i}. <a href='{t_link}'>{t_title}</a>\n"

        msg = (
            f"🔥 <b>Correlated Activity [{source}] — {tier_label}</b>\n"
            f"\n"
            f"📊 {count} related markets moving together:\n"
            f"\n"
            f"{market_lines}"
            f"\n"
            f"⚡ Max Score: {max_score}/100"
        )
        print(f"🔥 CLUSTER [{source}] {cluster_key} count={count} max={max_score}")

        success = self._send_internal(msg)
        if success:
            lead = markets[0] if markets else cluster_key
            self._log(
                lead,
                f"[CLUSTER] {self.ticker_map.get(lead, cluster_key[:40])}",
                source, "CLUSTER", "N/A", 0, 0, max_score,
                [f"{count} correlated markets, tier={tier_label}"],
                self.link_map.get(lead, "https://kalshi.com/"),
            )

        self.cluster_last_alert[cluster_key] = now

    def process_debug_trade(self, ticker: str, yes_price: int, contracts: int, volume_proxy: float, score: float, reasons: list, ts_str: str):
        if self.alert_mode != "debug":
            return

        now = time.time()

        # Per-market cooldown
        if now - self.market_last_alert[ticker] < self.debug_market_cooldown:
            return

        # Accumulate into aggregation bucket
        bucket = self._agg_buckets.get(ticker)
        if bucket is None or (now - bucket["start"]) > self.AGG_WINDOW:
            self._agg_buckets[ticker] = {
                "trades": 1, "contracts": contracts,
                "max_score": score, "reasons": list(reasons),
                "yes_price": yes_price, "first_ts": ts_str, "start": now,
            }
            bucket = self._agg_buckets[ticker]
        else:
            bucket["trades"] += 1
            bucket["contracts"] += contracts
            if score > bucket["max_score"]:
                bucket["max_score"] = score
                bucket["reasons"] = list(reasons)
            bucket["yes_price"] = yes_price

        if bucket["contracts"] < self.AGG_MIN_CONTRACTS and bucket["trades"] < self.AGG_MIN_TRADES:
            return

        total_trades    = bucket["trades"]
        total_contracts = bucket["contracts"]
        best_score      = bucket["max_score"]
        best_reasons    = bucket["reasons"]
        latest_yes      = bucket["yes_price"]

        if best_score < self.debug_min_score:
            return
        if latest_yes <= 0 or latest_yes >= 100:
            return

        # Per-minute rate limit
        if now - self.debug_window_start > 60:
            self.debug_window_start = now
            self.debug_sent_last_min = 0
        if self.debug_sent_last_min >= self.debug_max_per_min:
            return

        if now - self.debug_last_sent_ts < self.debug_min_gap_sec:
            return

        title     = self.ticker_map.get(ticker, ticker[:40])
        link      = self.link_map.get(ticker, "https://kalshi.com/browse")
        no_price  = 100 - latest_yes
        yes_label, no_label = self.option_labels_map.get(ticker, ("✅ Yes", "❌ No"))
        source    = "Polymarket" if _is_polymarket_ticker(ticker) else "Kalshi"

        reason_lines = "".join(f"  • {r}\n" for r in best_reasons)
        analysis = analyze_trade(title, yes_label, no_label, latest_yes, total_contracts, best_score, best_reasons, num_trades=total_trades) or ""
        ai_block = f"\n🧠 <b>AI Analysis:</b>\n{analysis}\n" if analysis else ""

        msg = (
            f"🧪 <b>[DEBUG] {source} Alert</b>\n"
            f"\n"
            f"❓ <b>{title}</b>\n"
            f"\n"
            f"1. {yes_label} — {latest_yes}%\n"
            f"2. {no_label} — {no_price}%\n"
            f"\n"
            f"📊 {total_trades} trade(s) · 💰 {total_contracts:,} contracts · ⚡ Score: {best_score}/100\n"
            f"\n"
            f"📈 <b>Why flagged:</b>\n"
            f"{reason_lines}"
            f"{ai_block}"
            f"\n"
            f"{self._build_footer(ticker, link)}\n"
            f"🕐 {ts_str}"
        )

        sent = self._send_debug_internal(msg)
        if not sent:
            return

        print(f"🧪 DEBUG [{source}] {ticker[:20]} trades={total_trades} contracts={total_contracts} score={best_score}")
        self.debug_last_sent_ts = now
        self.debug_sent_last_min += 1
        self.market_last_alert[ticker] = now

        # Log to CSV for P&L tracking
        if latest_yes <= 50:
            side, entry = yes_label, latest_yes
        else:
            side, entry = no_label, no_price
        self._log(ticker, title, source, "DEBUG", side, entry,
                         total_contracts, best_score, best_reasons, link, analysis)

        del self._agg_buckets[ticker]
