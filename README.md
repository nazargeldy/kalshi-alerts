# Kalshi Unusual Flow Monitor

A Python-based surveillance system for [Kalshi](https://kalshi.com) election markets. It detects unusual trading activity (high volume spikes relative to recent history) and alerts via Telegram.

##  Features

- **Real-time Monitoring**: Connects to Kalshi WebSocket API ('trades' channel).
- **Anomaly Detection**: Calculates a dynamic baseline for each market and scores new trades based on deviation.
- **Clustering**: Identifies when multiple correlated markets (e.g., "Democrat Senate" + "Harris Win") move together.
- **Smart Alerting**:
  - **Solo Alert**: Single market anomaly (Score >= 85).
  - **Cluster Alert**: 2+ markets spiking simultaneously (Max Score >= 70).
  - **Rate Limiting**: Daily cap (20 alerts/day) and cooldowns (10m per market) to prevent spam.

##  Setup

### Prerequisites
- Python 3.9+
- A Kalshi account (API Key & Private Key)
- A Telegram Bot (Token & Chat ID)

### Installation

1. **Clone the repo** (or unzip):
   \\\ash
   cd kalshi_alerts
   \\\

2. **Create Virtual Environment**:
   \\\ash
   python -m venv .venv
   .venv\Scripts\activate   # Windows
   \\\

3. **Install Dependencies**:
   \\\ash
   pip install -r requirements.txt
   \\\

4. **Configuration**:
   Create a '.env' file in the root directory:
   \\\ini
   KALSHI_ENV=prod
   KALSHI_KEY_ID=your_key_id_here
   KALSHI_PRIVATE_KEY_PATH=kalshi.key
   
   TELEGRAM_BOT_TOKEN=123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
   TELEGRAM_CHAT_ID=123456789
   \\\

##  Run Locally

To start the monitor in your terminal:

\\\ash
python monitor.py
\\\

You should see:
- ' Connected WS...'
- ' Watching for trades...'
- A confirmation message sent to your Telegram.

##  Run 24/7 (Windows Laptop)

To ensure the bot runs automatically on startup and persists through crashes:

1. **Use the Batch Script**:
   We've included 'run_monitor.bat'. It activates the environment, logs output to 'logs/', and restarts the script if it crashes.

2. **Task Scheduler Setup**:
   1. Open **Task Scheduler**.
   2. Click **Create Task**.
   3. **General**: Name "KalshiMonitor". Select "Run whether user is logged on or not" and "Run with highest privileges".
   4. **Triggers**: Click New -> "At startup".
   5. **Actions**: Click New -> Start a program -> Browse to 'c:\Users\geldy\kalshi_alerts\run_monitor.bat'.
   6. **Settings**: Check "If the task fails, restart every: 1 minute".

3. **Power Settings**:
   Ensure your laptop does not sleep when plugged in:
   - *Settings > System > Power > Screen and sleep* -> **When plugged in, put my device to sleep after: Never**.

##  Alert Logic

- **Score Calculation**: Based on volume relative to a 5-minute moving average.
- **Thresholds**:
  - **Solo**: Score >= 85.
  - **Cluster**: >= 2 markets with Max Score >= 70 within 5 minutes.
- **Safety**:
  - Max 20 alerts per day.
  - 10-minute cooldown per market (solo).
  - 5-minute cooldown per cluster.

##  Troubleshooting

- **No Alerts?**
  - Check 'logs/monitor.log' or 'logs/monitor.err'.
  - Verify your 'TELEGRAM_CHAT_ID'.
  - Ensure 'cluster_count' logic isn't filtering too aggressively.

- **WebSocket Disconnects?**
  - The script automatically reconnects with exponential backoff (1s... 60s).
  - Check your internet stability.

- **Telegram 401/404?**
  - 401: Invalid Bot Token.
  - 404: Bot hasn't messaged you before. Send '/start' to your bot in Telegram first.
