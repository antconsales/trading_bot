#!/usr/bin/env bash
# Pi Trader — One-shot setup script for Raspberry Pi 4
# Run as: bash setup_pi.sh

set -e

PI_TRADER_DIR="$HOME/pi_trader"
SERVICE_NAME="pi-trader"

echo "=== Pi Trader Setup ==="

# 1. System packages
echo "[1/7] Installing system packages..."
sudo apt-get update -qq
sudo apt-get install -y -qq python3-pip python3-venv git curl

# 2. Create virtual environment
echo "[2/7] Creating Python virtual environment..."
python3 -m venv "$PI_TRADER_DIR/.venv"
source "$PI_TRADER_DIR/.venv/bin/activate"

# 3. Install Python dependencies
echo "[3/7] Installing Python packages..."
pip install --upgrade pip --quiet
pip install -r "$PI_TRADER_DIR/requirements.txt" --quiet

echo "Packages installed:"
pip list | grep -E "aiohttp|telegram|feedparser"

# 4. Install Ollama (if not present)
if ! command -v ollama &>/dev/null; then
    echo "[4/7] Installing Ollama..."
    curl -fsSL https://ollama.com/install.sh | sh
else
    echo "[4/7] Ollama already installed: $(ollama --version)"
fi

# 5. Pull LLM model
echo "[5/7] Pulling qwen3.5:0.8b (this may take a few minutes)..."
ollama pull qwen3.5:0.8b || echo "WARNING: Could not pull model — do it manually with: ollama pull qwen3.5:0.8b"

# 6. Setup .env
if [ ! -f "$PI_TRADER_DIR/.env" ]; then
    echo "[6/7] Creating .env from example..."
    cp "$PI_TRADER_DIR/.env.example" "$PI_TRADER_DIR/.env"
    echo ""
    echo "⚠️  IMPORTANT: Edit $PI_TRADER_DIR/.env and fill in:"
    echo "   - BINANCE_API_KEY"
    echo "   - BINANCE_API_SECRET"
    echo "   - TELEGRAM_BOT_TOKEN"
    echo "   - TELEGRAM_CHAT_ID"
    echo "   - Set PAPER_MODE=false when ready for live trading"
    echo ""
else
    echo "[6/7] .env already exists — skipping"
fi

# 7. Install systemd service
echo "[7/7] Installing systemd service..."
SERVICE_FILE="$PI_TRADER_DIR/trading_daemon.service"
if [ -f "$SERVICE_FILE" ]; then
    # Replace placeholders
    sed \
        -e "s|__HOME__|$HOME|g" \
        -e "s|__USER__|$(whoami)|g" \
        "$SERVICE_FILE" | sudo tee "/etc/systemd/system/${SERVICE_NAME}.service" > /dev/null
    sudo systemctl daemon-reload
    sudo systemctl enable "$SERVICE_NAME"
    echo "Service installed: $SERVICE_NAME"
    echo "Start with: sudo systemctl start $SERVICE_NAME"
    echo "Logs with:  sudo journalctl -u $SERVICE_NAME -f"
else
    echo "WARNING: $SERVICE_FILE not found — skipping service install"
fi

echo ""
echo "=== Setup complete ==="
echo "Next steps:"
echo "  1. Edit $PI_TRADER_DIR/.env (fill in API keys)"
echo "  2. sudo systemctl start $SERVICE_NAME"
echo "  3. sudo journalctl -u $SERVICE_NAME -f"
