import os
import requests
import asyncio

class Alerter:
    def __init__(self):
        self.token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID")
        # Ensure we have a valid token URL format if token exists
        if self.token:
            self.base_url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        else:
            self.base_url = None

    def send(self, message: str) -> bool:
        """
        Sends a synchronous alert. returns True if success, False otherwise.
        """
        if not self.base_url or not self.chat_id:
            # Silent fail or log locally if configured.
            # print(f"DEBUG: Alert suppressed (no env vars): {message}")
            return False

        payload = {
            "chat_id": self.chat_id,
            "text": message,
            "parse_mode": "HTML"
        }
        
        try:
            resp = requests.post(self.base_url, json=payload, timeout=3)
            if resp.status_code == 200:
                print("✅ Telegram alert sent.")
                return True
            else:
                print(f"⚠️ Telegram failed: {resp.status_code} {resp.text}")
                return False
        except Exception as e:
            print(f"⚠️ Telegram exception: {e}")
            return False
