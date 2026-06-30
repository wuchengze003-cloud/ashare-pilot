#!/usr/bin/env bash
set -Eeuo pipefail

UMAMI_VERSION="v3.2.0"
UMAMI_COMMIT="2f6e2b5ff256862a081d9e74bed18a42ebf795e3"
PNPM_VERSION="10.15.1"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DIR="/opt/umami/source/${UMAMI_COMMIT}"
RELEASE_DIR="/opt/umami/releases/${UMAMI_VERSION}-${UMAMI_COMMIT:0:12}"
UMAMI_ENV="/etc/umami/umami.env"
A_SHARE_ENV="/etc/a-share-assistant/a-share.env"
CREDENTIALS="/root/.config/a-share-assistant/umami-admin.env"
NGINX_SITE="/etc/nginx/sites-available/content-ops-studio"
RUNTIME_ARCHIVE="${UMAMI_RUNTIME_ARCHIVE:-}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this installer as root." >&2
  exit 1
fi

for command in git node corepack psql nginx curl openssl; do
  command -v "${command}" >/dev/null || { echo "Missing command: ${command}" >&2; exit 1; }
done

install -d -m 0755 /opt/umami/source /opt/umami/releases
install -d -m 0750 /etc/umami /etc/a-share-assistant
install -d -m 0700 /root/.config/a-share-assistant
id umami >/dev/null 2>&1 || useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin umami
id a-share >/dev/null 2>&1 || useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin a-share

if [[ ! -f "${UMAMI_ENV}" ]]; then
  DB_PASSWORD="$(openssl rand -hex 32)"
  APP_SECRET="$(openssl rand -hex 32)"
  cat >"${UMAMI_ENV}" <<EOF
DATABASE_URL=postgresql://umami:${DB_PASSWORD}@127.0.0.1:5432/umami
APP_SECRET=${APP_SECRET}
BASE_PATH=/analytics
HOSTNAME=127.0.0.1
PORT=3102
NODE_ENV=production
NEXT_TELEMETRY_DISABLED=1
DISABLE_TELEMETRY=1
EOF
  chown root:umami "${UMAMI_ENV}"
  chmod 0640 "${UMAMI_ENV}"
else
  DB_PASSWORD="$(sed -nE 's#^DATABASE_URL=postgresql://umami:([^@]+)@.*#\1#p' "${UMAMI_ENV}")"
fi

if [[ -z "${DB_PASSWORD}" || ! "${DB_PASSWORD}" =~ ^[a-f0-9]{64}$ ]]; then
  echo "Refusing to continue: unexpected Umami database password format." >&2
  exit 1
fi

if ! sudo -u postgres psql -Atqc "SELECT 1 FROM pg_roles WHERE rolname='umami'" | grep -qx 1; then
  sudo -u postgres psql -v ON_ERROR_STOP=1 -c "CREATE ROLE umami LOGIN PASSWORD '${DB_PASSWORD}'"
else
  sudo -u postgres psql -v ON_ERROR_STOP=1 -c "ALTER ROLE umami WITH LOGIN PASSWORD '${DB_PASSWORD}'"
fi
if ! sudo -u postgres psql -Atqc "SELECT 1 FROM pg_database WHERE datname='umami'" | grep -qx 1; then
  sudo -u postgres createdb --owner=umami umami
fi
sudo -u postgres psql -v ON_ERROR_STOP=1 -c "ALTER DATABASE umami OWNER TO umami"
sudo -u postgres psql -v ON_ERROR_STOP=1 -c "ALTER DATABASE umami SET timezone TO 'UTC'"

rm -rf "${RELEASE_DIR}"
install -d -m 0755 "${RELEASE_DIR}"

if [[ -n "${RUNTIME_ARCHIVE}" ]]; then
  [[ -f "${RUNTIME_ARCHIVE}" ]] || { echo "Runtime archive not found: ${RUNTIME_ARCHIVE}" >&2; exit 1; }
  [[ -f "${RUNTIME_ARCHIVE}.sha256" ]] || { echo "Missing checksum: ${RUNTIME_ARCHIVE}.sha256" >&2; exit 1; }
  (cd "$(dirname "${RUNTIME_ARCHIVE}")" && sha256sum -c "$(basename "${RUNTIME_ARCHIVE}.sha256")")
  migration_count="$(sudo -u postgres psql -d umami -Atqc 'SELECT count(*) FROM _prisma_migrations WHERE finished_at IS NOT NULL' 2>/dev/null || true)"
  [[ "${migration_count}" == "20" ]] || {
    echo "Expected 20 applied Umami migrations before using a prebuilt runtime; found ${migration_count:-0}." >&2
    exit 1
  }
  tar -xzf "${RUNTIME_ARCHIVE}" -C "${RELEASE_DIR}"
else
  if [[ ! -d "${SOURCE_DIR}/.git" ]]; then
    rm -rf "${SOURCE_DIR}"
    git clone --filter=blob:none --no-checkout https://github.com/umami-software/umami.git "${SOURCE_DIR}"
  fi
  git -C "${SOURCE_DIR}" fetch --depth=1 origin "${UMAMI_COMMIT}"
  git -C "${SOURCE_DIR}" checkout --detach --force "${UMAMI_COMMIT}"
  [[ "$(git -C "${SOURCE_DIR}" rev-parse HEAD)" == "${UMAMI_COMMIT}" ]]

  node "${SCRIPT_DIR}/apply-umami-overrides.mjs" "${SOURCE_DIR}"
  cd "${SOURCE_DIR}"
  corepack pnpm@"${PNPM_VERSION}" install --lockfile-only --ignore-scripts
  corepack pnpm@"${PNPM_VERSION}" audit --prod --audit-level high
  corepack pnpm@"${PNPM_VERSION}" install --frozen-lockfile
  if [[ "${UMAMI_SKIP_TESTS:-0}" != "1" ]]; then
    corepack pnpm@"${PNPM_VERSION}" test
  fi

  set -a
  # shellcheck disable=SC1090
  . "${UMAMI_ENV}"
  set +a
  NODE_OPTIONS="--max-old-space-size=1024" corepack pnpm@"${PNPM_VERSION}" build

  install -d -m 0755 "${RELEASE_DIR}/.next/static"
  cp -a .next/standalone/. "${RELEASE_DIR}/"
  cp -a .next/static/. "${RELEASE_DIR}/.next/static/"
  cp -a public "${RELEASE_DIR}/public"
  if [[ -d geo ]]; then cp -a geo "${RELEASE_DIR}/geo"; fi
fi
chown -R root:root "${RELEASE_DIR}"
chmod -R a+rX "${RELEASE_DIR}"
ln -sfn "${RELEASE_DIR}" /opt/umami/current

install -o root -g root -m 0644 "${SCRIPT_DIR}/umami.service" /etc/systemd/system/umami.service
systemctl daemon-reload
systemctl enable --now umami.service

for _ in $(seq 1 60); do
  curl -fsS http://127.0.0.1:3102/analytics/api/heartbeat >/dev/null 2>&1 && break
  sleep 1
done
curl -fsS http://127.0.0.1:3102/analytics/api/heartbeat >/dev/null

if [[ -f "${CREDENTIALS}" ]]; then
  # shellcheck disable=SC1090
  . "${CREDENTIALS}"
else
  ADMIN_PASSWORD="$(openssl rand -hex 24)"
  WEBSITE_ID=""
fi

login() {
  local password="$1"
  curl -fsS http://127.0.0.1:3102/analytics/api/auth/login \
    -H 'Content-Type: application/json' \
    --data "{\"username\":\"admin\",\"password\":\"${password}\"}"
}

if LOGIN_JSON="$(login "${ADMIN_PASSWORD}" 2>/dev/null)"; then
  :
else
  LOGIN_JSON="$(login umami)"
  DEFAULT_TOKEN="$(printf '%s' "${LOGIN_JSON}" | node -e 'let s="";process.stdin.on("data",d=>s+=d).on("end",()=>process.stdout.write(JSON.parse(s).token))')"
  USER_ID="$(printf '%s' "${LOGIN_JSON}" | node -e 'let s="";process.stdin.on("data",d=>s+=d).on("end",()=>process.stdout.write(JSON.parse(s).user.id))')"
  curl -fsS "http://127.0.0.1:3102/analytics/api/users/${USER_ID}" \
    -H "Authorization: Bearer ${DEFAULT_TOKEN}" \
    -H 'Content-Type: application/json' \
    --data "{\"password\":\"${ADMIN_PASSWORD}\"}" >/dev/null
  LOGIN_JSON="$(login "${ADMIN_PASSWORD}")"
fi
TOKEN="$(printf '%s' "${LOGIN_JSON}" | node -e 'let s="";process.stdin.on("data",d=>s+=d).on("end",()=>process.stdout.write(JSON.parse(s).token))')"

if [[ -z "${WEBSITE_ID:-}" ]]; then
  WEBSITE_JSON="$(curl -fsS http://127.0.0.1:3102/analytics/api/websites \
    -H "Authorization: Bearer ${TOKEN}" \
    -H 'Content-Type: application/json' \
    --data '{"name":"A股投资助手","domain":"47.77.231.22"}')"
  WEBSITE_ID="$(printf '%s' "${WEBSITE_JSON}" | node -e 'let s="";process.stdin.on("data",d=>s+=d).on("end",()=>process.stdout.write(JSON.parse(s).id))')"
fi

curl -fsS "http://127.0.0.1:3102/analytics/api/websites/${WEBSITE_ID}" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H 'Content-Type: application/json' \
  --data '{"replayConfig":{"replayEnabled":true,"heatmapEnabled":true,"sampleRate":0.5,"heatmapSampleRate":1,"maskLevel":"moderate","maxDuration":300000,"blockSelector":".analytics-private"}}' >/dev/null

cat >"${CREDENTIALS}" <<EOF
ADMIN_PASSWORD=${ADMIN_PASSWORD}
WEBSITE_ID=${WEBSITE_ID}
EOF
chmod 0600 "${CREDENTIALS}"

if [[ ! -f "${A_SHARE_ENV}" ]]; then
  INTERNAL_API_TOKEN="$(openssl rand -hex 32)"
  cat >"${A_SHARE_ENV}" <<EOF
PYSERVER_URL=http://127.0.0.1:8001
RUNTIME_DATA_DIR=data/runtime
NEXT_PUBLIC_SITE_URL=http://47.77.231.22/a-share
NEXT_PUBLIC_UMAMI_WEBSITE_ID=${WEBSITE_ID}
NEXT_PUBLIC_UMAMI_SCRIPT_URL=/analytics/script.js
NEXT_PUBLIC_UMAMI_RECORDER_URL=/analytics/recorder.js
NEXT_PUBLIC_UMAMI_DOMAINS=47.77.231.22
NEXT_PUBLIC_UMAMI_REPLAY_ENABLED=1
INTERNAL_API_TOKEN=${INTERNAL_API_TOKEN}
EOF
else
  sed -i "/^NEXT_PUBLIC_UMAMI_/d" "${A_SHARE_ENV}"
  cat >>"${A_SHARE_ENV}" <<EOF
NEXT_PUBLIC_UMAMI_WEBSITE_ID=${WEBSITE_ID}
NEXT_PUBLIC_UMAMI_SCRIPT_URL=/analytics/script.js
NEXT_PUBLIC_UMAMI_RECORDER_URL=/analytics/recorder.js
NEXT_PUBLIC_UMAMI_DOMAINS=47.77.231.22
NEXT_PUBLIC_UMAMI_REPLAY_ENABLED=1
EOF
  grep -q '^INTERNAL_API_TOKEN=' "${A_SHARE_ENV}" || printf 'INTERNAL_API_TOKEN=%s\n' "$(openssl rand -hex 32)" >>"${A_SHARE_ENV}"
fi
chown root:a-share "${A_SHARE_ENV}"
chmod 0640 "${A_SHARE_ENV}"

install -o root -g root -m 0644 "${SCRIPT_DIR}/nginx-rate-limits.conf" /etc/nginx/conf.d/analytics-rate-limits.conf
cp -a "${NGINX_SITE}" "${NGINX_SITE}.pre-umami-$(date +%Y%m%d-%H%M%S)"
sed "s/__WEBSITE_ID__/${WEBSITE_ID}/g" "${SCRIPT_DIR}/nginx-site.conf.template" >"${NGINX_SITE}"
nginx -t
systemctl reload nginx

echo "Umami ${UMAMI_VERSION} (${UMAMI_COMMIT}) is running on 127.0.0.1:3102."
echo "Website ID: ${WEBSITE_ID}"
echo "Credentials: ${CREDENTIALS} (root-only)"
