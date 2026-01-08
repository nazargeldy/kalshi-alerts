from collections import defaultdict, deque
import time
from typing import Dict, List, Any

class MarketClusterTracker:
    def __init__(self, window_seconds=300):
        self.window = window_seconds
        self.events = defaultdict(deque)  # cluster_key -> deque[(ts, ticker, score)]

    def _prune(self, dq: deque, now: float):
        cutoff = now - self.window
        while dq and dq[0][0] < cutoff:
            dq.popleft()

    def add_event(self, cluster_key: str, ticker: str, score: float) -> Dict[str, Any]:
        now = time.time()
        dq = self.events[cluster_key]
        dq.append((now, ticker, score))
        self._prune(dq, now)

        unique_markets = {t for _, t, _ in dq}
        scores = [s for _, _, s in dq]
        
        return {
            "count": len(unique_markets),
            "markets": list(unique_markets),
            "max_score": max(scores) if scores else 0,
        }
