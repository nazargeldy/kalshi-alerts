import csv
import datetime
import logging
import os
import re
import time
from collections import defaultdict
from ai_reasoning import analyze_trade
from notion_logger import log_alert as notion_log

logger = logging.getLogger("kalshi_monitor")

CSV_FILE = "alerts_history.csv"
CSV_HEADERS = [
    "Timestamp",   # when the alert fired
    "Source",      # Kalshi | Polymarket
    "Market",      # clean title
    "Side to Buy", # which outcome to trade
    "Score /100",  # anomaly score
    "AI Lean",     # Leaning YES / Leaning NO
    "Reasons",     # pipe-separated flags
    "Link",        # direct market link
    "Result",      # WIN | LOSS | PUSH — fill manually after resolution
]


def _extract_ai_lean(analysis: str) -> str:
    if not analysis:
        return ""
    m = re.search(r"[Ll]eaning\s+(YES|NO|Up|Down|yes|no)", analysis)
    if m:
        return f"Leaning {m.group(1).upper()}"
    lower = analysis.lower()
    if "leaning yes" in lower:
        return "Leaning YES"
    if "leaning no" in lower:
        return "Leaning NO"
    return ""


def _is_polymarket_ticker(ticker: str) -> bool:
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

        self.market_last_alert = defaultdict(float)
        self.cluster_last_alert = defaultdict(float)

        self.MARKET_COOLDOWN = 3600  # 1 hour per market
        self.CLUSTER_COOLDOWN = 1800 # 30 min per cluster

    def _maybe_reset_daily_cap(self):
        today = datetime.date.today()
        if today != self._cap_date:
            self._cap_date = today
            self.alerts_sent_today = 0

    def can_send(self) -> bool:
        self._maybe_reset_daily_cap()
        return self.alerts_sent_today < self.daily_cap

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

    def _log(self, ticker, title, source, side, score, reasons, link, analysis="", ts_str=""):
        now_str = ts_str or datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lean = _extract_ai_lean(analysis)

        # CSV backup
        file_exists = os.path.isfile(CSV_FILE)
        try:
            with open(CSV_FILE, mode="a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                if not file_exists:
                    writer.writerow(CSV_HEADERS)
                writer.writerow([now_str, source, title, side,
                                  round(score, 1), lean, " | ".join(reasons), link, ""])
        except Exception as e:
            print(f"⚠️ CSV log failed: {e}")

        # Notion
        notion_log(ticker=ticker, title=title, source=source, alert_type="SOLO",
                   side=side, entry_price=0, contracts=0, score=score,
                   reasons=reasons, link=link, analysis=analysis, timestamp_str=now_str)

    def _build_footer(self, ticker: str, link: str) -> str:
        if _is_polymarket_ticker(ticker):
            return f"🔗 <a href='{link}'>Trade on Polymarket</a>"
        return f"🔗 <a href='{link}'>Trade on Kalshi</a>"

    def process_solo_alert(self, ticker: str, score: float, reasons: list, yes_price: int = 50, contracts: int = 0):
        if score < 50:
            return

        now = time.time()
        if now - self.market_last_alert[ticker] < self.MARKET_COOLDOWN:
            return

        title    = self.ticker_map.get(ticker, ticker[:40])
        no_price = 100 - yes_price
        link     = self.link_map.get(ticker, "https://kalshi.com/browse")
        yes_label, no_label = self.option_labels_map.get(ticker, ("✅ Yes", "❌ No"))
        source   = "Polymarket" if _is_polymarket_ticker(ticker) else "Kalshi"

        reason_lines = "".join(f"  • {r}\n" for r in reasons)
        analysis = analyze_trade(title, yes_label, no_label, yes_price, contracts, score, reasons, num_trades=1) or ""

        # AI SKIP gate — if Groq says this is junk, suppress the alert
        if analysis.startswith("SKIP:"):
            logger.info(f"AI-skipped [{source}] {title[:50]} — {analysis}")
            self.market_last_alert[ticker] = now  # still set cooldown so we don't retry
            return

        ai_block = f"\n🧠 <b>AI Analysis:</b>\n{analysis}\n" if analysis else ""

        # Determine side from AI lean; fallback to price-based direction
        lean = _extract_ai_lean(analysis)
        if lean == "Leaning YES":
            side = yes_label
        elif lean == "Leaning NO":
            side = no_label
        else:
            # Heuristic: if price moved up (positive delta implied by high price), follow momentum
            side = yes_label if yes_price <= 50 else no_label

        msg = (
            f"🚨 <b>Unusual Activity — {source}</b>\n"
            f"\n"
            f"❓ <b>{title}</b>\n"
            f"\n"
            f"1. {yes_label} — {yes_price}%\n"
            f"2. {no_label} — {no_price}%\n"
            f"\n"
            f"📌 <b>Side to watch:</b> {side}\n"
            f"⚡ Score: {score}/100\n"
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
            self._log(ticker, title, source, side, score, reasons, link, analysis)

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
            lead_title = self.ticker_map.get(lead, cluster_key[:40])
            lead_link  = self.link_map.get(lead, "https://kalshi.com/")
            self._log(lead, f"[CLUSTER] {lead_title}", source, "N/A",
                      max_score, [f"{count} correlated markets, tier={tier_label}"], lead_link)

        self.cluster_last_alert[cluster_key] = now

    def process_debug_trade(self, *args, **kwargs):
        pass  # suppressed — not sent to Telegram or Notion
