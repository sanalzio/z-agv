#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${1:-${port:-/dev/ttyACM0}}"
FQBN="arduino:avr:leonardo"
SKETCH="$ROOT/firmware/agv_leonardo"

echo "[Z-AGV] Uyarı: USB üzerinden yüklerken pil E-Stop anahtarının KAPALI olduğundan emin olun."
echo "[Z-AGV] Derleniyor (${FQBN})..."
arduino-cli compile --fqbn "$FQBN" "$SKETCH"

echo "[Z-AGV] Yükleniyor (${PORT})..."
arduino-cli upload -p "$PORT" --fqbn "$FQBN" "$SKETCH"
echo "[Z-AGV] Yükleme tamamlandı."
