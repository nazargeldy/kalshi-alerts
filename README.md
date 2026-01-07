# Kalshi Unusual Flow Monitor

This project monitors the Kalshi prediction market WebSocket feed for statistically unusual trading activity.

## Features

- **Real-time Ingestion**: Connects to Kalshi's v2 WebSocket.
- **Persistence**: Stores all trades in a local SQLite database (`kalshi_trades.db`) with deduplication.
- **Baseline Stats**: Maintains in-memory rolling windows (1m, 5m, 60m, 24h) for every ticker.
- **Scoring Engine**: Calculates a "Unusual Score" (0-100) for every trade based on:
  - Size Shock (Z-Score vs 24h Median/MAD)
  - Burst Detection (Rate vs 60m Average)
  - Absolute Size Whales

## Setup

1. **Environment Variables**:
   Create a `.env` file or set:
   ```bash
   export KALSHI_ENV="prod"
   export KALSHI_KEY_ID="your_key_id"
   export KALSHI_PRIVATE_KEY_PATH="path/to/key.pem"
   ```

2. **Run**:
   ```bash
   python monitor.py
   ```

## Files

- `monitor.py`: Main entry point. Loops, connects, and orchestrates modules.
- `storage.py`: SQLite abstraction.
- `baselines.py`: Rolling statistics logic.
- `scoring.py`: The anomaly detection algorithm.
- `verify_*.py`: Unit tests and verification scripts.
