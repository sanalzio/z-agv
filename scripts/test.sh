#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "[Z-AGV] Backend, PWA ve protokol testleri çalıştırılıyor..."
"$ROOT/.venv/bin/python" "$ROOT/tests/test_backend.py"

echo "[Z-AGV] Firmware derleme kontrolü..."
arduino-cli compile --fqbn arduino:avr:leonardo --warnings all "$ROOT/firmware/agv_leonardo"
