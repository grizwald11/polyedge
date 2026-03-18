#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

pm2 start ecosystem.config.js
pm2 save

echo "PolyEdge started. Run 'pm2 logs polyedge' to view output."
