from storage import TradeStore
import time

def verify():
    store = TradeStore("kalshi_trades.db")
    
    # 1. Check initial count
    initial_count = store.cur.execute("SELECT count(*) FROM trades").fetchone()[0]
    print(f"Initial count: {initial_count}")
    
    # 2. Insert a dummy trade
    # market_ticker, ts, yes, no, contracts
    dummy_ticker = "TEST-DEDUPE-MARKET"
    dummy_ts = int(time.time())
    
    print(f"Inserting dummy trade: {dummy_ticker} at {dummy_ts}")
    store.insert_trade({
        "market_ticker": dummy_ticker,
        "ts": dummy_ts,
        "yes_price": 50,
        "no_price": 50,
        "count": 10
    })
    store.commit()
    
    after_first = store.cur.execute("SELECT count(*) FROM trades").fetchone()[0]
    print(f"Count after 1st insert: {after_first}")
    
    if after_first != initial_count + 1:
        print("ERROR: First insert failed to increase count!")
    else:
        print("SUCCESS: First insert worked.")
        
    # 3. Insert SAME dummy trade
    print("Inserting DUPLICATE dummy trade...")
    store.insert_trade({
        "market_ticker": dummy_ticker,
        "ts": dummy_ts, # MATCHING TS, TICKER, PRICE, COUNT
        "yes_price": 50,
        "no_price": 50,
        "count": 10
    })
    store.commit()
    
    after_second = store.cur.execute("SELECT count(*) FROM trades").fetchone()[0]
    print(f"Count after 2nd insert: {after_second}")
    
    if after_second == after_first:
        print("SUCCESS: Dedupe worked (count did not increase).")
        # Cleanup
        store.cur.execute("DELETE FROM trades WHERE market_ticker = ?", (dummy_ticker,))
        store.commit()
        print("Cleanup done.")
    else:
        print("FAILURE: Count increased! Dedupe broken.")

if __name__ == "__main__":
    verify()
