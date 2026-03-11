"""Quick script to list the non-sports markets we're monitoring."""
from monitor import (
    load_private_key, fetch_open_markets, pick_target_tickers,
    PRIVATE_KEY_PATH, SUBSCRIBE_TICKER_LIMIT
)

pk = load_private_key(PRIVATE_KEY_PATH)
markets = fetch_open_markets(pk)
markets.sort(key=lambda m: int(m.get("volume") or 0), reverse=True)
tickers = pick_target_tickers(markets)[:SUBSCRIBE_TICKER_LIMIT]

print(f"\n=== {len(tickers)} non-sports markets (out of {len(markets)} total) ===\n")
for t in tickers:
    for m in markets:
        if m.get("ticker") == t:
            title = (m.get("title") or "")[:90]
            vol = m.get("volume", 0)
            et = m.get("event_ticker", "")
            print(f"  {t} | vol={vol:,} | {title}")
            break
