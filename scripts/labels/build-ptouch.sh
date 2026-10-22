#!/bin/bash
# Build the ptouch command line tool (ptouch-rs) from a pinned commit of the mlloyd5/ptouch-rs fork,
# which carries the PT-E310BT status fix until it is released upstream. Needs Docker; the build runs in
# a Debian bullseye Rust image (glibc 2.31), so the binary runs on Debian 11+/Ubuntu 22.04+, including
# the ARM Docker image. The Dockerfile and the Debian installer do the same build themselves.
#
#   scripts/labels/build-ptouch.sh [amd64|arm64] [output dir]
#
# Copy the result to /usr/local/bin/ptouch on the ARM box (or into the container) before running the
# installer: it keeps a ptouch that is already on PATH instead of downloading the upstream release.
# With a Rust toolchain on the box itself this is simply:
#   cargo install --git https://github.com/mlloyd5/ptouch-rs.git --rev c6478c5 --locked ptouch-cli
set -euo pipefail

PTOUCH_RS_REPO="${PTOUCH_RS_REPO:-https://github.com/mlloyd5/ptouch-rs.git}"
PTOUCH_RS_COMMIT="${PTOUCH_RS_COMMIT:-c6478c5}"   # pt-e310bt-status-notification on top of v0.14.0
RUST_IMAGE="${RUST_IMAGE:-rust:1-bullseye}"

arch="${1:-$(uname -m)}"
case "$arch" in
  amd64|x86_64) platform="linux/amd64"; suffix="amd64" ;;
  arm64|aarch64) platform="linux/arm64"; suffix="arm64" ;;
  *) echo "unknown architecture: $arch (use amd64 or arm64)" >&2; exit 1 ;;
esac
out="${2:-.}"
mkdir -p "$out"

echo "Building ptouch ($suffix) from $PTOUCH_RS_REPO at $PTOUCH_RS_COMMIT ..."
docker run --rm --platform "$platform" -v "$(cd "$out" && pwd):/out" "$RUST_IMAGE" bash -euo pipefail -c "
  apt-get update -qq && apt-get install -y -qq --no-install-recommends libusb-1.0-0-dev pkg-config > /dev/null
  cargo install --quiet --git $PTOUCH_RS_REPO --rev $PTOUCH_RS_COMMIT --locked --root /opt/ptouch ptouch-cli
  cp /opt/ptouch/bin/ptouch /out/ptouch-linux-$suffix
  /out/ptouch-linux-$suffix --version
"
if [ ! -f "$out/ptouch-linux-$suffix" ]; then
  echo "the container built ptouch but nothing arrived in $out: Docker cannot write there (on Docker Desktop" \
       "only shared folders such as your home directory work)" >&2
  exit 1
fi
echo "wrote $out/ptouch-linux-$suffix"
