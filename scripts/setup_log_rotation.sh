#!/usr/bin/env bash
# setup_log_rotation.sh — Configure pm2-logrotate for PolyEdge
# Run ONCE after first `pm2 start ecosystem.config.js`
#
# Without this, PM2 logs at ~/.pm2/logs/ grow unbounded and will
# eventually exhaust disk space on the Mac Mini.

set -euo pipefail

echo "=== PolyEdge PM2 Log Rotation Setup ==="

# Check pm2 is available
if ! command -v pm2 &>/dev/null; then
    echo "ERROR: pm2 not found. Install with: npm install -g pm2"
    exit 1
fi

# Install pm2-logrotate module
echo "Installing pm2-logrotate..."
pm2 install pm2-logrotate

# Configure rotation settings
echo "Configuring rotation settings..."
pm2 set pm2-logrotate:max_size 50M      # Rotate when log exceeds 50 MB
pm2 set pm2-logrotate:retain 5           # Keep last 5 rotated files
pm2 set pm2-logrotate:compress true      # gzip old logs
pm2 set pm2-logrotate:dateFormat YYYY-MM-DD_HH-mm-ss
pm2 set pm2-logrotate:workerInterval 30  # Check every 30 seconds
pm2 set pm2-logrotate:rotateInterval "0 0 * * *"  # Also rotate daily at midnight

echo ""
echo "=== Log rotation configured ==="
echo "Verify with: pm2 conf pm2-logrotate"
pm2 conf pm2-logrotate
