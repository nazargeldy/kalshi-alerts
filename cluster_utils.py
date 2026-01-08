def infer_cluster_key(ticker: str) -> str:
    t = ticker.lower()
    if "trump" in t or "biden" in t or "president" in t:
        return "presidency"
    if "house" in t or "senate" in t or "congress" in t:
        return "congress"
    if "supreme" in t or "scotus" in t:
        return "scotus"
    if "election" in t or "primary" in t:
        return "elections"
    if "rate" in t or "fed" in t or "cut" in t:
        return "rates"
    if "nba" in t or "basketball" in t:
        return "nba"
    return "other"
