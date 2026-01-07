from scoring import score_trade
from baselines import MarketBaselines

def test_scoring_logic():
    print("Testing Scoring Engine...")
    
    # Setup a baseline with known stats
    # Let's say we have a steady stream of small trades: volume ~1000
    baseline_state = {
        "trades_1m": 2,
        "trades_60m": 120, # avg 2 per min
        "median_24h": 1000.0,
        "mad_24h": 100.0   # small deviation
    }
    
    # 1. Normal trade (Vol=1100) -> z=(1100-1000)/(148.26+1) ~= 0.6
    # Burst = 3 (1m has 2 trades + this one = 3) / avg(2) = 1.5x -> 0 pts
    # Score should be 0
    res = score_trade(1100, baseline_state, hours_to_close=None)
    print(f"Normal trade: {res}")
    assert res["score"] == 0
    
    # 2. Size Shock (Vol=5000) -> z=(5000-1000)/149 ~= 26.8 -> >8 -> +40 pts
    # Abs size < 100k -> 0
    # Burst 1.5x -> 0
    # Score 40
    res = score_trade(5000, baseline_state, hours_to_close=None)
    print(f"Shock trade: {res}")
    assert res["score"] == 40
    assert "size_z" in res["reasons"][0]
    
    # 3. Burst Shock
    # trades_1m = 25 (sudden spike)
    # trades_60m = 60 (avg 1/min)
    burst_baseline = {
        "trades_1m": 25, 
        "trades_60m": 60, 
        "median_24h": 1000.0,
        "mad_24h": 500.0
    }
    # Trade vol 1000 (normal)
    # Burst ratio: 25 / 1 = 25x -> >10 -> +25 pts
    res = score_trade(1000, burst_baseline, hours_to_close=None)
    print(f"Burst trade: {res}")
    assert res["score"] == 25
    assert "burst" in res["reasons"][0]

    # 4. Abs Size Gate
    res = score_trade(300_000, baseline_state, hours_to_close=None)
    print(f"Whale trade: {res}")
    # Size z score will be HUGE -> 40 pts
    # Abs size >= 250k -> 20 pts
    # Total 60
    assert res["score"] >= 60
    print("✅ Scoring logic verified.")

if __name__ == "__main__":
    test_scoring_logic()
