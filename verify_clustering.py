import time
from clustering import MarketClusterTracker
from cluster_utils import infer_cluster_key

def test_clustering_logic():
    print("Testing Clustering Logic...")
    
    # 1. Test Inference
    assert infer_cluster_key("Will Trump win?") == "presidency"
    assert infer_cluster_key("Who will control the House?") == "congress"
    assert infer_cluster_key("Random Event") == "other"
    print("✅ Key Inference Passed")

    # 2. Test Tracker
    # Window = 2 seconds for fast test
    tracker = MarketClusterTracker(window_seconds=2)
    
    # Event A (Time 0)
    info = tracker.add_event("presidency", "TRUMP-MARKET", 70)
    print("After Event A:", info)
    assert info["count"] == 1
    
    # Event B (Time 0.1) -> SAME cluster, DIFF market
    time.sleep(0.1)
    info = tracker.add_event("presidency", "BIDEN-MARKET", 80)
    print("After Event B:", info)
    assert info["count"] == 2
    assert info["max_score"] == 80
    
    # Event C (Time 0.2) -> DIFF cluster
    time.sleep(0.1)
    info = tracker.add_event("scotus", "JUSTICE-MARKET", 90)
    print("After Event C (SCOTUS):", info)
    assert info["count"] == 1 # New cluster start
    
    # Wait for decay (2.5s > 2s window)
    print("Waiting for decay...")
    time.sleep(2.5)
    
    # Event D (Time ~2.7) -> Should be alone
    info = tracker.add_event("presidency", "TRUMP-MARKET-2", 65)
    print("After Event D (Decayed):", info)
    assert info["count"] == 1 # Previous A & B expired
    
    print("✅ Clustering Logic Verified")

if __name__ == "__main__":
    test_clustering_logic()
