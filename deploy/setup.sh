#!/bin/bash
# =============================================================
# Kalshi Monitor - Oracle Cloud Setup Script
# Run this on your Oracle instance after cloning the repo.
# Usage: bash deploy/setup.sh
# =============================================================
set -e

APP_DIR="/home/ubuntu/kalshi_alerts"
SERVICE_NAME="kalshi-monitor"

echo "=========================================="
echo " Kalshi Monitor - Oracle Cloud Setup"
echo "=========================================="

# 1. System dependencies
echo ""
echo "[1/6] Installing system packages..."
sudo apt-get update -qq
sudo apt-get install -y python3 python3-venv python3-pip git

# 2. Create venv & install deps
echo ""
echo "[2/6] Setting up Python virtual environment..."
cd "$APP_DIR"
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
echo "  ✅ Dependencies installed."

# 3. Check .env exists
echo ""
echo "[3/6] Checking configuration..."
if [ ! -f "$APP_DIR/.env" ]; then
    echo "  ❌ ERROR: .env file not found at $APP_DIR/.env"
    echo "  Copy your .env file with:"
    echo "    scp .env ubuntu@<YOUR_IP>:$APP_DIR/.env"
    exit 1
fi
echo "  ✅ .env found."

# 4. Check private key exists
KEY_PATH=$(grep KALSHI_PRIVATE_KEY_PATH "$APP_DIR/.env" | cut -d= -f2 | tr -d ' "'"'"'')
if [ ! -f "$KEY_PATH" ]; then
    echo "  ❌ ERROR: Private key not found at $KEY_PATH"
    echo "  Copy your .pem key file to the server."
    exit 1
fi
echo "  ✅ Private key found at $KEY_PATH"

# 5. Install systemd service
echo ""
echo "[4/6] Installing systemd service..."
sudo cp "$APP_DIR/deploy/$SERVICE_NAME.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
echo "  ✅ Service installed and enabled (will auto-start on boot)."

# 6. Create logs directory
echo ""
echo "[5/6] Creating logs directory..."
mkdir -p "$APP_DIR/logs"
echo "  ✅ Logs directory ready."

# 7. Start!
echo ""
echo "[6/6] Starting service..."
sudo systemctl start "$SERVICE_NAME"
sleep 2

if sudo systemctl is-active --quiet "$SERVICE_NAME"; then
    echo "  ✅ Kalshi Monitor is RUNNING!"
else
    echo "  ⚠️  Service may have failed. Check logs:"
    echo "    sudo journalctl -u $SERVICE_NAME -n 30 --no-pager"
fi

echo ""
echo "=========================================="
echo " DONE! Useful commands:"
echo "=========================================="
echo ""
echo "  Status:    sudo systemctl status $SERVICE_NAME"
echo "  Logs:      sudo journalctl -u $SERVICE_NAME -f"
echo "  Stop:      sudo systemctl stop $SERVICE_NAME"
echo "  Restart:   sudo systemctl restart $SERVICE_NAME"
echo "  App logs:  tail -f $APP_DIR/logs/monitor.log"
echo ""
