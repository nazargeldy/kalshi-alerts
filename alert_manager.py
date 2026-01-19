import os
import time
from collections import defaultdict

class AlertManager:
    def __init__(self, alerter, daily_cap=20, ticker_map=None):
        self.alerter = alerter
        self.daily_cap = daily_cap
        self.ticker_map = ticker_map or {}
        self.alerts_sent_today = 0
        
        # Cooldown tracking
        self.market_last_alert = defaultdict(float) # ticker -> timestamp
        self.cluster_last_alert = defaultdict(float) # cluster_key -> timestamp
        
        # Production Config
        self.MARKET_COOLDOWN = 600  # 10 mins
        self.CLUSTER_COOLDOWN = 300 # 5 mins

        # Debug Config
        self.alert_mode = os.getenv("ALERT_MODE", "prod").lower()
        self.debug_sample_every = int(os.getenv("DEBUG_SAMPLE_EVERY", "20"))
        self.debug_min_contracts = int(os.getenv("DEBUG_MIN_CONTRACTS", "200"))
        self.debug_max_per_min = int(os.getenv("DEBUG_MAX_PER_MIN", "3"))

        # Debug State
        self.debug_trades_seen = 0
        self.debug_sent_last_min = 0
        self.debug_window_start = 0

    def can_send(self) -> bool:
        return self.alerts_sent_today < self.daily_cap

    def _send_internal(self, msg: str):
        if not self.can_send():
            print("⚠️ Daily alert cap reached. Suppressing.")
            return

        success = self.alerter.send(msg)
        if success:
            self.alerts_sent_today += 1

    def process_solo_alert(self, ticker: str, score: float, reasons: list):
        # Production Rule: Score >= 85
        if score < 85:
            return

        now = time.time()
        # Rule: Market Cooldown
        if now - self.market_last_alert[ticker] < self.MARKET_COOLDOWN:
            return

        title = self.ticker_map.get(ticker, ticker)
        msg = (
            f"🚨 <b>SOLO EXTREME {score}</b>\n"
            f"<a href='https://kalshi.com/markets/{ticker}'>{title}</a>\n"
            f"Reasons: {', '.join(reasons)}"
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

        tier_label = "TIER 1" if is_tier_1 else "TIER 2"
        
        market_lines = []
        for t in markets:
            title = self.ticker_map.get(t, t)
            market_lines.append(f"- {title}")

        msg = (
            f"🔥 <b>CLUSTER {cluster_key} ({tier_label})</b>\n"
            f"Count: {count} mkts\n"
            f"Max Score: {max_score}\n"
            f"Tickers:\n{chr(10).join(market_lines)}"
        )
        print(f"🔥 CLUSTER ALERT SENT for {cluster_key}")
        self._send_internal(msg)
        self.cluster_last_alert[cluster_key] = now

    def process_debug_trade(self, ticker: str, yes_price: int, contracts: int, volume_proxy: float, score: float, reasons: list, ts_str: str):
        if self.alert_mode != "debug":
            return

        self.debug_trades_seen += 1
        
        # Rate Limit Window Reset
        now = time.time()
        if now - self.debug_window_start > 60:
            self.debug_window_start = now
            self.debug_sent_last_min = 0

        # Hard Cap Check
        if self.debug_sent_last_min >= self.debug_max_per_min:
            return

        # Criteria Check: Sampling OR Size
        is_sample = (self.debug_trades_seen % self.debug_sample_every == 0)
        is_large = (contracts >= self.debug_min_contracts)

        if is_sample or is_large:
            title = self.ticker_map.get(ticker, ticker)
            msg = (
                f"🧪 <b>TRADE (debug)</b>\n"
                f"{title}\n"
                f"YES: {yes_price}¢ ({yes_price/100:.2f})\n"
                f"Contracts: {contracts}\n"
                f"ProxyV: {volume_proxy:.0f}\n"
                f"Score: {score} | {', '.join(reasons)}\n"
                f"Time: {ts_str}"
            )
            print(f"🧪 DEBUG ALERT SENT for {ticker}")
            
            # Send directly via alerter to bypass daily cap if desired, 
            # OR use _send_internal to respect global safety.
            # Using _send_internal is safer to prevent blowing up quota.
            self._send_internal(msg)
            
            self.debug_sent_last_min += 1
