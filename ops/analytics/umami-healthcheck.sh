#!/usr/bin/env bash
set -euo pipefail

if ! curl -fsS --max-time 10 http://127.0.0.1:3102/analytics/api/heartbeat >/dev/null; then
  systemctl restart umami.service
fi
