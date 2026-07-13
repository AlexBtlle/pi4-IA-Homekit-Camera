#!/bin/bash
# Build the patched go2rtc binary for the HEVC/WebRTC live path (#59).
#
# Why a patch: Apple's new HKSV spec (§5 WebRTC Call Sequence) requires the
# ACCESSORY to generate the SDP offer — stock go2rtc's consumer API only
# answers a remote offer. go2rtc-solicit.patch adds two WebSocket messages
# ("webrtc/solicit" → go2rtc creates the offer; "webrtc/answer" → accepts the
# controller's answer) built on go2rtc's own CreateCompleteOffer/SetAnswer
# primitives. ~120 added lines, one new file — same vendoring precedent as
# build-static-ffmpeg.sh.
#
# Run on any machine with Go ≥1.23 (cross-compiles to the Pi):
#   bash scripts/build-go2rtc.sh              # arm64 (Pi 5) — the target
#   GOARCH=amd64 bash scripts/build-go2rtc.sh # x86 build box / CI test
#
# Output: build/go2rtc-solicit-<version>-linux-<arch>
set -euo pipefail

GO2RTC_TAG="v1.9.9"   # pinned: the patch is verified against this tag
GOOS="${GOOS:-linux}"
GOARCH="${GOARCH:-arm64}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PATCH="$SCRIPT_DIR/go2rtc-solicit.patch"
BUILD_DIR="$SCRIPT_DIR/../build"
SRC_DIR="$BUILD_DIR/go2rtc-$GO2RTC_TAG"
OUT="$BUILD_DIR/go2rtc-solicit-$GO2RTC_TAG-$GOOS-$GOARCH"

command -v go >/dev/null || { echo "Go toolchain required (apt install golang-go or golang.org/dl)"; exit 1; }
[ -f "$PATCH" ] || { echo "patch not found: $PATCH"; exit 1; }

mkdir -p "$BUILD_DIR"
if [ ! -d "$SRC_DIR" ]; then
    git clone --depth 1 --branch "$GO2RTC_TAG" \
        https://github.com/AlexxIT/go2rtc.git "$SRC_DIR"
fi

cd "$SRC_DIR"
git checkout -q "$GO2RTC_TAG" -- . 2>/dev/null || true
git clean -qfd 2>/dev/null || true
git apply --check "$PATCH"
git apply "$PATCH"

# Same flags as go2rtc's official release workflow (CGO_ENABLED=0 — the
# cgo-only v4l2 sources are excluded by design, we don't use them).
CGO_ENABLED=0 GOOS="$GOOS" GOARCH="$GOARCH" \
    go build -ldflags "-s -w" -trimpath -o "$OUT" .

echo ""
echo "Built: $OUT"
echo "Deploy to the Pi 5 as /opt/pi4cam/go2rtc/go2rtc (install.sh will own"
echo "this once the WebRTC path ships; manual copy is fine for testing)."
