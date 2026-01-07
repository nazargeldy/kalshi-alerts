import json
import sqlite3
import time
from typing import Any, Dict, Optional


class TradeStore:
    def __init__(self, db_path: str = "kalshi_trades.db", env: str = "prod"):
        self.db_path = db_path
        self.env = env
        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA synchronous=NORMAL;")
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trades (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              market_ticker TEXT NOT NULL,
              yes_price_cents INTEGER,
              no_price_cents INTEGER,
              contracts INTEGER,
              ts_exchange INTEGER,
              ts_received_ms INTEGER NOT NULL,
              env TEXT NOT NULL,
              raw_json TEXT NOT NULL,
              UNIQUE(market_ticker, ts_exchange, yes_price_cents, no_price_cents, contracts)
            );
            """
        )
        self.conn.commit()

    def insert_trade(
        self,
        market_ticker: str,
        yes_price_cents: Optional[int],
        no_price_cents: Optional[int],
        contracts: Optional[int],
        ts_exchange: Optional[int],
        raw_msg: Dict[str, Any],
    ) -> None:
        ts_received_ms = int(time.time() * 1000)
        raw_json = json.dumps(raw_msg, separators=(",", ":"), ensure_ascii=False)

        self.conn.execute(
            """
            INSERT OR IGNORE INTO trades
            (market_ticker, yes_price_cents, no_price_cents, contracts, ts_exchange, ts_received_ms, env, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                market_ticker,
                yes_price_cents,
                no_price_cents,
                contracts,
                ts_exchange,
                ts_received_ms,
                self.env,
                raw_json,
            ),
        )
        if self.conn.total_changes > 0:
             # This is a bit rough since total_changes counts everything since connection open, 
             # but strictly after an INSERT it should increment.
             # Better is checking cursor.rowcount but execute() returns cursor in Python standard lib.
             pass
        return self.conn.total_changes

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        try:
            self.conn.commit()
        finally:
            self.conn.close()
