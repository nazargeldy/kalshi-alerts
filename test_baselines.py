import time
from baselines import MarketBaselines

def test_baselines():
    print("Testing Baselines...")
    bl = MarketBaselines()
    ticker = "TEST-TICKER"
    
    # 1. Add 4 trades (not enough for median stats which requires 5)
    now = int(time.time() * 1000)
    for i in range(4):
        bl.update(ticker, now, contracts=10, yes_price_cents=50) # volume = 500
        
    snap = bl.snapshot(ticker)
    print("Snapshot after 4 trades:", snap)
    assert snap['trades_1m'] == 4
    assert snap['median_24h'] is None
    
    # 2. Add 5th trade -> should trigger stats
    bl.update(ticker, now, contracts=10, yes_price_cents=50) # volume = 500
    snap = bl.snapshot(ticker)
    print("Snapshot after 5 trades:", snap)
    assert snap['median_24h'] == 500.0
    assert snap['mad_24h'] == 0.0 # All 500, deviation 0
    
    # 3. Add outlier
    bl.update(ticker, now, contracts=100, yes_price_cents=50) # volume = 5000
    # Data: [500, 500, 500, 500, 500, 5000]
    # Sorted: 500, 500, 500, 500, 500, 5000
    # Median of 6 elements: Average of 3rd and 4th (500, 500) -> 500
    # Deviations: 0, 0, 0, 0, 0, 4500
    # MAD of Deviations: Median of (0,0,0,0,0,4500) -> 0
    
    snap = bl.snapshot(ticker)
    print("Snapshot after outlier:", snap)
    assert snap['median_24h'] == 500.0
    
    # 4. Window rolling
    # Advance time by 61 minutes
    future = now + (61 * 60 * 1000)
    bl.update(ticker, future, contracts=1, yes_price_cents=1)
    
    snap = bl.snapshot(ticker)
    print("Snapshot after 61m:", snap)
    # The previous 6 trades are > 60m old, so trades_60m should be 1 (the new one)
    # trades_24h should have all 7
    assert snap['trades_1m'] == 1
    assert snap['trades_5m'] == 1
    assert snap['trades_60m'] == 1
    assert len(bl.data[ticker]['trades']['24h']) == 7
    
    print("\n✅ All Baseline tests passed!")

if __name__ == "__main__":
    test_baselines()
