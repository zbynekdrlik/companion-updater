#!/bin/bash
set -euo pipefail

# Build the companion-updater binary.
# 1. Build the WASM frontend with trunk → frontend/dist/
# 2. Build the Rust backend, which embeds frontend/dist/ via include_dir!

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "[1/2] Building frontend (trunk)..."
cd "${SCRIPT_DIR}/frontend"
trunk build --release

echo "[2/2] Building backend (cargo)..."
cd "${SCRIPT_DIR}"
cargo build --release -p companion-updater

BIN="${SCRIPT_DIR}/target/release/companion-updater"
if [ ! -x "${BIN}" ]; then
  echo "ERROR: binary not produced at ${BIN}"
  exit 1
fi

SIZE=$(du -h "${BIN}" | cut -f1)
echo ""
echo "Binary: ${BIN} (${SIZE})"
