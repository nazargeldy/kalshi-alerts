import datetime
import os
import time
from collections import defaultdict
from ai_reasoning import analyze_trade

class AlertManager:
    def __init__(self, alerter, daily_cap=20, ticker_map=None, link_map=None, option_labels_map=None):
        self.alerter = alerter
        self.daily_cap = daily_cap
        self.ticker_map = ticker_map or {}
        self.link_map = link_map or {}
        self.option_labels_map = option_labels_map or {}  # {ticker: (yes_label, no_label)}
        self.alerts_sent_today = 0
        self._cap_date = datetime.date.today()
        
        # Cooldown tracking
        self.market_last_alert = defaultdict(float) # ticker -> timestamp
        self.cluster_last_alert = defaultdict(float) # cluster_key -> timestamp
        
        # Production Config
        self.MARKET_COOLDOWN = 600  # 10 mins
        self.CLUSTER_COOLDOWN = 300 # 5 mins

        # Debug Config
        self.alert_mode = os.getenv("ALERT_MODE", "prod").lower()
        self.debug_max_per_min = int(os.getenv("DEBUG_MAX_PER_MIN", "3"))
        self.debug_min_contracts = int(os.getenv("DEBUG_MIN_CONTRACTS", "50"))

        # Debug State — rate limiting
        self.debug_sent_last_min = 0
        self.debug_window_start = 0

        # Trade aggregation: collect trades per ticker over a window
        self.AGG_WINDOW = int(os.getenv("DEBUG_AGG_WINDOW", "120"))  # 2 min
        self.AGG_MIN_TRADES = int(os.getenv("DEBUG_AGG_MIN_TRADES", "3"))  # min trades to alert
        self.AGG_MIN_CONTRACTS = int(os.getenv("DEBUG_AGG_MIN_CONTRACTS", "50"))  # min total contracts
        # {ticker: {"trades": int, "contracts": int, "max_score": float,
        #           "reasons": list, "yes_price": int, "first_ts": str, "start": float}}
        self._agg_buckets = {}

    def _maybe_reset_daily_cap(self):
        """Reset the daily alert counter at midnight."""
        today = datetime.date.today()
        if today != self._cap_date:
            self._cap_date = today
            self.alerts_sent_today = 0

    def can_send(self) -> bool:
        self._maybe_reset_daily_cap()
        return self.alerts_sent_today < self.daily_cap

    def _send_internal(self, msg: str):
        if not self.can_send():
            print("⚠️ Daily alert cap reached. Suppressing.")
            return

        success = self.alerter.send(msg)
        if success:
            self.alerts_sent_today += 1

    def process_solo_alert(self, ticker: str, score: float, reasons: list, yes_price: int = 50, contracts: int = 0):
        # Production Rule: Score >= 60
        if score < 60:
            return

        now = time.time()
        # Rule: Market Cooldown
        if now - self.market_last_alert[ticker] < self.MARKET_COOLDOWN:
            return

        title = self.ticker_map.get(ticker, ticker)
        no_price = 100 - yes_price
        link = self.link_map.get(ticker, f"https://kalshi.com/browse")
        yes_label, no_label = self.option_labels_map.get(ticker, ("Yes", "No"))

        reason_lines = ""
        for r in reasons:
            reason_lines += f"  • {r}\n"

        # AI reasoning
        ai_block = ""
        analysis = analyze_trade(title, yes_label, no_label, yes_price, contracts, score, reasons, num_trades=1)
        if analysis:
            ai_block = f"\n🧠 <b>AI Analysis:</b>\n{analysis}\n"

        msg = (
            f"🚨 <b>Unusual Activity Detected</b>\n"
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
            f"🔗 <a href='{link}'>Trade on Kalshi</a>"
        )
        print(f"🚨 SOLO ALERT SENT for {ticker}")
        self._send_internal(msg)
        self.market_last_alert[ticker] = now

    def process_cluster_alert(self, cluster_key: str, count: int, max_score: float, markets: list):
        # Production Rules:
        # Tier 1: Max Score >= 70 AND Count >= 2
        # Tier 2: Max Score >= 60 AND Count >= 3
        
        is_tier_1 = (max_score >= 70 and count >= 2)
        is_tier_2 = (max_score >= 60 and count >= 3)
        
        if not (is_tier_1 or is_tier_2):
            return

        now = time.time()
        # Rule: Cluster Cooldown
        if now - self.cluster_last_alert[cluster_key] < self.CLUSTER_COOLDOWN:
            return

        tier_label = "HIGH" if is_tier_1 else "MODERATE"
        
        market_lines = ""
        for i, t in enumerate(markets, 1):
            title = self.ticker_map.get(t, t)
            link = self.link_map.get(t, "https://kalshi.com/browse")
            market_lines += f"{i}. <a href='{link}'>{title}</a>\n"

        msg = (
            f"🔥 <b>Correlated Activity — {tier_label}</b>\n"
            f"\n"
            f"📊 {count} related markets moving together:\n"
            f"\n"
            f"{market_lines}"
            f"\n"
            f"⚡ Max Score: {max_score}/100"
        )
        print(f"🔥 CLUSTER ALERT SENT for {cluster_key}")
        self._send_internal(msg)
        self.cluster_last_alert[cluster_key] = now

    def process_debug_trade(self, ticker: str, yes_price: int, contracts: int, volume_proxy: float, score: float, reasons: list, ts_str: str):
        if self.alert_mode != "debug":
            return

        now = time.time()

        # Accumulate into aggregation bucket
        bucket = self._agg_buckets.get(ticker)
        if bucket is None or (now - bucket["start"]) > self.AGG_WINDOW:
            # Start new bucket
            self._agg_buckets[ticker] = {
                "trades": 1,
                "contracts": contracts,
                "max_score": score,
                "reasons": list(reasons),
                "yes_price": yes_price,
                "first_ts": ts_str,
                "start": now,
            }
            return

        # Add to existing bucket
        bucket["trades"] += 1
        bucket["contracts"] += contracts
        if score > bucket["max_score"]:
            bucket["max_score"] = score
            bucket["reasons"] = list(reasons)
        bucket["yes_price"] = yes_price  # latest price

        # Check if bucket is ready to fire
        if bucket["contracts"] < self.AGG_MIN_CONTRACTS and bucket["trades"] < self.AGG_MIN_TRADES:
            return

        # Rate limit
        if now - self.debug_window_start > 60:
            self.debug_window_start = now
            self.debug_sent_last_min = 0
        if self.debug_sent_last_min >= self.debug_max_per_min:
            return

        # Fire the aggregated alert
        total_trades = bucket["trades"]
        total_contracts = bucket["contracts"]
        best_score = bucket["max_score"]
        best_reasons = bucket["reasons"]
        latest_yes = bucket["yes_price"]

        title = self.ticker_map.get(ticker, ticker)
        link = self.link_map.get(ticker, "https://kalshi.com/browse")
        no_price = 100 - latest_yes
        yes_label, no_label = self.option_labels_map.get(ticker, ("✅ Yes", "❌ No"))

        reason_lines = ""
        for r in best_reasons:
            reason_lines += f"  • {r}\n"

        # AI reasoning
        ai_block = ""
        analysis = analyze_trade(title, yes_label, no_label, latest_yes, total_contracts, best_score, best_reasons, num_trades=total_trades)
        if analysis:
            ai_block = f"\n🧠 <b>AI Analysis:</b>\n{analysis}\n"

        msg = (
            f"❓ <b>{title}</b>\n"
            f"\n"
            f"1. {yes_label} — {latest_yes}%\n"
            f"2. {no_label} — {no_price}%\n"
            f"\n"
            f"📊 {total_trades} trades · 💰 {total_contracts:,} contracts · ⚡ Score: {best_score}/100\n"
            f"\n"
            f"📈 <b>Why flagged:</b>\n"
            f"{reason_lines}"
            f"{ai_block}"
            f"\n"
            f"🔗 <a href='{link}'>Trade on Kalshi</a>\n"
            f"🕐 {ts_str}"
        )
        print(f"🧪 DEBUG ALERT SENT for {ticker} ({total_trades} trades, {total_contracts} contracts)")

        self._send_internal(msg)
        self.debug_sent_last_min += 1

        # Reset bucket after sending
        del self._agg_buckets[ticker]
