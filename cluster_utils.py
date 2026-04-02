def infer_cluster_key(ticker: str) -> str:
    """
    Derive a cluster key from a Kalshi ticker so that related markets
    (e.g. KXBTC-24DEC-B60000 and KXBTC-24DEC-B65000) group together.

    Strategy: use the series prefix — everything before the first '-'.
    This naturally groups KXBTC-*, INX-*, FED-*, PRES-*, etc.
    Falls back to the first 6 chars for tickers with no '-'.
    """
    t = ticker.upper()
    dash = t.find("-")
    if dash > 0:
        return t[:dash]   # e.g. "KXBTC", "INX", "FED", "PRES", "KXNFLSB"
    return t[:6] if len(t) >= 6 else t
