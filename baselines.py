import time
import statistics
from collections import deque
from typing import Dict, Deque, Tuple, Any

WINDOWS_MS = {
    "1m": 60_000,
    "5m": 5 * 60_000,
    "60m": 60 * 60_000,
    "24h": 24 * 60 * 60_000,
}

class MarketBaselines:
    def __init__(self):
        self.data: Dict[str, Dict] = {}

    def _init_market(self, ticker: str):
        self.data[ticker] = {
            "trades": {
                "1m": deque(),
                "5m": deque(),
                "60m": deque(),
                "24h": deque(),
            },
            # Price tracking: deque of (ts_ms, yes_price_cents)
            "prices_5m": deque(),
            "prices_60m": deque(),
            "median_24h": None,
            "mad_24h": None,
        }

    def update(
        self,
        market_ticker: str,
        ts_received_ms: int,
        contracts: int,
        yes_price_cents: int,
    ):
        if market_ticker not in self.data:
            self._init_market(market_ticker)

        volume_proxy = contracts * yes_price_cents
        now = ts_received_ms
        d = self.data[market_ticker]

        for window, dq in d["trades"].items():
            dq.append((now, volume_proxy))
            cutoff = now - WINDOWS_MS[window]
            while dq and dq[0][0] < cutoff:
                dq.popleft()

        # Track price history (5m and 60m windows)
        d["prices_5m"].append((now, yes_price_cents))
        while d["prices_5m"] and d["prices_5m"][0][0] < now - WINDOWS_MS["5m"]:
            d["prices_5m"].popleft()

        d["prices_60m"].append((now, yes_price_cents))
        while d["prices_60m"] and d["prices_60m"][0][0] < now - WINDOWS_MS["60m"]:
            d["prices_60m"].popleft()

        # Update 24h median + MAD
        vols_24h = [v for _, v in d["trades"]["24h"]]
        if len(vols_24h) >= 5:  # minimum data
            med = statistics.median(vols_24h)
            abs_dev = [abs(v - med) for v in vols_24h]
            mad = statistics.median(abs_dev)
            d["median_24h"] = med
            d["mad_24h"] = mad

    def snapshot(self, market_ticker: str) -> Dict[str, Any]:
        if market_ticker not in self.data:
            return {}

        d = self.data[market_ticker]

        # Price delta: difference between current price and oldest price in window
        price_delta_5m = None
        price_delta_60m = None
        if len(d["prices_5m"]) >= 2:
            oldest = d["prices_5m"][0][1]
            newest = d["prices_5m"][-1][1]
            price_delta_5m = newest - oldest  # in cents
        if len(d["prices_60m"]) >= 2:
            oldest = d["prices_60m"][0][1]
            newest = d["prices_60m"][-1][1]
            price_delta_60m = newest - oldest

        return {
            "trades_1m": len(d["trades"]["1m"]),
            "trades_5m": len(d["trades"]["5m"]),
            "trades_60m": len(d["trades"]["60m"]),
            "median_24h": d["median_24h"],
            "mad_24h": d["mad_24h"],
            "price_delta_5m": price_delta_5m,
            "price_delta_60m": price_delta_60m,
        }
