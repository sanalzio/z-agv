#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT=8000
UFW_OPENED=0

cleanup() {
  if [[ "$UFW_OPENED" -eq 1 ]]; then
    UFW_OPENED=0
    echo ""
    echo "[Z-AGV] Geçici UFW kuralı kaldırılıyor (${PORT}/tcp)..."
    sudo ufw delete allow "${PORT}/tcp" || true
  fi
}
trap cleanup EXIT INT TERM

# Open port 8000 only while UFW is active; trap guarantees cleanup on Ctrl+C
if command -v ufw >/dev/null 2>&1 && sudo ufw status | grep -q "Status: active"; then
  echo "[Z-AGV] UFW aktif — ${PORT}/tcp geçici olarak açılıyor..."
  sudo ufw allow "${PORT}/tcp"
  UFW_OPENED=1
fi

echo "[Z-AGV] SCADA erişim adresleri:"
ip -4 -br addr 2>/dev/null | awk '$2=="UP" && $1!="lo" {sub(/\/.*/, "", $3); print "  -> http://" $3 ":8000"}' || true
echo "  -> http://localhost:${PORT}"
echo "[Z-AGV] Sunucu başlatılıyor (durdurmak için Ctrl+C)..."

sudo rfcomm bind 0 00:21:07:00:4F:AE 1
"$ROOT/.venv/bin/python" "$ROOT/backend/server.py"
