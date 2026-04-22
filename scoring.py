from typing import Dict, List, Optional, Any


def score_trade(
    volume_proxy: float,
    baselines: Dict[str, Any],
    hours_to_close: Optional[float] = None,
    yes_price_cents: int = 50,
    contracts: int = 0,
    vpin: float = 0.0,
    wallet_score: int = 0,
    wallet_reason: str = "",
) -> Dict[str, Any]:
    """
    Score a trade 0-100 for anomaly/insider-like activity.

    Anomaly signals (tracked for 3-signal gate):
      A) Size shock   — z-score vs 24h baseline
      B) Burst        — 1m trade frequency spike
      C) Momentum     — price delta in 5m or 60m window
      D) VPIN         — order-flow imbalance (one-sided buying ≥ 0.65)
      E) Wallet       — fresh whale or known high-win-rate actor

    Context signals (don't count toward 3-signal gate):
      F) Short-dated  — hours until resolution
      G) Volume gate  — absolute dollar volume
      H) Conviction   — asymmetric yes_price

    Gate rule: score is capped at 45 (below alert threshold) unless
    at least 2 of {A,B,C,D,E} trigger. At 3+ the full score is allowed.
    This prevents cold-start false positives from F+G+H stacking alone.
    """
    score = 0
    reasons: List[str] = []

    # Track which anomaly signal categories fired
    anomaly_hits = 0  # incremented for A–E

    # ── H) Conviction (evaluated early for cold-start contract scoring) ────────
    # Separate from the main conviction block below — used only for context.

    # ── Raw contract count (cold-start safe, not an anomaly signal) ───────────
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

    # ── A) Size shock (z-score vs 24h median/MAD) ─────────────────────────────
    median = baselines.get("median_24h")
    mad    = baselines.get("mad_24h")
    if median is not None and mad is not None and mad > 0:
        z = (volume_proxy - median) / (1.4826 * mad + 1)
        if z >= 8:
            score += 40
            reasons.append(f"Trade size {z:.1f}x above normal (z-score)")
            anomaly_hits += 1
        elif z >= 5:
            score += 25
            reasons.append(f"Trade size {z:.1f}x above normal (z-score)")
            anomaly_hits += 1
        elif z >= 3:
            score += 10
            reasons.append(f"Trade size {z:.1f}x above normal (z-score)")
            anomaly_hits += 1

    # ── B) Burst (1m trade count vs 60m average) ──────────────────────────────
    # Only when we have real history — fake floor causes startup false positives
    t1  = baselines.get("trades_1m", 0)
    t60 = baselines.get("trades_60m", 0)
    if t60 >= 3:
        avg_per_min = max(t60 / 60, 0.5)
        burst = t1 / avg_per_min
        if burst >= 10:
            score += 25
            reasons.append(f"{burst:.0f}x spike in trade frequency")
            anomaly_hits += 1
        elif burst >= 6:
            score += 18
            reasons.append(f"{burst:.0f}x spike in trade frequency")
            anomaly_hits += 1
        elif burst >= 3:
            score += 10
            reasons.append(f"{burst:.0f}x spike in trade frequency")
            anomaly_hits += 1

    # ── F) Short-dated urgency ─────────────────────────────────────────────────
    if hours_to_close is not None:
        if hours_to_close <= 0.167:        # ≤ 10 minutes — last-minute rush
            score += 30
            reasons.append("Resolves in <10 minutes — last-minute rush")
        elif hours_to_close <= 2:
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

    # ── G) Absolute volume gate ────────────────────────────────────────────────
    if volume_proxy >= 250_000:
        score += 20
        reasons.append(f"Very large trade volume (${volume_proxy/100:,.0f})")
    elif volume_proxy >= 100_000:
        score += 15
        reasons.append(f"Large trade volume (${volume_proxy/100:,.0f})")
    elif volume_proxy >= 30_000:
        score += 8
        reasons.append(f"Elevated trade volume (${volume_proxy/100:,.0f})")

    # ── C) Price momentum ─────────────────────────────────────────────────────
    pd5  = baselines.get("price_delta_5m")
    pd60 = baselines.get("price_delta_60m")
    best_delta  = 0
    delta_window = ""
    if pd5  is not None and abs(pd5)  > best_delta:
        best_delta   = abs(pd5)
        delta_window = "5m"
    if pd60 is not None and abs(pd60) > best_delta:
        best_delta   = abs(pd60)
        delta_window = "60m"

    if best_delta >= 25:
        score += 20
        reasons.append(f"Price moved {best_delta:+d}¢ in {delta_window}")
        anomaly_hits += 1
    elif best_delta >= 15:
        score += 14
        reasons.append(f"Price moved {best_delta:+d}¢ in {delta_window}")
        anomaly_hits += 1
    elif best_delta >= 8:
        score += 8
        reasons.append(f"Price moved {best_delta:+d}¢ in {delta_window}")
        # partial — don't count as a full anomaly hit

    # ── D) VPIN — order-flow imbalance ────────────────────────────────────────
    if vpin >= 0.85:
        score += 20
        reasons.append(f"Extreme order-flow imbalance (VPIN={vpin:.2f}) — one-sided buying")
        anomaly_hits += 1
    elif vpin >= 0.70:
        score += 12
        reasons.append(f"High order-flow imbalance (VPIN={vpin:.2f})")
        anomaly_hits += 1
    elif vpin >= 0.60:
        score += 6
        reasons.append(f"Elevated order-flow imbalance (VPIN={vpin:.2f})")
        # partial

    # ── E) Wallet reputation ──────────────────────────────────────────────────
    if wallet_score > 0 and wallet_reason:
        score += wallet_score
        reasons.append(wallet_reason)
        if wallet_score >= 8:
            anomaly_hits += 1

    # ── H) Conviction ────────────────────────────────────────────────────────
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

    # ── 3-signal gate ─────────────────────────────────────────────────────────
    # Require at least 2 anomaly signals (A–E) before any alert fires;
    # 3+ lifts the cap entirely. This prevents pure "big+short+conviction"
    # cold-start stacks from crossing the 50-point alert threshold.
    raw = min(score, 100)
    if anomaly_hits < 2:
        final = min(raw, 45)   # hard block — no alert
    elif anomaly_hits == 2:
        final = min(raw, 72)   # allow moderate alerts
    else:
        final = raw             # 3+ signals: full score

    return {
        "score":         final,
        "reasons":       reasons,
        "anomaly_hits":  anomaly_hits,
    }
