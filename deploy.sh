#!/bin/bash
# Deploy kalshi-arb-bot to a fresh Ubuntu droplet
# Usage: ssh root@159.65.44.106 'bash -s' < deploy.sh

set -e

APP_DIR="/opt/kalshi-arb-bot"
REPO="https://github.com/nstef18447/kalshi-arb-bot-.git"
SERVICE="kalshi-bot"

echo "=== Kalshi Arb Bot — Deployment ==="

# 1. Install Python if needed
if ! command -v python3 &>/dev/null; then
    echo "Installing Python..."
    apt-get update && apt-get install -y python3 python3-venv python3-pip git
else
    echo "Python3 already installed: $(python3 --version)"
fi

# 2. Clone or pull repo
if [ -d "$APP_DIR" ]; then
    echo "Updating existing repo..."
    cd "$APP_DIR"
    git pull origin master
else
    echo "Cloning repo..."
    git clone "$REPO" "$APP_DIR"
    cd "$APP_DIR"
fi

# 3. Set up venv and install deps
echo "Setting up virtual environment..."
python3 -m venv venv
venv/bin/pip install --upgrade pip
venv/bin/pip install -r requirements.txt

# 4. Check for .env and private key
if [ ! -f "$APP_DIR/.env" ]; then
    echo ""
    echo "WARNING: No .env file found at $APP_DIR/.env"
    echo "Create it with:"
    echo "  KALSHI_API_KEY=your-api-key"
    echo "  KALSHI_PRIVATE_KEY_PATH=$APP_DIR/kalshi_private_key.pem"
    echo "  KALSHI_ENV=demo"
    echo ""
fi

if [ ! -f "$APP_DIR/kalshi_private_key.pem" ]; then
    echo "WARNING: No private key found at $APP_DIR/kalshi_private_key.pem"
    echo "Copy it from your local machine with:"
    echo "  scp kalshi_private_key.pem root@159.65.44.106:$APP_DIR/"
    echo ""
fi

# 5. Install systemd service
echo "Installing systemd service..."
cat > /etc/systemd/system/${SERVICE}.service <<EOF
[Unit]
Description=Kalshi Arb Bot (Demo)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/venv/bin/python main.py
Restart=always
RestartSec=10
EnvironmentFile=$APP_DIR/.env

# Logging to journalctl
StandardOutput=journal
StandardError=journal
SyslogIdentifier=$SERVICE

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable $SERVICE

echo ""
echo "=== Deployment complete ==="
echo ""
echo "Next steps:"
echo "  1. Copy your .env and private key to $APP_DIR/ (if not already there)"
echo "  2. Start the bot:  systemctl start $SERVICE"
echo "  3. View logs:      journalctl -u $SERVICE -f"
echo "  4. Stop the bot:   systemctl stop $SERVICE"
echo "  5. Check status:   systemctl status $SERVICE"
echo ""
