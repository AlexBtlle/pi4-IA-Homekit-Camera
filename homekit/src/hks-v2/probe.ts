/**
 * tvOS 27 protocol probe (#59) — a THROWAWAY HomeKit accessory exposing the
 * new HKSV spec's services with stub data, to observe what a real controller
 * (Apple TV 4K on the tvOS 27 beta) actually does with them.
 *
 * This is the week-1 go/no-go experiment: the Developer Preview PDF
 * describes the protocol, but nothing proves the CURRENT beta drives it.
 * Every characteristic read/write is logged verbosely; the stub answers
 * just enough (canned SDP offer, echoed session IDs) to keep a conversation
 * going. No media flows — that needs the go2rtc integration, which is only
 * worth building if this probe shows the controller talking.
 *
 * Deliberately separate from the production accessory: its own pairing
 * identity, its own persist directory (./probe-persist), never installed as
 * a service. Run it on the Pi (or any Linux box on the same LAN):
 *
 *     cd /opt/pi4cam/homekit && npm run build && node dist/hks-v2/probe.js
 *     node dist/hks-v2/probe.js --with-legacy   # + legacy CameraController
 *
 * Interpreting results:
 * - "READ  Camera Capabilities" → the controller parses the new discovery
 *   service: the beta knows the spec exists.
 * - "WRITE WebRTC Solicit Offer" → the controller initiates the new live
 *   view flow (per §5 the ACCESSORY generates the SDP offer!): full go.
 * - Nothing beyond pairing → the beta doesn't drive the new services yet;
 *   re-test on the next beta / GA (September). Compare with --with-legacy
 *   to check whether a legacy RTP service is a prerequisite for the Home
 *   app to treat the accessory as a camera at all.
 */
import crypto from "crypto";
import fs from "fs";
import os from "os";
import path from "path";

import {
  Accessory,
  Categories,
  Characteristic,
  CharacteristicValue,
  HAPStorage,
  Service,
  uuid,
} from "@homebridge/hap-nodejs";
import qrcode from "qrcode-terminal";

import {
  buildAudioStreamTiers,
  buildCameraCapabilities,
  buildIceCandidates,
  buildVideoStreamTiers,
  cannedSdpOffer,
  defaultTiers2K,
} from "./payloads";
import {
  CameraCapabilitiesCharacteristic,
  CameraCapabilitiesService,
  CameraGlobalOperatingModeService,
  CameraWebRTCStreamManagementService,
  SensorUuidCharacteristic,
  StreamingEnabledCharacteristic,
  WebRTCNumberOfActiveSessionsCharacteristic,
  WebRTCProvideAnswerCharacteristic,
  WebRTCReofferCharacteristic,
  WebRTCSolicitOfferCharacteristic,
  WebRTCStreamingControlCharacteristic,
  WebRTCSupportedAudioStreamTiersCharacteristic,
  WebRTCSupportedVideoStreamTiersCharacteristic,
  WebRTCUpdateSessionCharacteristic,
} from "./services";
import {
  CAMERA_CAPABILITIES_VERSION,
  SolicitOfferStatus,
  VideoCodec,
  WebRTCStreamingStatus,
} from "./spec";
import { decodeTlv8, encodeTlv8, hexDump, uint8, utf8 } from "./tlv8";

const PROBE_DIR = path.resolve("probe-persist");
const PINCODE = "031-45-154"; // throwaway probe — never the production PIN

// ---------------------------------------------------------------------------
// Logging — timestamps matter: we correlate with what the Apple TV shows.
// ---------------------------------------------------------------------------

function log(kind: string, name: string, detail = ""): void {
  const ts = new Date().toISOString().slice(11, 23);
  console.log(`[${ts}] ${kind.padEnd(5)} ${name}${detail ? ` — ${detail}` : ""}`);
}

/** TLV8 characteristics carry base64 strings in HAP-NodeJS. */
function fromB64(value: CharacteristicValue | null | undefined): Buffer {
  return Buffer.from(typeof value === "string" ? value : "", "base64");
}

function describeTlv(buf: Buffer): string {
  try {
    const entries = decodeTlv8(buf);
    const parts = entries.map((e) =>
      e.data.length === 0
        ? `t${e.type}:∅`
        : `t${e.type}[${e.data.length}]=${e.data.length <= 40 ? e.data.toString("hex") : e.data.subarray(0, 40).toString("hex") + "…"}`,
    );
    return parts.join(" ");
  } catch (err) {
    return `unparseable (${(err as Error).message}): ${hexDump(buf, 64)}`;
  }
}

// ---------------------------------------------------------------------------
// Probe identity — stable across runs so re-pairing isn't needed each time.
// ---------------------------------------------------------------------------

interface ProbeIdentity {
  username: string;
  setupID: string;
  sensorUuidHex: string;
}

function loadIdentity(): ProbeIdentity {
  const file = path.join(PROBE_DIR, "probe-identity.json");
  if (fs.existsSync(file)) {
    return JSON.parse(fs.readFileSync(file, "utf8"));
  }
  const octets = crypto.randomBytes(5);
  const identity: ProbeIdentity = {
    // Locally-administered unicast MAC.
    username: ["1A", ...[...octets].map((b) => b.toString(16).padStart(2, "0").toUpperCase())].join(":"),
    setupID: Array.from({ length: 4 }, () =>
      "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"[crypto.randomInt(36)],
    ).join(""),
    sensorUuidHex: crypto.randomBytes(16).toString("hex"),
  };
  fs.writeFileSync(file, JSON.stringify(identity, null, 2));
  return identity;
}

function lanIp(): string {
  for (const addrs of Object.values(os.networkInterfaces())) {
    for (const a of addrs ?? []) {
      if (a.family === "IPv4" && !a.internal) return a.address;
    }
  }
  return "127.0.0.1";
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

function main(): void {
  const withLegacy = process.argv.includes("--with-legacy");

  fs.mkdirSync(PROBE_DIR, { recursive: true, mode: 0o700 });
  HAPStorage.setCustomStoragePath(PROBE_DIR);
  const identity = loadIdentity();
  const sensorUuid = Buffer.from(identity.sensorUuidHex, "hex");
  const tiers = defaultTiers2K();
  const ip = lanIp();

  const accessory = new Accessory(
    "HKSV v2 Probe",
    uuid.generate(`pi4cam-probe:${identity.username}`),
  );
  accessory
    .getService(Service.AccessoryInformation)!
    .setCharacteristic(Characteristic.Manufacturer, "pi4-IA-Homekit-Camera")
    .setCharacteristic(Characteristic.Model, "HKSV v2 protocol probe")
    .setCharacteristic(Characteristic.SerialNumber, identity.username)
    .setCharacteristic(Characteristic.FirmwareRevision, "0.0.1");

  // --- §3.1 Camera Capabilities -------------------------------------------
  const caps = new CameraCapabilitiesService();
  caps
    .getCharacteristic(Characteristic.Version)!
    .onGet(() => {
      log("READ", "Camera Capabilities Version", `→ "${CAMERA_CAPABILITIES_VERSION}"`);
      return CAMERA_CAPABILITIES_VERSION;
    })
    .updateValue(CAMERA_CAPABILITIES_VERSION);
  const capsPayload = buildCameraCapabilities(sensorUuid, 4608, 2592, tiers);
  caps
    .getCharacteristic(CameraCapabilitiesCharacteristic)!
    .onGet(() => {
      log("READ", "Camera Capabilities", `→ ${capsPayload.length} bytes`);
      return capsPayload.toString("base64");
    })
    .updateValue(capsPayload.toString("base64"));
  accessory.addService(caps);

  // --- §3.2 Camera Global Operating Mode ----------------------------------
  const opMode = new CameraGlobalOperatingModeService();
  let cameraActive = 1;
  let streamingEnabled = true;
  opMode
    .getCharacteristic(Characteristic.HomeKitCameraActive)!
    .onGet(() => {
      log("READ", "HomeKit Camera Active", `→ ${cameraActive}`);
      return cameraActive;
    })
    .onSet((v) => {
      log("WRITE", "HomeKit Camera Active", String(v));
      cameraActive = v as number;
    })
    .updateValue(cameraActive);
  opMode
    .getCharacteristic(StreamingEnabledCharacteristic)!
    .onGet(() => {
      log("READ", "Streaming Enabled (op mode)", `→ ${streamingEnabled}`);
      return streamingEnabled;
    })
    .onSet((v) => {
      log("WRITE", "Streaming Enabled (op mode)", String(v));
      streamingEnabled = Boolean(v);
    })
    .updateValue(streamingEnabled);
  opMode
    .getCharacteristic(Characteristic.CameraOperatingModeIndicator)!
    .updateValue(1);
  accessory.addService(opMode);

  // --- §3.7 Camera WebRTC Stream Management --------------------------------
  const webrtc = new CameraWebRTCStreamManagementService();
  const sessions = new Map<string, { createdAt: number }>();
  const activeSessions = webrtc.getCharacteristic(
    WebRTCNumberOfActiveSessionsCharacteristic,
  )!;

  const videoTiersPayload = buildVideoStreamTiers(VideoCodec.H265, 96, tiers);
  webrtc
    .getCharacteristic(WebRTCSupportedVideoStreamTiersCharacteristic)!
    .onGet(() => {
      log("READ", "WebRTC Supported Video Stream Tiers", `→ H.265, ${tiers.length} tiers`);
      return videoTiersPayload.toString("base64");
    })
    .updateValue(videoTiersPayload.toString("base64"));

  const audioTiersPayload = buildAudioStreamTiers(97);
  webrtc
    .getCharacteristic(WebRTCSupportedAudioStreamTiersCharacteristic)!
    .onGet(() => {
      log("READ", "WebRTC Supported Audio Stream Tiers", "→ Opus 48 kHz");
      return audioTiersPayload.toString("base64");
    })
    .updateValue(audioTiersPayload.toString("base64"));

  webrtc
    .getCharacteristic(StreamingEnabledCharacteristic)!
    .onGet(() => {
      log("READ", "Streaming Enabled (WebRTC svc)", `→ ${streamingEnabled}`);
      return streamingEnabled;
    })
    .updateValue(streamingEnabled);

  webrtc
    .getCharacteristic(SensorUuidCharacteristic)!
    .onGet(() => {
      log("READ", "Sensor UUID (WebRTC svc)");
      return sensorUuid.toString("base64");
    })
    .updateValue(sensorUuid.toString("base64"));

  // §4.17 — THE key probe point. Per §5, the controller writes here and the
  // ACCESSORY must come back with a session ID + an SDP OFFER (the accessory
  // is the offerer — inverse of WHEP; noted for the go2rtc phase).
  webrtc
    .getCharacteristic(WebRTCSolicitOfferCharacteristic)!
    .onSet((value) => {
      const raw = fromB64(value);
      log("WRITE", "WebRTC Solicit Offer", describeTlv(raw));
      const sessionId = crypto.randomBytes(16);
      sessions.set(sessionId.toString("hex"), { createdAt: Date.now() });
      activeSessions.updateValue(sessions.size);
      const sdp = cannedSdpOffer(ip, BigInt(Date.now()));
      const candidates = buildIceCandidates([
        { candidate: `candidate:1 1 udp 2130706431 ${ip} 40000 typ host`, sdpMid: "0", sdpMLineIndex: 0 },
      ]);
      const entries = [
        { type: 1, data: sessionId },
        { type: 2, data: utf8(sdp) },
        ...candidates.map((c) => ({ type: 3, data: c })),
        { type: 4, data: uint8(SolicitOfferStatus.Success) },
      ];
      const response = encodeTlv8(entries).toString("base64");
      log("RESP", "WebRTC Solicit Offer", `session ${sessionId.toString("hex").slice(0, 8)}…, SDP offer ${sdp.length} chars`);
      return response;
    });

  // §4.18 — if this fires, the beta went all the way through SDP negotiation.
  webrtc
    .getCharacteristic(WebRTCProvideAnswerCharacteristic)!
    .onSet((value) => {
      const raw = fromB64(value);
      const entries = safeDecode(raw);
      const sid = entries.find((e) => e.type === 1)?.data;
      const answer = entries.find((e) => e.type === 2)?.data;
      log("WRITE", "WebRTC Provide Answer", describeTlv(raw));
      if (answer) {
        console.log("----- SDP ANSWER from controller -----");
        console.log(answer.toString("utf8"));
        console.log("---------------------------------------");
      }
      const known = sid !== undefined && sessions.has(sid.toString("hex"));
      const status = known
        ? WebRTCStreamingStatus.Success
        : WebRTCStreamingStatus.UnknownSessionIdentifier;
      return encodeTlv8([
        { type: 1, data: sid ?? Buffer.alloc(0) },
        { type: 2, data: uint8(status) },
      ]).toString("base64");
    });

  // §4.19 — End command tears a session down.
  webrtc
    .getCharacteristic(WebRTCStreamingControlCharacteristic)!
    .onSet((value) => {
      const raw = fromB64(value);
      const entries = safeDecode(raw);
      const sid = entries.find((e) => e.type === 1)?.data;
      log("WRITE", "WebRTC Streaming Control", describeTlv(raw));
      let status = WebRTCStreamingStatus.UnknownSessionIdentifier;
      if (sid !== undefined && sessions.delete(sid.toString("hex"))) {
        activeSessions.updateValue(sessions.size);
        status = WebRTCStreamingStatus.Success;
      }
      return encodeTlv8([
        { type: 1, data: sid ?? Buffer.alloc(0) },
        { type: 2, data: uint8(status) },
      ]).toString("base64");
    });

  for (const [char, name] of [
    [WebRTCReofferCharacteristic, "WebRTC Reoffer"],
    [WebRTCUpdateSessionCharacteristic, "WebRTC Update Session"],
  ] as const) {
    webrtc.getCharacteristic(char)!.onSet((value) => {
      const raw = fromB64(value);
      const sid = safeDecode(raw).find((e) => e.type === 1)?.data;
      log("WRITE", name, describeTlv(raw));
      return encodeTlv8([
        { type: 1, data: sid ?? Buffer.alloc(0) },
        { type: 2, data: uint8(WebRTCStreamingStatus.Success) },
      ]).toString("base64");
    });
  }
  activeSessions.updateValue(0);
  accessory.addService(webrtc);

  // --- optional legacy CameraController ------------------------------------
  // Comparison arm: does the Home app need a classic RTP streaming service
  // to treat the accessory as a camera at all? (Stub: 1x1 JPEG snapshots,
  // stream requests logged then ignored.)
  if (withLegacy) {
    configureLegacyController(accessory);
    log("INFO", "legacy CameraController attached (--with-legacy)");
  }

  accessory.publish({
    username: identity.username,
    pincode: PINCODE,
    category: Categories.IP_CAMERA,
    setupID: identity.setupID,
    addIdentifyingMaterial: true,
  });

  console.log("");
  console.log("==========================================================");
  console.log("  HKSV v2 probe — pair it from the Home app on the tvOS 27");
  console.log("  beta network, then watch this log.");
  console.log("");
  qrcode.generate(accessory.setupURI(), { small: true });
  console.log(`  PIN: ${PINCODE}   (identity: ${identity.username})`);
  console.log(`  Mode: ${withLegacy ? "new services + legacy controller" : "new services only"}`);
  console.log("==========================================================");
  console.log("");

  const shutdown = () => {
    console.log("\n[probe] shutting down…");
    accessory.unpublish();
    process.exit(0);
  };
  process.on("SIGINT", shutdown);
  process.on("SIGTERM", shutdown);
}

function safeDecode(buf: Buffer) {
  try {
    return decodeTlv8(buf);
  } catch {
    return [];
  }
}

/** Minimal legacy camera controller: valid snapshots, no real streaming. */
function configureLegacyController(accessory: Accessory): void {
  /* eslint-disable @typescript-eslint/no-require-imports */
  const {
    CameraController,
    H264Level,
    H264Profile,
    SRTPCryptoSuites,
    AudioStreamingCodecType,
    AudioStreamingSamplerate,
  } = require("@homebridge/hap-nodejs");
  // 1x1 black JPEG — enough for the Home app's snapshot probe.
  const TINY_JPEG = Buffer.from(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHR" +
      "ofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAARCAABAAEDASIAAhEB" +
      "AxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAFQEBAQAAAAAAAAAAAA" +
      "AAAAAAAAX/2gAMAwEAAhEDEQA/AKpgA//Z",
    "base64",
  );
  const controller = new CameraController({
    cameraStreamCount: 1,
    delegate: {
      handleSnapshotRequest(_req: unknown, cb: (e?: Error, b?: Buffer) => void) {
        log("READ", "legacy snapshot");
        cb(undefined, TINY_JPEG);
      },
      prepareStream(_req: unknown, cb: (e?: Error) => void) {
        log("WRITE", "legacy prepareStream (ignored)");
        cb(new Error("probe: no media"));
      },
      handleStreamRequest(_req: unknown, cb: (e?: Error) => void) {
        log("WRITE", "legacy handleStreamRequest (ignored)");
        cb(new Error("probe: no media"));
      },
    },
    streamingOptions: {
      supportedCryptoSuites: [SRTPCryptoSuites.AES_CM_128_HMAC_SHA1_80],
      video: {
        codec: {
          profiles: [H264Profile.MAIN],
          levels: [H264Level.LEVEL4_0],
        },
        resolutions: [
          [1920, 1080, 30],
          [1280, 720, 30],
          [640, 360, 30],
          [320, 240, 15],
        ],
      },
      audio: {
        twoWayAudio: false,
        codecs: [
          {
            type: AudioStreamingCodecType.AAC_ELD,
            samplerate: AudioStreamingSamplerate.KHZ_16,
          },
        ],
      },
    },
  });
  accessory.configureController(controller);
}

main();
