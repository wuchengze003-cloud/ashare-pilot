#!/usr/bin/env bash
# Start pyserver (FastAPI on :8001) and web (Next.js on :3000) together.
# Reuse a listener only when both its working directory and command identify it
# as this project's service. Never terminate an unrelated process.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY_PORT="${PY_PORT:-8001}"
WEB_PORT="${WEB_PORT:-3000}"

PORT_ACTION=""

inspect_port() {
  local port="$1" label="$2"
  local pids pid process_cwd process_command expected_cwd
  pids="$(lsof -ti tcp:"$port" -sTCP:LISTEN || true)"
  if [[ -z "$pids" ]]; then
    PORT_ACTION="start"
    return 0
  fi

  expected_cwd="$ROOT/$label"
  for pid in $pids; do
    process_cwd="$(
      lsof -a -p "$pid" -d cwd -Fn 2>/dev/null |
        sed -n 's/^n//p' |
        head -n 1
    )"
    process_command="$(ps -p "$pid" -o command= 2>/dev/null || true)"

    if [[ "$process_cwd" != "$expected_cwd" && "$process_cwd" != "$expected_cwd/"* ]]; then
      echo "[start] refusing to use port $port ($label): pid $pid belongs to cwd ${process_cwd:-unknown}" >&2
      return 1
    fi
    if [[ "$label" == "pyserver" && "$process_command" != *"uvicorn"*"main:app"* ]]; then
      echo "[start] refusing to use port $port ($label): pid $pid is not this project's uvicorn service" >&2
      return 1
    fi
    if [[
      "$label" == "web" &&
      "$process_command" != *"next"*"dev"* &&
      "$process_command" != *"next"*"start"* &&
      "$process_command" != *"next-server"*
    ]]; then
      echo "[start] refusing to use port $port ($label): pid $pid is not this project's Next.js service" >&2
      return 1
    fi
  done

  echo "[start] reusing $label listener on :$port (pid ${pids//$'\n'/,})"
  PORT_ACTION="reuse"
}

inspect_port "$PY_PORT" pyserver
PY_ACTION="$PORT_ACTION"
inspect_port "$WEB_PORT" web
WEB_ACTION="$PORT_ACTION"

cleanup() {
  if [[ -z "${PY_PID:-}" && -z "${WEB_PID:-}" ]]; then
    return 0
  fi
  echo "[start] shutting down"
  [[ -n "${PY_PID:-}" ]] && kill "$PY_PID" 2>/dev/null || true
  [[ -n "${WEB_PID:-}" ]] && kill "$WEB_PID" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

if [[ "$PY_ACTION" == "start" ]]; then
  echo "[start] launching pyserver on :$PY_PORT"
  ( cd "$ROOT/pyserver" && uv run uvicorn main:app --port "$PY_PORT" ) &
  PY_PID=$!
fi

if [[ "$WEB_ACTION" == "start" ]]; then
  echo "[start] launching web on :$WEB_PORT"
  ( cd "$ROOT/web" && npm run dev -- --port "$WEB_PORT" ) &
  WEB_PID=$!
fi

# If both services were already healthy project listeners, there is nothing for
# this invocation to supervise or clean up.
if [[ -z "${PY_PID:-}" && -z "${WEB_PID:-}" ]]; then
  exit 0
fi

# Exit when a process started by this invocation dies. (`wait -n` with PIDs
# needs bash 5.1+; stock macOS bash is 3.2.)
while true; do
  [[ -n "${PY_PID:-}" ]] && ! kill -0 "$PY_PID" 2>/dev/null && break
  [[ -n "${WEB_PID:-}" ]] && ! kill -0 "$WEB_PID" 2>/dev/null && break
  sleep 1
done
