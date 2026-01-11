import time
from collections import defaultdict

class AlertManager:
    def __init__(self, alerter, daily_cap=20):
        self.alerter = alerter
        self.daily_cap = daily_cap
        self.alerts_sent_today = 0
        
        # Cooldown tracking
        self.market_last_alert = defaultdict(float) # ticker -> timestamp
        self.cluster_last_alert = defaultdict(float) # cluster_key -> timestamp
        
        # Conf
        self.MARKET_COOLDOWN = 600  # 10 mins
        self.CLUSTER_COOLDOWN = 300 # 5 mins

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
        # Rule: TEMPORARY TEST - ALLOW ALL SCORES
        if score < 1:
            return

        now = time.time()
        # Rule: Market Cooldown
        if now - self.market_last_alert[ticker] < self.MARKET_COOLDOWN:
            return

        msg = (
            f"🚨 <b>SOLO EXTREME {score}</b>\n"
            f"{ticker}\n"
            f"Reasons: {', '.join(reasons)}"
        )
        print(f"🚨 SOLO ALERT SENT for {ticker}")
        self._send_internal(msg)
        self.market_last_alert[ticker] = now

    def process_cluster_alert(self, cluster_key: str, count: int, max_score: float, markets: list):
        # Rule: Cluster confirmed (count >= 2) AND max_score >= 70
        if count < 2 or max_score < 70:
            return

        now = time.time()
        # Rule: Cluster Cooldown
        if now - self.cluster_last_alert[cluster_key] < self.CLUSTER_COOLDOWN:
            return

        msg = (
            f"🔥 <b>CLUSTER {cluster_key}</b>\n"
            f"Count: {count} mkts\n"
            f"Max Score: {max_score}\n"
            f"Tickers: {', '.join(markets)}"
        )
        print(f"🔥 CLUSTER ALERT SENT for {cluster_key}")
        self._send_internal(msg)
        self.cluster_last_alert[cluster_key] = now
