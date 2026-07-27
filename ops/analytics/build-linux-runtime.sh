#!/usr/bin/env bash
set -Eeuo pipefail

UMAMI_COMMIT="2f6e2b5ff256862a081d9e74bed18a42ebf795e3"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK_DIR="${UMAMI_BUILD_DIR:-${TMPDIR:-/tmp}/umami-linux-runtime}"
SOURCE_DIR="${WORK_DIR}/source"
OUTPUT="${UMAMI_RUNTIME_OUTPUT:-${WORK_DIR}/umami-${UMAMI_COMMIT:0:12}-linux-amd64.tgz}"
IMAGE="umami-runtime:${UMAMI_COMMIT:0:12}"

mkdir -p "${WORK_DIR}"
if [[ ! -d "${SOURCE_DIR}/.git" ]]; then
  git clone --filter=blob:none --no-checkout https://github.com/umami-software/umami.git "${SOURCE_DIR}"
fi
git -C "${SOURCE_DIR}" fetch --depth=1 origin "${UMAMI_COMMIT}"
git -C "${SOURCE_DIR}" checkout --detach --force "${UMAMI_COMMIT}"
[[ "$(git -C "${SOURCE_DIR}" rev-parse HEAD)" == "${UMAMI_COMMIT}" ]]
node "${SCRIPT_DIR}/apply-umami-overrides.mjs" "${SOURCE_DIR}"

cd "${SOURCE_DIR}"
corepack pnpm@10.15.1 install --lockfile-only --ignore-scripts
corepack pnpm@10.15.1 audit --prod --audit-level high

ensure_image() {
  local image="$1"
  local mirror="$2"
  if docker image inspect "${image}" >/dev/null 2>&1; then return; fi
  if ! docker pull --platform linux/amd64 "${image}"; then
    docker pull --platform linux/amd64 "${mirror}"
    docker tag "${mirror}" "${image}"
  fi
}

ensure_image node:22-bookworm-slim docker.m.daocloud.io/library/node:22-bookworm-slim
ensure_image busybox:1.37 docker.m.daocloud.io/library/busybox:1.37
docker build --platform linux/amd64 -t "${IMAGE}" -f "${SCRIPT_DIR}/Dockerfile.runtime" .

container="$(docker create --platform linux/amd64 "${IMAGE}")"
trap 'docker rm -f "${container}" >/dev/null 2>&1 || true' EXIT
rm -rf "${WORK_DIR}/artifact"
docker cp "${container}:/artifact" "${WORK_DIR}/artifact"
tar -C "${WORK_DIR}/artifact" -czf "${OUTPUT}" .
if command -v sha256sum >/dev/null 2>&1; then
  (cd "$(dirname "${OUTPUT}")" && sha256sum "$(basename "${OUTPUT}")") >"${OUTPUT}.sha256"
else
  (cd "$(dirname "${OUTPUT}")" && shasum -a 256 "$(basename "${OUTPUT}")") >"${OUTPUT}.sha256"
fi
echo "${OUTPUT}"
