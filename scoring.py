from typing import Dict, List, Optional, Any, Union

def score_trade(
    volume_proxy: float,
    baselines: Dict[str, Any],
    hours_to_close: Optional[float] = None,
) -> Dict[str, Any]:
    score = 0
    reasons = []

    median = baselines.get("median_24h")
    mad = baselines.get("mad_24h")

    # 1) Size shock
    if median is not None and mad is not None and mad > 0:
        z = (volume_proxy - median) / (1.4826 * mad + 1)
        if z >= 8:
            score += 40
            reasons.append(f"size_z={z:.1f}")
        elif z >= 5:
            score += 25
            reasons.append(f"size_z={z:.1f}")
        elif z >= 3:
            score += 10
            reasons.append(f"size_z={z:.1f}")

    # 2) Burst
    t1 = baselines.get("trades_1m", 0)
    t60 = baselines.get("trades_60m", 0)
    avg_per_min = max(t60 / 60, 1)
    burst = t1 / avg_per_min

    if burst >= 10:
        score += 25
        reasons.append(f"burst={burst:.1f}x")
    elif burst >= 6:
        score += 18
        reasons.append(f"burst={burst:.1f}x")
    elif burst >= 3:
        score += 10
        reasons.append(f"burst={burst:.1f}x")

    # 3) Short-dated
    if hours_to_close is not None:
        if hours_to_close <= 24:
            score += 20
            reasons.append("short_dated<=24h")
        elif hours_to_close <= 72:
            score += 12
            reasons.append("short_dated<=72h")
        elif hours_to_close <= 168:
            score += 6
            reasons.append("short_dated<=7d")

    # 4) Absolute size gate
    if volume_proxy >= 250_000:
        score += 20
        reasons.append("abs_size>=250k")
    elif volume_proxy >= 100_000:
        score += 15
        reasons.append("abs_size>=100k")

    return {
        "score": min(score, 100),
        "reasons": reasons,
    }
