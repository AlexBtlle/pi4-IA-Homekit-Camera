/**
 * UUIDs and enums from Apple's "HomeKit Secure Video Open Source
 * Compatibility Guide" (Developer Preview v1.0, 2026-06-03) — the new
 * HKSV spec adding WebRTC live streaming, HEVC tiers and CMAF recording
 * ingest. Section references (§) point into that document.
 *
 * Developer Preview caveat: the Camera Capabilities / Camera Motion Zones
 * version strings are "17.99" and explicitly "may be updated prior to
 * release" — everything here may shift before GA.
 */

const BASE = "-0000-1000-8000-0026BB765291";
const apple = (short: string) => `${short}${BASE}`;

// ---------------------------------------------------------------------------
// Services (§3)
// ---------------------------------------------------------------------------

export const ServiceUuid = {
  /** §3.1 — presence of this service (Version "17.99") is what signals
   *  support for the whole new-spec camera functionality. */
  CameraCapabilities: apple("00008010"),
  /** §3.2 */
  CameraGlobalOperatingMode: apple("00008032"),
  /** §3.4 */
  CameraMotionZones: apple("00008021"),
  /** §3.5 */
  CameraBufferManagement: apple("00008000"),
  /** §3.6 — multi-tier RTP (legacy transport, kept alongside WebRTC) */
  CameraMultiTierRtpStreamManagement: apple("00008031"),
  /** §3.7 */
  CameraWebRTCStreamManagement: apple("00008033"),
  /** §3.8 — same UUID as today's Camera Recording Management */
  CameraRecordingManagement: apple("00000204"),
  /** §3.9 */
  CameraKeyManagement: apple("00008050"),
  /** §3.10 */
  CameraClientCertificateManagement: apple("00008080"),
} as const;

// ---------------------------------------------------------------------------
// Characteristics (§4)
// ---------------------------------------------------------------------------

export const CharacteristicUuid = {
  /** §4.1 — data */
  SensorUuid: apple("0000805B"),
  /** §4.2 — bool */
  MotionEnabled: apple("00008087"),
  /** §4.3 — tlv8, RTP tiers */
  SupportedVideoStreamTiers: apple("00008043"),
  /** §4.4 — tlv8, RTP audio tiers */
  SupportedAudioStreamTiers: apple("00008044"),
  /** §4.5 — tlv8, Paired Read */
  CameraCapabilities: apple("00008011"),
  /** §4.6 — tlv8 */
  ContributingSensors: apple("00008086"),
  /** §4.7 — tlv8, Paired Write + Timed Write */
  CameraKey: apple("00008051"),
  /** §4.8 — tlv8 */
  CameraKeyId: apple("00008052"),
  /** §4.9 — tlv8, write-response → Clip ID */
  BufferUploadCommand: apple("00008013"),
  /** §4.10 — tlv8, Paired Write */
  BufferActivityCommand: apple("00008017"),
  /** §4.11 — tlv8, write-response (event queue query/ack) */
  BufferEventCommand: apple("00008014"),
  /** §4.12 — uint32, Paired Read + Notify */
  BufferEventSequenceNumber: apple("00008015"),
  /** §4.13 — tlv8 (CMAF publishing_point_url + server CA certs) */
  CameraRecordingPublishingPoint: apple("00008016"),
  /** §4.14 — tlv8 */
  CameraZones: apple("00008022"),
  /** §4.15 — bool */
  StreamingEnabled: apple("00008041"),
  /** §4.16 — tlv8, write-response */
  RtpStreamingControl: apple("00008045"),
  /** §4.17 — tlv8, write-response: controller asks, ACCESSORY offers */
  WebRTCSolicitOffer: apple("00008053"),
  /** §4.18 — tlv8, write-response: controller returns the SDP ANSWER */
  WebRTCProvideAnswer: apple("00008054"),
  /** §4.19 — tlv8, write-response (Command 1 = End) */
  WebRTCStreamingControl: apple("00008056"),
  /** §4.20 — uint8 0-255, Paired Read + Notify */
  WebRTCNumberOfActiveSessions: apple("00008057"),
  /** §4.21 — tlv8, write-response */
  WebRTCReoffer: apple("00008058"),
  /** §4.22 — tlv8, write-response (SFrame key rotation) */
  WebRTCUpdateSession: apple("0000805C"),
  /** §4.23 — tlv8, Paired Read + Notify */
  WebRTCSupportedVideoStreamTiers: apple("00008059"),
  /** §4.24 — tlv8, Paired Read + Notify */
  WebRTCSupportedAudioStreamTiers: apple("0000805A"),
  /** §4.25 — tlv8, write-response (nonce in → CSR + signature out) */
  CameraClientCsr: apple("00008081"),
  /** §4.26 — tlv8, Paired Write + Timed Write */
  CameraClientCertificate: apple("00008082"),
  /** §4.27 — tlv8, Paired Read + Notify */
  CameraClientCertificateStatus: apple("00008083"),
} as const;

/** §3.1 note — value of the standard Version characteristic on the Camera
 *  Capabilities service. "May be updated prior to release." */
export const CAMERA_CAPABILITIES_VERSION = "17.99";

// ---------------------------------------------------------------------------
// Enums
// ---------------------------------------------------------------------------

/** §4.3 / §4.23 — Video Codec (nb: differs from legacy HAP's 0-based enum) */
export enum VideoCodec {
  H264 = 1,
  H265 = 2,
}

/** §4.4 / §4.24 — Audio Codec (0-2 and 4-255 reserved by Apple) */
export enum AudioCodec {
  Opus = 3,
}

/** §4.3 (page 14) — Camera Video Quality */
export enum VideoQuality {
  /** 4K stream of a camera that simultaneously offers a 2K High */
  Highest = 1,
  High = 2,
  Medium = 3,
  Low = 4,
}

/** §4.4 — Audio Stream Tier sample-rate enum */
export enum AudioSampleRate {
  Khz16 = 1,
  Khz24 = 2,
  Khz32 = 3,
  Khz48 = 4,
}

/** §4.4 — Audio Stream Tier bit-depth enum */
export enum AudioBitDepth {
  Bits8 = 1,
  Bits16 = 2,
  Bits24 = 3,
}

/** §4.5 — Sensor Configuration */
export enum SensorType {
  Unknown = 0,
  Primary = 1,
  Generic = 255,
}
export enum SensorIntent {
  Unknown = 0,
  Main = 1,
  Package = 2,
  Generic = 255,
}

/** §4.17 — WebRTC Solicit Offer response Status */
export enum SolicitOfferStatus {
  Success = 0,
  PrivacyModeActive = 1,
  Error = 2,
}

/** §4.18-4.19 — WebRTC Streaming Status */
export enum WebRTCStreamingStatus {
  Success = 0,
  UnknownSessionIdentifier = 1,
  Busy = 2,
  Error = 3,
}

// TLV field types, named per spec tables --------------------------------

/** §4.5 Camera Capabilities value */
export const CapTlv = { Version: 1, CameraSensors: 2 } as const;
/** §4.5 Camera Sensors */
export const SensorsTlv = { SensorConfiguration: 1 } as const;
/** §4.5 Sensor Configuration */
export const SensorCfgTlv = {
  SensorDimensions: 1,
  SensorUuid: 2,
  SensorType: 3,
  SensorIntent: 4,
  VideoStreamCapabilities: 5,
} as const;
/** §4.5 Sensor Dimensions */
export const DimTlv = { Width: 1, Height: 2 } as const;
/** §4.5 Camera Video Stream Capabilities */
export const VideoCapTlv = {
  Identifier: 1,
  VideoQuality: 2,
  Width: 3,
  Height: 4,
  FramesPerSecond: 5,
  AverageBitRate: 6,
  PeakBitRate: 7,
} as const;

/** §4.3 / §4.23 Supported Video Stream Tiers value */
export const VideoTiersTlv = { Codec: 1, PayloadType: 2, Tiers: 3 } as const;
/** §4.3 Video Stream Tier */
export const VideoTierTlv = {
  Identifier: 1,
  Quality: 2,
  TargetAverageBitrate: 3, // kbps
  Width: 4,
  Height: 5,
  FrameRate: 6,
} as const;

/** §4.4 / §4.24 Supported Audio Stream Tiers value */
export const AudioTiersTlv = { Codec: 1, PayloadType: 2, Tiers: 3 } as const;
/** §4.4 Audio Stream Tier */
export const AudioTierTlv = {
  Identifier: 1,
  TargetAverageBitrate: 2, // bits per second
  SampleRate: 3,
  BitDepth: 4,
  PacketTime: 5, // ms — only 20 allowed in this spec revision
  NumberOfChannels: 6, // only 1 allowed in this spec revision
} as const;

/** §4.17 WebRTC Solicit Offer write / response */
export const SolicitOfferTlv = {
  // write
  Options: 1,
  // response
  SessionIdentifier: 1,
  SdpOffer: 2,
  AdditionalCandidates: 3,
  Status: 4,
  SFrameConfiguration: 5,
} as const;
/** §4.17 WebRTC Offer Options */
export const OfferOptionsTlv = { SFrameEnabled: 1 } as const;
/** §4.17 WebRTC ICE Candidate */
export const IceCandidateTlv = {
  Candidate: 1,
  SdpMid: 2,
  SdpMLineIndex: 3,
} as const;

/** §4.18 WebRTC Provide Answer write / response */
export const ProvideAnswerTlv = {
  SessionIdentifier: 1,
  SdpAnswer: 2,
  AdditionalCandidates: 3,
  Status: 2, // response field 2 (same table position as SdpAnswer on write)
} as const;

/** §4.19 WebRTC Streaming Control */
export const StreamingControlTlv = {
  SessionIdentifier: 1,
  Command: 2, // 1 = End
  Status: 2, // response
} as const;
