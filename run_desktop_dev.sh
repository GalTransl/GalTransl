#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3}"
if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
  PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
fi

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Python not found. Set PYTHON_BIN or create .venv first." >&2
  exit 1
fi
if ! command -v npm >/dev/null 2>&1; then
  echo "npm not found in PATH." >&2
  exit 1
fi
if [[ ! -d "$ROOT_DIR/desktop/node_modules" ]]; then
  echo "Installing desktop frontend dependencies..."
  npm --prefix "$ROOT_DIR/desktop" ci --no-audit --no-fund
fi

BACKEND_PID=""
cleanup() {
  if [[ -n "$BACKEND_PID" ]] && kill -0 "$BACKEND_PID" 2>/dev/null; then
    kill "$BACKEND_PID" 2>/dev/null || true
    wait "$BACKEND_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

if "$PYTHON_BIN" -c "import socket; s=socket.create_connection(('127.0.0.1',12333),0.2); s.close()" >/dev/null 2>&1; then
  echo "Reusing GalTransl backend already listening on 127.0.0.1:12333."
else
  "$PYTHON_BIN" run_backend.py --host 127.0.0.1 --port 12333 &
  BACKEND_PID=$!
  BACKEND_READY="false"

  for _ in $(seq 1 80); do
    if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
      echo "GalTransl backend exited during startup." >&2
      exit 1
    fi
    if "$PYTHON_BIN" -c "import socket; s=socket.create_connection(('127.0.0.1',12333),0.2); s.close()" >/dev/null 2>&1; then
      BACKEND_READY="true"
      break
    fi
    sleep 0.25
  done

  if [[ "$BACKEND_READY" != "true" ]]; then
    echo "GalTransl backend did not become ready on 127.0.0.1:12333." >&2
    exit 1
  fi
fi

cd "$ROOT_DIR/desktop"
if command -v cargo >/dev/null 2>&1; then
  npm run tauri:dev
else
  echo "Cargo not found; starting browser frontend at http://127.0.0.1:1420"
  npm run dev
fi
