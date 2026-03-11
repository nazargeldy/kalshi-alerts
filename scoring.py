from typing import Dict, List, Optional, Any, Union

def score_trade(
    volume_proxy: float,
    baselines: Dict[str, Any],
    hours_to_close: Optional[float] = None,
    yes_price_cents: int = 50,
) -> Dict[str, Any]:
    """
    Score a trade 0-100 for anomaly/insider-like activity.
    
    Components:
      1) Size shock (z-score vs 24h median/MAD)       — up to 40 pts
      2) Burst (1m count vs 60m average)               — up to 25 pts
      3) Short-dated (hours until market close)         — up to 20 pts
      4) Absolute size gate                             — up to 20 pts
      5) Price momentum (5m / 60m price delta)          — up to 20 pts
      6) Conviction (asymmetric price = high risk/reward) — up to 15 pts
    
    Max theoretical raw = 140; capped to 100.
    """
    score = 0
    reasons = []

    median = baselines.get("median_24h")
    mad = baselines.get("mad_24h")

    # 1) Size shock
    if median is not None and mad is not None and mad > 0:
        z = (volume_proxy - median) / (1.4826 * mad + 1)
        if z >= 8:
            score += 40
            reasons.append(f"Trade size {z:.1f}x above normal")
        elif z >= 5:
            score += 25
            reasons.append(f"Trade size {z:.1f}x above normal")
        elif z >= 3:
            score += 10
            reasons.append(f"Trade size {z:.1f}x above normal")

    # 2) Burst
    t1 = baselines.get("trades_1m", 0)
    t60 = baselines.get("trades_60m", 0)
    avg_per_min = max(t60 / 60, 1)
    burst = t1 / avg_per_min

    if burst >= 10:
        score += 25
        reasons.append(f"{burst:.0f}x spike in trade frequency")
    elif burst >= 6:
        score += 18
        reasons.append(f"{burst:.0f}x spike in trade frequency")
    elif burst >= 3:
        score += 10
        reasons.append(f"{burst:.0f}x spike in trade frequency")

    # 3) Short-dated
    if hours_to_close is not None:
        if hours_to_close <= 24:
            score += 20
            reasons.append("Closes within 24 hours")
        elif hours_to_close <= 72:
            score += 12
            reasons.append("Closes within 3 days")
        elif hours_to_close <= 168:
            score += 6
            reasons.append("Closes within 7 days")

    # 4) Absolute size gate
    if volume_proxy >= 250_000:
        score += 20
        reasons.append("Very large trade (volume > $2,500)")
    elif volume_proxy >= 100_000:
        score += 15
        reasons.append("Large trade (volume > $1,000)")

    # 5) Price momentum — large price move coinciding with volume = informed flow
    pd5 = baselines.get("price_delta_5m")
    pd60 = baselines.get("price_delta_60m")
    # Use the larger absolute delta
    best_delta = 0
    delta_window = ""
    if pd5 is not None and abs(pd5) > best_delta:
        best_delta = abs(pd5)
        delta_window = "5m"
    if pd60 is not None and abs(pd60) > best_delta:
        best_delta = abs(pd60)
        delta_window = "60m"

    if best_delta >= 25:  # 25+ cent move = massive
        score += 20
        reasons.append(f"Price shifted {best_delta:+d}¢ in {delta_window}")
    elif best_delta >= 15:  # 15+ cent move
        score += 14
        reasons.append(f"Price shifted {best_delta:+d}¢ in {delta_window}")
    elif best_delta >= 8:   # 8+ cent move
        score += 8
        reasons.append(f"Price shifted {best_delta:+d}¢ in {delta_window}")

    # 6) Conviction — buying at extreme odds suggests confidence
    # yes_price near 5-15¢ (long-shot YES) or 85-95¢ (long-shot NO) = high asymmetry
    if yes_price_cents <= 15 or yes_price_cents >= 85:
        score += 15
        side = "YES" if yes_price_cents <= 15 else "NO"
        reasons.append(f"Most likely: {side} (price {yes_price_cents}¢)")
    elif yes_price_cents <= 25 or yes_price_cents >= 75:
        score += 8
        side = "YES" if yes_price_cents <= 50 else "NO"
        reasons.append(f"Most likely: {side} (price {yes_price_cents}¢)")

    return {
        "score": min(score, 100),
        "reasons": reasons,
    }
