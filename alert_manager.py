import csv
import datetime
import os
import time
from collections import defaultdict
from ai_reasoning import analyze_trade


def _is_polymarket_ticker(ticker: str) -> bool:
    """Polymarket condition IDs are 0x-prefixed 66-char hex strings."""
    return ticker.startswith("0x") and len(ticker) > 20


class AlertManager:
    def __init__(self, alerter, daily_cap=20, ticker_map=None, link_map=None, option_labels_map=None):
        self.alerter = alerter
        self.daily_cap = daily_cap
        self.ticker_map = ticker_map or {}
        self.link_map = link_map or {}
        self.option_labels_map = option_labels_map or {}  # {ticker: (yes_label, no_label)}
        self.alerts_sent_today = 0
        self._cap_date = datetime.date.today()

        # Cooldown tracking (shared across prod + debug)
        self.market_last_alert = defaultdict(float)   # ticker -> timestamp
        self.cluster_last_alert = defaultdict(float)  # cluster_key -> timestamp

        # Production Config
        self.MARKET_COOLDOWN = 600   # 10 mins between alerts for the same market
        self.CLUSTER_COOLDOWN = 300  # 5 mins between alerts for the same cluster

        # Debug Config
        self.alert_mode = os.getenv("ALERT_MODE", "prod").lower()
        self.debug_max_per_min = int(os.getenv("DEBUG_MAX_PER_MIN", "3"))
        self.debug_min_contracts = int(os.getenv("DEBUG_MIN_CONTRACTS", "50"))
        self.debug_min_score = float(os.getenv("DEBUG_MIN_SCORE", "30"))
        self.debug_daily_cap = int(os.getenv("DEBUG_DAILY_CAP", "60"))
        self.debug_min_gap_sec = int(os.getenv("DEBUG_MIN_GAP_SEC", "30"))
        self.debug_market_cooldown = int(os.getenv("DEBUG_MARKET_COOLDOWN", "300"))  # 5 min per market

        # Debug State — rate limiting
        self.debug_sent_last_min = 0
        self.debug_window_start = 0.0
        self.debug_alerts_sent_today = 0
        self.debug_last_sent_ts = 0.0

        # Trade aggregation: collect trades per ticker over a window
        self.AGG_WINDOW = int(os.getenv("DEBUG_AGG_WINDOW", "120"))
        self.AGG_MIN_TRADES = int(os.getenv("DEBUG_AGG_MIN_TRADES", "2"))
        self.AGG_MIN_CONTRACTS = int(os.getenv("DEBUG_AGG_MIN_CONTRACTS", "50"))
        self._agg_buckets = {}

    def _maybe_reset_daily_cap(self):
        """Reset daily alert counters at midnight."""
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

    def _send_internal(self, msg: str) -> bool:
        """Production send path — uses production daily cap."""
        if not self.can_send():
            print("⚠️ Daily alert cap reached. Suppressing.")
            return False
        success = self.alerter.send(msg)
        if not success:
            print(f"ALERT FAILED TO SEND: {msg[:100]}")
        if success:
            self.alerts_sent_today += 1
        return success

    def _send_debug_internal(self, msg: str) -> bool:
        """Debug-only send path — separate daily cap, never touches production cap."""
        if not self.can_send_debug():
            print("⚠️ Debug daily alert cap reached. Suppressing.")
            return False
        success = self.alerter.send(msg)
        if not success:
            print(f"DEBUG ALERT FAILED TO SEND: {msg[:100]}")
        if success:
            self.debug_alerts_sent_today += 1
        return success

    def _log_trade_to_csv(self, ticker: str, title: str, side_to_choose: str, price: int, score: float, link: str):
        csv_file = "alerts_history.csv"
        file_exists = os.path.isfile(csv_file)
        try:
            with open(csv_file, mode="a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                if not file_exists:
                    writer.writerow(["Timestamp", "Ticker", "Name", "Side to Choose", "Price", "Score", "Link"])
                now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                writer.writerow([now_str, ticker, title, side_to_choose, f"{price}¢", round(score, 1), link])
        except Exception as e:
            print(f"⚠️ Failed to log to CSV: {e}")

    def _build_footer(self, ticker: str, link: str) -> str:
        """Build the exchange-aware footer line for alerts."""
        if _is_polymarket_ticker(ticker):
            return f"🔗 <a href='{link}'>Trade on Polymarket</a>"
        return f"🔗 <a href='{link}'>Trade on Kalshi</a>"

    def process_solo_alert(self, ticker: str, score: float, reasons: list, yes_price: int = 50, contracts: int = 0):
        # Production Rule: Score >= 50
        if score < 50:
            return

        now = time.time()
        if now - self.market_last_alert[ticker] < self.MARKET_COOLDOWN:
            return

        title = self.ticker_map.get(ticker, ticker[:40])
        no_price = 100 - yes_price
        link = self.link_map.get(ticker, "https://kalshi.com/browse")
        yes_label, no_label = self.option_labels_map.get(ticker, ("✅ Yes", "❌ No"))

        reason_lines = "".join(f"  • {r}\n" for r in reasons)

        ai_block = ""
        analysis = analyze_trade(title, yes_label, no_label, yes_price, contracts, score, reasons, num_trades=1)
        if analysis:
            ai_block = f"\n🧠 <b>AI Analysis:</b>\n{analysis}\n"

        source = "Polymarket" if _is_polymarket_ticker(ticker) else "Kalshi"

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
        print(f"🚨 SOLO ALERT [{source}] {ticker[:20]} (score={score})")

        success = self._send_internal(msg)
        if success:
            if yes_price <= 50:
                side_to_choose, price_to_buy = yes_label, yes_price
            else:
                side_to_choose, price_to_buy = no_label, 100 - yes_price
            self._log_trade_to_csv(ticker, title, side_to_choose, price_to_buy, score, link)

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
            title = self.ticker_map.get(t, t[:40])
            link = self.link_map.get(t, "https://kalshi.com/browse")
            market_lines += f"{i}. <a href='{link}'>{title}</a>\n"

        msg = (
            f"🔥 <b>Correlated Activity [{source}] — {tier_label}</b>\n"
            f"\n"
            f"📊 {count} related markets moving together:\n"
            f"\n"
            f"{market_lines}"
            f"\n"
            f"⚡ Max Score: {max_score}/100"
        )
        print(f"🔥 CLUSTER ALERT [{source}] {cluster_key} (count={count}, max_score={max_score})")

        success = self._send_internal(msg)
        if success:
            lead_ticker = markets[0] if markets else cluster_key
            lead_title = self.ticker_map.get(lead_ticker, cluster_key[:40])
            lead_link = self.link_map.get(lead_ticker, "https://kalshi.com/")
            self._log_trade_to_csv(lead_ticker, f"[CLUSTER] {lead_title}", "N/A", 0, max_score, lead_link)

        self.cluster_last_alert[cluster_key] = now

    def process_debug_trade(self, ticker: str, yes_price: int, contracts: int, volume_proxy: float, score: float, reasons: list, ts_str: str):
        if self.alert_mode != "debug":
            return

        now = time.time()

        # Per-market cooldown in debug mode — prevents duplicate alerts
        if now - self.market_last_alert[ticker] < self.debug_market_cooldown:
            return

        # Accumulate into aggregation bucket
        bucket = self._agg_buckets.get(ticker)
        if bucket is None or (now - bucket["start"]) > self.AGG_WINDOW:
            self._agg_buckets[ticker] = {
                "trades": 1,
                "contracts": contracts,
                "max_score": score,
                "reasons": list(reasons),
                "yes_price": yes_price,
                "first_ts": ts_str,
                "start": now,
            }
            bucket = self._agg_buckets[ticker]
        else:
            bucket["trades"] += 1
            bucket["contracts"] += contracts
            if score > bucket["max_score"]:
                bucket["max_score"] = score
                bucket["reasons"] = list(reasons)
            bucket["yes_price"] = yes_price

        # Threshold check
        if bucket["contracts"] < self.AGG_MIN_CONTRACTS and bucket["trades"] < self.AGG_MIN_TRADES:
            return

        total_trades     = bucket["trades"]
        total_contracts  = bucket["contracts"]
        best_score       = bucket["max_score"]
        best_reasons     = bucket["reasons"]
        latest_yes       = bucket["yes_price"]

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

        # Global burst smoothing
        if now - self.debug_last_sent_ts < self.debug_min_gap_sec:
            return

        title     = self.ticker_map.get(ticker, ticker[:40])
        link      = self.link_map.get(ticker, "https://kalshi.com/browse")
        no_price  = 100 - latest_yes
        yes_label, no_label = self.option_labels_map.get(ticker, ("✅ Yes", "❌ No"))
        source    = "Polymarket" if _is_polymarket_ticker(ticker) else "Kalshi"

        reason_lines = "".join(f"  • {r}\n" for r in best_reasons)

        ai_block = ""
        analysis = analyze_trade(title, yes_label, no_label, latest_yes, total_contracts, best_score, best_reasons, num_trades=total_trades)
        if analysis:
            ai_block = f"\n🧠 <b>AI Analysis:</b>\n{analysis}\n"

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

        print(f"🧪 DEBUG [{source}] {ticker[:20]} ({total_trades} trades, {total_contracts} contracts, score={best_score})")
        self.debug_last_sent_ts = now
        self.debug_sent_last_min += 1
        self.market_last_alert[ticker] = now  # apply cooldown to prevent dupes

        del self._agg_buckets[ticker]
