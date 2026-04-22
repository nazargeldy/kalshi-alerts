from typing import Dict, List, Optional, Any, Union

def score_trade(
    volume_proxy: float,
    baselines: Dict[str, Any],
    hours_to_close: Optional[float] = None,
    yes_price_cents: int = 50,
    contracts: int = 0,
) -> Dict[str, Any]:
    """
    Score a trade 0-100 for anomaly/insider-like activity.

    Components:
      0) Raw contract count (cold-start safe)         — up to 15 pts
      1) Size shock (z-score vs 24h median/MAD)       — up to 40 pts
      2) Burst (1m count vs 60m average)              — up to 25 pts
      3) Short-dated (hours until market close)        — up to 20 pts
      4) Absolute volume gate                          — up to 20 pts
      5) Price momentum (5m / 60m price delta)         — up to 20 pts
      6) Conviction (asymmetric price = high risk/reward) — up to 15 pts

    Max theoretical raw = 155; capped to 100.
    """
    score = 0
    reasons = []

    # 0) Raw contract count — works immediately, no baseline needed
    # volume_proxy = contracts * yes_price_cents, so derive contracts if not passed
    effective_contracts = contracts
    if effective_contracts <= 0 and yes_price_cents > 0:
        effective_contracts = int(volume_proxy / yes_price_cents)

    if effective_contracts >= 2000:
        score += 15
        reasons.append(f"Whale order: {effective_contracts:,} contracts")
    elif effective_contracts >= 750:
        score += 10
        reasons.append(f"Large order: {effective_contracts:,} contracts")
    elif effective_contracts >= 250:
        score += 5
        reasons.append(f"Notable order: {effective_contracts:,} contracts")

    # 1) Size shock (z-score vs 24h median/MAD)
    median = baselines.get("median_24h")
    mad = baselines.get("mad_24h")

    if median is not None and mad is not None and mad > 0:
        z = (volume_proxy - median) / (1.4826 * mad + 1)
        if z >= 8:
            score += 40
            reasons.append(f"Trade size {z:.1f}x above normal (z-score)")
        elif z >= 5:
            score += 25
            reasons.append(f"Trade size {z:.1f}x above normal (z-score)")
        elif z >= 3:
            score += 10
            reasons.append(f"Trade size {z:.1f}x above normal (z-score)")

    # 2) Burst (1m trade count vs 60m average rate)
    # Only score burst when we have real history (t60 >= 3); otherwise the
    # 0.5/min floor manufactures a fake spike on service restart.
    t1 = baselines.get("trades_1m", 0)
    t60 = baselines.get("trades_60m", 0)
    if t60 >= 3:
        avg_per_min = max(t60 / 60, 0.5)
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

    # 3) Short-dated urgency
    if hours_to_close is not None:
        if hours_to_close <= 2:
            score += 20
            reasons.append("Closes within 2 hours")
        elif hours_to_close <= 24:
            score += 15
            reasons.append("Closes within 24 hours")
        elif hours_to_close <= 72:
            score += 8
            reasons.append("Closes within 3 days")
        elif hours_to_close <= 168:
            score += 4
            reasons.append("Closes within 7 days")

    # 4) Absolute volume gate (volume_proxy = contracts * yes_price_cents)
    if volume_proxy >= 250_000:
        score += 20
        reasons.append(f"Very large trade volume (${volume_proxy/100:,.0f})")
    elif volume_proxy >= 100_000:
        score += 15
        reasons.append(f"Large trade volume (${volume_proxy/100:,.0f})")
    elif volume_proxy >= 30_000:
        score += 8
        reasons.append(f"Elevated trade volume (${volume_proxy/100:,.0f})")

    # 5) Price momentum — large price move coinciding with volume = informed flow
    pd5 = baselines.get("price_delta_5m")
    pd60 = baselines.get("price_delta_60m")
    best_delta = 0
    delta_window = ""
    if pd5 is not None and abs(pd5) > best_delta:
        best_delta = abs(pd5)
        delta_window = "5m"
    if pd60 is not None and abs(pd60) > best_delta:
        best_delta = abs(pd60)
        delta_window = "60m"

    if best_delta >= 25:
        score += 20
        reasons.append(f"Price moved {best_delta:+d}¢ in {delta_window}")
    elif best_delta >= 15:
        score += 14
        reasons.append(f"Price moved {best_delta:+d}¢ in {delta_window}")
    elif best_delta >= 8:
        score += 8
        reasons.append(f"Price moved {best_delta:+d}¢ in {delta_window}")

    # 6) Conviction — trading at extreme odds signals high confidence
    # Extreme: ≤10¢ or ≥90¢  |  Strong: ≤20¢ or ≥80¢  |  Skewed: ≤30¢ or ≥70¢
    if yes_price_cents <= 10 or yes_price_cents >= 90:
        score += 15
        side = "YES" if yes_price_cents <= 10 else "NO"
        reasons.append(f"Extreme conviction: {side} at {yes_price_cents}¢")
    elif yes_price_cents <= 20 or yes_price_cents >= 80:
        score += 10
        side = "YES" if yes_price_cents <= 50 else "NO"
        reasons.append(f"High conviction: {side} at {yes_price_cents}¢")
    elif yes_price_cents <= 30 or yes_price_cents >= 70:
        score += 5
        side = "YES" if yes_price_cents <= 50 else "NO"
        reasons.append(f"Skewed odds: {side} at {yes_price_cents}¢")

    # Gate: require at least one real anomaly signal (z-score, burst, or price
    # momentum) before the score can reach alert threshold. This prevents
    # cold-start false positives where only size + short-dated + conviction
    # stack up without any baseline comparison.
    has_real_signal = any(
        "z-score" in r or "spike in trade" in r or "Price moved" in r
        for r in reasons
    )
    final = min(score, 100)
    if not has_real_signal:
        final = min(final, 45)  # cap below alert threshold until baselines warm up

    return {
        "score": final,
        "reasons": reasons,
    }
