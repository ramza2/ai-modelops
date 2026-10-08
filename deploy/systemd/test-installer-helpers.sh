#!/usr/bin/env bash
# Focused regression checks for install-node-agent-service.sh helpers.
# Does not require root, systemd, Docker, or real tokens.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
INSTALLER="${ROOT_DIR}/scripts/install-node-agent-service.sh"

# Keep in sync with env_files_are_same() in install-node-agent-service.sh.
env_files_are_same() {
  local src="$1"
  local dst="$2"
  local src_norm dst_norm
  if [[ -e "${dst}" && "${src}" -ef "${dst}" ]]; then
    return 0
  fi
  src_norm="$(realpath -m "${src}")"
  dst_norm="$(realpath -m "${dst}")"
  [[ "${src_norm}" == "${dst_norm}" ]]
}

# Ensure installer still defines the helper (static contract).
grep -q '^env_files_are_same() {' "${INSTALLER}"
grep -q '^require_service_user_runtime_paths() {' "${INSTALLER}"
grep -q 'env_files_are_same "${ENV_FILE}" "${PROTECTED_ENV_FILE}"' "${INSTALLER}"
grep -q 'chown root:root "${PROTECTED_ENV_FILE}"' "${INSTALLER}"
grep -q 'require_service_user_runtime_paths' "${INSTALLER}"
# Self-copy avoidance: install(1) must not run when same.
if ! grep -A20 '^install_env_file() {' "${INSTALLER}" | grep -q 'env_files_are_same'; then
  echo "FAIL: install_env_file missing same-file guard" >&2
  exit 1
fi

tmpdir="$(mktemp -d)"
cleanup() { rm -rf "${tmpdir}"; }
trap cleanup EXIT

# --- same EnvironmentFile source/destination ---
identical="${tmpdir}/node-agent.env"
printf 'NODE_AGENT_TOKEN=test-not-a-real-secret\n' >"${identical}"
if ! env_files_are_same "${identical}" "${identical}"; then
  echo "FAIL: identical paths should be same" >&2
  exit 1
fi

protected_dir="${tmpdir}/etc-modelops"
mkdir -p "${protected_dir}"
ln -s "${identical}" "${protected_dir}/node-agent.env"
if ! env_files_are_same "${identical}" "${protected_dir}/node-agent.env"; then
  echo "FAIL: symlink to same inode should be same" >&2
  exit 1
fi

other="${tmpdir}/other.env"
printf 'NODE_AGENT_TOKEN=test-not-a-real-secret\n' >"${other}"
if env_files_are_same "${identical}" "${other}"; then
  echo "FAIL: distinct files must not be same" >&2
  exit 1
fi
echo "OK: env_files_are_same"

# --- inaccessible path fixture (service-user style negative check) ---
locked="${tmpdir}/locked"
mkdir -p "${locked}/node-agent/app"
printf 'pass\n' >"${locked}/node-agent/app/main.py"
chmod 000 "${locked}"
if [[ "$(id -u)" -eq 0 ]]; then
  echo "SKIP: inaccessible-path negative check under root"
else
  if test -x "${locked}/node-agent" 2>/dev/null || test -r "${locked}/node-agent/app/main.py" 2>/dev/null; then
    chmod 700 "${locked}" || true
    echo "FAIL: expected locked tree to be inaccessible" >&2
    exit 1
  fi
  echo "OK: inaccessible path negative fixture"
fi
chmod 700 "${locked}" 2>/dev/null || true

# Confirm path-access failure message contract exists (no auto chmod/chown repo).
grep -q 'does not chmod/chown the repository' "${INSTALLER}"
grep -q 'fail_service_user_path_access' "${INSTALLER}"

echo "installer helper regressions passed"
