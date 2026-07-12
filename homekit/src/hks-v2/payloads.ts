/**
 * TLV8 payload builders for the new-HKSV-spec characteristics (#59).
 *
 * Pure functions (Buffer in → Buffer out) so every advertised payload is
 * unit-testable byte-for-byte — the probe and the future production path
 * share them. Field layouts follow the spec tables referenced in spec.ts.
 */
import {
  AudioBitDepth,
  AudioCodec,
  AudioSampleRate,
  AudioTierTlv,
  AudioTiersTlv,
  CapTlv,
  DimTlv,
  SensorCfgTlv,
  SensorIntent,
  SensorType,
  SensorsTlv,
  VideoCapTlv,
  VideoCodec,
  VideoQuality,
  VideoTierTlv,
  VideoTiersTlv,
} from "./spec";
import {
  SEPARATOR,
  TlvEntry,
  encodeTlv8,
  joinTlv8List,
  uint8,
  uint16,
  uint32,
  utf8,
} from "./tlv8";

/** "Repeated" TLV fields: same-type items delimited by a zero-length
 *  type-0x00 TLV — the convention hap-nodejs's own encoder uses. */
function repeated(type: number, items: Buffer[]): TlvEntry[] {
  const out: TlvEntry[] = [];
  items.forEach((data, i) => {
    if (i > 0) out.push(SEPARATOR);
    out.push({ type, data });
  });
  return out;
}

export interface VideoTier {
  /** uint32 tier identifier, referenced by streaming-control commands */
  id: number;
  quality: VideoQuality;
  width: number;
  height: number;
  fps: number;
  /** kbps */
  avgKbps: number;
  /** kbps — only used by Camera Capabilities (peak column) */
  peakKbps: number;
}

/** The 2K-camera ladder from §2 Minimum Requirements (16:9 sensor). */
export function defaultTiers2K(): VideoTier[] {
  return [
    { id: 1, quality: VideoQuality.High, width: 2560, height: 1440, fps: 30, avgKbps: 2800, peakKbps: 3000 },
    { id: 2, quality: VideoQuality.Medium, width: 1920, height: 1080, fps: 30, avgKbps: 1700, peakKbps: 1800 },
    { id: 3, quality: VideoQuality.Low, width: 640, height: 360, fps: 15, avgKbps: 180, peakKbps: 190 },
  ];
}

/** §4.5 — Camera Capabilities characteristic value. */
export function buildCameraCapabilities(
  sensorUuid: Buffer,
  sensorWidth: number,
  sensorHeight: number,
  tiers: VideoTier[],
): Buffer {
  const dims = encodeTlv8([
    { type: DimTlv.Width, data: uint16(sensorWidth) },
    { type: DimTlv.Height, data: uint16(sensorHeight) },
  ]);
  const streamCaps = tiers.map((t) =>
    encodeTlv8([
      // Identifier here is "a UUID identifying this video configuration" —
      // 16 bytes derived deterministically from the tier id.
      { type: VideoCapTlv.Identifier, data: tierUuid(sensorUuid, t.id) },
      { type: VideoCapTlv.VideoQuality, data: uint8(t.quality) },
      { type: VideoCapTlv.Width, data: uint16(t.width) },
      { type: VideoCapTlv.Height, data: uint16(t.height) },
      { type: VideoCapTlv.FramesPerSecond, data: uint8(t.fps) },
      { type: VideoCapTlv.AverageBitRate, data: uint32(t.avgKbps) },
      { type: VideoCapTlv.PeakBitRate, data: uint32(t.peakKbps) },
    ]),
  );
  const sensorCfg = encodeTlv8([
    { type: SensorCfgTlv.SensorDimensions, data: dims },
    { type: SensorCfgTlv.SensorUuid, data: sensorUuid },
    { type: SensorCfgTlv.SensorType, data: uint8(SensorType.Primary) },
    { type: SensorCfgTlv.SensorIntent, data: uint8(SensorIntent.Main) },
    ...repeated(SensorCfgTlv.VideoStreamCapabilities, streamCaps),
  ]);
  const sensors = encodeTlv8([
    { type: SensorsTlv.SensorConfiguration, data: sensorCfg },
  ]);
  return encodeTlv8([
    { type: CapTlv.Version, data: uint8(1) },
    { type: CapTlv.CameraSensors, data: sensors },
  ]);
}

/** Derive a stable 16-byte per-tier UUID from the sensor UUID + tier id. */
export function tierUuid(sensorUuid: Buffer, tierId: number): Buffer {
  const b = Buffer.from(sensorUuid); // copy
  b[15] = (b[15] + tierId) & 0xff;
  return b.subarray(0, 16);
}

/** §4.23 (also §4.3 shape) — WebRTC Supported Video Stream Tiers value. */
export function buildVideoStreamTiers(
  codec: VideoCodec,
  payloadType: number,
  tiers: VideoTier[],
): Buffer {
  const tierBufs = tiers.map((t) =>
    encodeTlv8([
      { type: VideoTierTlv.Identifier, data: uint32(t.id) },
      { type: VideoTierTlv.Quality, data: uint8(t.quality) },
      { type: VideoTierTlv.TargetAverageBitrate, data: uint32(t.avgKbps) },
      { type: VideoTierTlv.Width, data: uint16(t.width) },
      { type: VideoTierTlv.Height, data: uint16(t.height) },
      { type: VideoTierTlv.FrameRate, data: uint8(t.fps) },
    ]),
  );
  return encodeTlv8([
    { type: VideoTiersTlv.Codec, data: uint8(codec) },
    { type: VideoTiersTlv.PayloadType, data: uint8(payloadType) },
    ...repeated(VideoTiersTlv.Tiers, tierBufs),
  ]);
}

/**
 * §4.24 (also §4.4 shape) — WebRTC Supported Audio Stream Tiers value.
 * Opus only; exactly one tier allowed in this spec revision. Sample rate is
 * reported as 48 kHz (Opus transmission rate) per the §2 note; capture rate
 * (16/24 kHz) is an encoder concern, not advertised here.
 */
export function buildAudioStreamTiers(payloadType: number): Buffer {
  const tier = encodeTlv8([
    { type: AudioTierTlv.Identifier, data: uint32(1) },
    { type: AudioTierTlv.TargetAverageBitrate, data: uint32(24_000) }, // b/s
    { type: AudioTierTlv.SampleRate, data: uint8(AudioSampleRate.Khz48) },
    { type: AudioTierTlv.BitDepth, data: uint8(AudioBitDepth.Bits16) },
    { type: AudioTierTlv.PacketTime, data: uint8(20) },
    { type: AudioTierTlv.NumberOfChannels, data: uint8(1) },
  ]);
  return encodeTlv8([
    { type: AudioTiersTlv.Codec, data: uint8(AudioCodec.Opus) },
    { type: AudioTiersTlv.PayloadType, data: uint8(payloadType) },
    { type: AudioTiersTlv.Tiers, data: tier },
  ]);
}

/** §4.17 — encode the ICE candidate list of a Solicit Offer response. */
export function buildIceCandidates(
  candidates: { candidate: string; sdpMid?: string; sdpMLineIndex?: number }[],
): Buffer[] {
  return candidates.map((c) =>
    encodeTlv8([
      { type: 1, data: utf8(c.candidate) },
      ...(c.sdpMid !== undefined ? [{ type: 2, data: utf8(c.sdpMid) }] : []),
      ...(c.sdpMLineIndex !== undefined
        ? [{ type: 3, data: uint16(c.sdpMLineIndex) }]
        : []),
    ]),
  );
}

export { joinTlv8List };

/**
 * A canned, RFC 8866-plausible SDP offer: H.265 video + Opus audio over
 * DTLS-SRTP with a host ICE candidate. The PROBE only — the fingerprint and
 * candidate are placeholders, no media engine sits behind them. The goal is
 * to observe whether the controller parses the offer and comes back with a
 * WebRTC Provide Answer write; actual media needs the go2rtc integration.
 */
export function cannedSdpOffer(hostIp: string, sessionId: bigint): string {
  return [
    "v=0",
    `o=- ${sessionId} 1 IN IP4 ${hostIp}`,
    "s=-",
    "t=0 0",
    "a=group:BUNDLE 0 1",
    "a=ice-options:trickle",
    "a=fingerprint:sha-256 " +
      "00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:" +
      "00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF",
    "a=setup:actpass",
    `m=video 9 UDP/TLS/RTP/SAVPF 96`,
    `c=IN IP4 ${hostIp}`,
    "a=mid:0",
    "a=sendonly",
    "a=rtcp-mux",
    "a=ice-ufrag:pi4c",
    "a=ice-pwd:pi4camprobeicepassword0000",
    "a=rtpmap:96 H265/90000",
    "a=fmtp:96 profile-id=1",
    `a=candidate:1 1 udp 2130706431 ${hostIp} 40000 typ host`,
    `m=audio 9 UDP/TLS/RTP/SAVPF 97`,
    `c=IN IP4 ${hostIp}`,
    "a=mid:1",
    "a=sendonly",
    "a=rtcp-mux",
    "a=ice-ufrag:pi4c",
    "a=ice-pwd:pi4camprobeicepassword0000",
    "a=rtpmap:97 opus/48000/2",
    "a=fmtp:97 minptime=10;useinbandfec=1",
  ].join("\r\n") + "\r\n";
}
