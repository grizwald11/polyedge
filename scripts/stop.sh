#!/usr/bin/env bash
set -euo pipefail

pm2 stop polyedge
pm2 delete polyedge
pm2 save

echo "PolyEdge stopped and removed from pm2."
