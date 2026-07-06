#!/usr/bin/env bash
# Build a lean, statically-composed ffmpeg for pi4cam's live/HKSV spawn paths.
#
# WHY — Debian's ffmpeg links ~150 shared libraries (x265, OpenCL, SDL,
# Pulse…). On a 512 MB Pi with HKSV armed, just STARTING it costs 5-8 s:
# field-measured 7.6 s real for 1.5 s CPU with the page cache 100 % warm —
# the process stalls in memory reclaim while mapping and relocating ~50 MB
# of private pages it never uses. That startup tax is paid on EVERY live
# view (#43).
#
# This build contains only what the HomeKit app actually spawns ffmpeg for:
# RTSP in, RTP/SRTP out, -c:v copy, fragmented MP4 + native AAC for the
# HKSV prebuffer — and zero external dependencies. One small binary,
# ~0.2 s startup, trivially cacheable.
#
# Normal users don't run this: install.sh fetches the prebuilt binary from
# the project releases (built by CI from this very script). Run it yourself
# to build locally (any 64-bit Pi; ~30-60 min on a Zero 2 W):
#   bash scripts/build-static-ffmpeg.sh
# The HomeKit app auto-detects /opt/pi4cam/bin/ffmpeg-static on restart;
# to go back to the system ffmpeg, delete that file and restart.
#
# CI usage (see .github/workflows/ffmpeg-static.yml):
#   PREFIX=$PWD/out JOBS=$(nproc) bash scripts/build-static-ffmpeg.sh
set -euo pipefail

FFMPEG_VERSION="${FFMPEG_VERSION:-7.1.1}"
JOBS="${JOBS:-2}"          # -j2 keeps gcc well under the Zero 2 W's 512 MB
PREFIX="${PREFIX:-/opt/pi4cam/bin}"
WORK="${TMPDIR:-$HOME}/pi4cam-ffmpeg-build"
SUDO="sudo"
[[ "$(id -u)" -eq 0 ]] && SUDO=""

info() { echo "==> $*"; }

info "Installing build dependencies..."
${SUDO} apt-get update -qq || true
${SUDO} apt-get install -y build-essential curl xz-utils ca-certificates

mkdir -p "${WORK}"
cd "${WORK}"
if [[ ! -d "ffmpeg-${FFMPEG_VERSION}" ]]; then
    info "Downloading ffmpeg ${FFMPEG_VERSION}..."
    curl -fL "https://ffmpeg.org/releases/ffmpeg-${FFMPEG_VERSION}.tar.xz" | tar xJ
fi
cd "ffmpeg-${FFMPEG_VERSION}"

info "Configuring (rtsp/rtp/srtp + h264 copy + native aac + fmp4, nothing else)..."
./configure \
    --disable-everything \
    --disable-shared --enable-static \
    --disable-autodetect \
    --disable-doc --disable-debug \
    --disable-swscale --disable-postproc \
    --enable-small \
    --enable-network \
    --enable-protocol=tcp,udp,rtp,srtp,pipe,file \
    --enable-demuxer=rtsp,sdp \
    --enable-muxer=mp4,rtp \
    --enable-parser=h264,hevc,aac \
    --enable-decoder=pcm_u8,pcm_s16le \
    --enable-bsf=extract_extradata,h264_mp4toannexb,aac_adtstoasc \
    --enable-encoder=aac \
    --enable-indev=lavfi \
    --enable-filter=anullsrc,aresample,aformat,anull \
    --extra-cflags='-Os'
# ^ hevc parser: NOT used by the app, but ffmpeg 7.1's ff_h2645_sei_reset()
#   calls the aom film-grain code unguarded while only CONFIG_HEVC_SEI builds
#   that object — an h264-only link dies on ff_aom_uninit_film_grain_params
#   (hit in the field). The hevc parser selects hevc_sei and costs ~100 KB.
# ^ pcm decoders: the lavfi input wraps anullsrc frames as PCM packets that
#   must be decoded before the AAC encode — without them the prebuffer
#   command fails with 'no decoder found for: pcm_u8'.

info "Building (-j${JOBS} — expect 30-60 min on a Zero 2 W, ~2 min in CI)..."
make -j"${JOBS}" ffmpeg

${SUDO} mkdir -p "${PREFIX}"
${SUDO} install -m 755 ffmpeg "${PREFIX}/ffmpeg-static"
${SUDO} strip "${PREFIX}/ffmpeg-static" 2>/dev/null || true

info "Self-test: the exact features the HomeKit app uses..."
# prebuffer path: lavfi silence -> native aac -> fragmented mp4
"${PREFIX}/ffmpeg-static" -hide_banner -loglevel error \
    -f lavfi -i anullsrc=channel_layout=mono:sample_rate=32000 -t 0.2 \
    -c:a aac -b:a 32k -f mp4 -movflags frag_keyframe+empty_moov -y /dev/null
# live path: rtsp demuxer + rtp muxer + srtp protocol present
"${PREFIX}/ffmpeg-static" -hide_banner -demuxers 2>/dev/null | grep -q rtsp
"${PREFIX}/ffmpeg-static" -hide_banner -muxers   2>/dev/null | grep -q " rtp "
"${PREFIX}/ffmpeg-static" -hide_banner -protocols 2>/dev/null | grep -q srtp
# live path, full command shape against a dead port: every option must parse
# (the only acceptable failure is the connection itself)
LIVE_OUT=$("${PREFIX}/ffmpeg-static" -hide_banner -loglevel error \
    -fflags nobuffer -flags low_delay -analyzeduration 0 -probesize 32 \
    -rtsp_transport tcp -i rtsp://127.0.0.1:1/selftest \
    -an -sn -dn -codec:v copy -f rtp -payload_type 99 -ssrc 12345 \
    -srtp_out_suite AES_CM_128_HMAC_SHA1_80 \
    -srtp_out_params a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tr \
    "srtp://127.0.0.1:2?rtcpport=3&pkt_size=1316" 2>&1 || true)
echo "${LIVE_OUT}" | grep -q "Connection refused" \
    || { echo "live-path self-test failed: ${LIVE_OUT}" >&2; exit 1; }
info "Self-test OK."

echo ""
info "Startup benchmark — system ffmpeg vs this build:"
time ffmpeg -version >/dev/null 2>&1 || true
time "${PREFIX}/ffmpeg-static" -version >/dev/null
ls -lh "${PREFIX}/ffmpeg-static"

echo ""
echo "=========================================================="
echo "  ${PREFIX}/ffmpeg-static installed."
echo "  Restart the HomeKit app to pick it up (it logs its choice):"
echo "    sudo systemctl restart pi4cam-homekit"
echo "  Build dir kept at ${WORK} (delete it to reclaim ~500 MB)."
echo "=========================================================="
