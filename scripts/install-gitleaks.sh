#!/usr/bin/env bash
# install-gitleaks.sh DIR: puts the gitleaks binary in DIR, for the CI jobs.
#
# Pinned like everything else the pipeline runs: one version, and the SHA-256
# of each archive it may download, checked BEFORE anything is unpacked. Used
# instead of gitleaks-action, which ran on a Node version GitHub is retiring
# and did not read the changes of merge commits. To upgrade: change VERSION and
# both checksums, taken from that release's gitleaks_<version>_checksums.txt.
set -euo pipefail

VERSION="8.30.1"
SHA256_linux_x64="551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"
SHA256_darwin_arm64="b40ab0ae55c505963e365f271a8d3846efbc170aa17f2607f13df610a9aeb6a5"

dest=${1:?usage: install-gitleaks.sh DIR}
case "$(uname -s)-$(uname -m)" in
  Linux-x86_64) platform=linux_x64 ;;
  Darwin-arm64) platform=darwin_arm64 ;;
  *) echo "install-gitleaks: no pinned archive for $(uname -s)-$(uname -m)" >&2; exit 1 ;;
esac
expected_var="SHA256_${platform}"
expected=${!expected_var}

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
archive="$work/gitleaks.tar.gz"
curl -fsSL --retry 3 -o "$archive" \
  "https://github.com/gitleaks/gitleaks/releases/download/v${VERSION}/gitleaks_${VERSION}_${platform}.tar.gz"

actual=$(shasum -a 256 "$archive" | cut -d' ' -f1)
if [ "$actual" != "$expected" ]; then
  echo "install-gitleaks: checksum mismatch for gitleaks ${VERSION} ${platform}: nothing installed" >&2
  exit 1
fi

tar -xzf "$archive" -C "$work" gitleaks
mkdir -p "$dest"
install -m 755 "$work/gitleaks" "$dest/gitleaks"
"$dest/gitleaks" version
