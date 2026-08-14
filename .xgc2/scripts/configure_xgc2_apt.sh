#!/usr/bin/env bash
set -euo pipefail

validate_https_url() {
  local label="$1" value="$2"
  local pattern='^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~%+:/=-]*)?$'
  if [[ -z "${value}" || "${value}" =~ [[:space:][:cntrl:]] ||
        ! "${value}" =~ ${pattern} ]]; then
    echo "${label} must be an HTTPS URL without credentials, query, fragment, whitespace, or control characters" >&2
    return 1
  fi
}

if [[ $# -ne 2 ]]; then
  echo "usage: $0 <https-apt-base-url> <focal|jammy|noble>" >&2
  exit 2
fi

apt_base_url="${1%/}"
distribution="$2"
archive_key_url="https://xgc2.apt.xiaokang.ink/xgc2-archive-keyring.gpg"
archive_key_fingerprint="2A8E11B36F56D307ADF626D85E5FDC30979EA43F"

validate_https_url "XGC2 APT base URL" "${apt_base_url}"
validate_https_url "XGC2 APT archive key URL" "${archive_key_url}"
case "${distribution}" in
  focal|jammy|noble) ;;
  *)
    echo "unsupported XGC2 APT distribution: ${distribution}" >&2
    exit 1
    ;;
esac

apt-get update
apt-get install -y --no-install-recommends ca-certificates curl gnupg
key_file="$(mktemp /tmp/xgc2-archive-keyring.XXXXXX)"
trap 'rm -f -- "${key_file}"' EXIT
curl -fsSL "${archive_key_url}" -o "${key_file}"
actual_fingerprints="$(
  gpg --show-keys --with-colons "${key_file}" 2>/dev/null |
    awk -F: '$1 == "fpr" {print $10; exit}'
)"
if [[ "${actual_fingerprints}" != "${archive_key_fingerprint}" ]]; then
  echo "XGC2 APT archive key fingerprint mismatch" >&2
  exit 1
fi

install -d -m 0755 /etc/apt/keyrings
install -m 0644 "${key_file}" /etc/apt/keyrings/xgc2-archive-keyring.gpg
cat >/etc/apt/sources.list.d/xgc2-release-train.list <<EOF
deb [signed-by=/etc/apt/keyrings/xgc2-archive-keyring.gpg] ${apt_base_url} ${distribution} main
EOF
apt-get update
