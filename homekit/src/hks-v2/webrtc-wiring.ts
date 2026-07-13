/**
 * Binds the Camera WebRTC Stream Management service's characteristics to a
 * WebRTCSessionManager — the production version of the probe's stubs.
 * TLV layouts per §4.17-4.22; the write-response mechanism is hap-nodejs's
 * (an onSet handler's return value answers a write-response request).
 *
 * Open protocol questions left for field observation (logged loudly):
 * - Tier selection: §4.17's write carries only Options (SFrame) — no tier.
 *   Until the real controller shows how it picks one (SDP? Update Session?),
 *   every session opens on the configured default source (the High tier).
 * - SFrame: requested via Options; v1 answers without SFrame Configuration.
 */
import {
  CharacteristicValue,
  Service,
} from "@homebridge/hap-nodejs";

import {
  OfferOptionsTlv,
  SolicitOfferStatus,
  SolicitOfferTlv,
  WebRTCStreamingStatus,
} from "./spec";
import {
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
import { decodeTlv8, encodeTlv8, tlvGet, uint8, utf8 } from "./tlv8";
import { BusyError, WebRTCSessionManager } from "./webrtc-sessions";

export interface WebRTCWiringOptions {
  /** Advertised payloads, prebuilt with payloads.ts. */
  videoTiersPayload: Buffer;
  audioTiersPayload: Buffer;
  sensorUuid: Buffer;
  /** go2rtc stream source used for solicited sessions (High tier). */
  defaultSrc: string;
}

function fromB64(value: CharacteristicValue | null | undefined): Buffer {
  return Buffer.from(typeof value === "string" ? value : "", "base64");
}

export class WebRTCManagementWiring {
  readonly service: CameraWebRTCStreamManagementService;
  streamingEnabled = true;

  constructor(
    private readonly sessions: WebRTCSessionManager,
    private readonly opts: WebRTCWiringOptions,
    service?: CameraWebRTCStreamManagementService,
  ) {
    this.service = service ?? new CameraWebRTCStreamManagementService();
    this.wire();
  }

  private wire(): void {
    const svc: Service = this.service;

    svc
      .getCharacteristic(WebRTCSupportedVideoStreamTiersCharacteristic)!
      .updateValue(this.opts.videoTiersPayload.toString("base64"));
    svc
      .getCharacteristic(WebRTCSupportedAudioStreamTiersCharacteristic)!
      .updateValue(this.opts.audioTiersPayload.toString("base64"));
    svc
      .getCharacteristic(SensorUuidCharacteristic)!
      .updateValue(this.opts.sensorUuid.toString("base64"));
    svc
      .getCharacteristic(StreamingEnabledCharacteristic)!
      .onGet(() => this.streamingEnabled)
      .onSet((v) => {
        this.streamingEnabled = Boolean(v);
        console.log(`[webrtc] streaming enabled → ${this.streamingEnabled}`);
      })
      .updateValue(this.streamingEnabled);

    const activeSessions = svc.getCharacteristic(
      WebRTCNumberOfActiveSessionsCharacteristic,
    )!;
    activeSessions.updateValue(0);
    this.sessions.onCountChanged = (n) => activeSessions.updateValue(n);

    svc
      .getCharacteristic(WebRTCSolicitOfferCharacteristic)!
      .onSet(async (value) => this.handleSolicit(fromB64(value)));
    svc
      .getCharacteristic(WebRTCProvideAnswerCharacteristic)!
      .onSet(async (value) => this.handleAnswer(fromB64(value)));
    svc
      .getCharacteristic(WebRTCStreamingControlCharacteristic)!
      .onSet(async (value) => this.handleControl(fromB64(value)));

    // §4.21/§4.22 — renegotiation and SFrame key rotation: not implemented
    // in v1 (documented fallback: the controller reconnects). Status=Error
    // for a known session, UnknownSessionIdentifier otherwise.
    for (const [char, name] of [
      [WebRTCReofferCharacteristic, "Reoffer"],
      [WebRTCUpdateSessionCharacteristic, "Update Session"],
    ] as const) {
      svc.getCharacteristic(char)!.onSet(async (value) => {
        const entries = safeDecode(fromB64(value));
        const sid = tlvGet(entries, 1) ?? Buffer.alloc(0);
        console.warn(`[webrtc] ${name} not supported in v1 — returning Error`);
        return encodeTlv8([
          { type: 1, data: sid },
          { type: 2, data: uint8(WebRTCStreamingStatus.Error) },
        ]).toString("base64");
      });
    }
  }

  private async handleSolicit(raw: Buffer): Promise<string> {
    const entries = safeDecode(raw);
    const options = tlvGet(entries, SolicitOfferTlv.Options);
    const sframeRequested =
      options !== undefined &&
      tlvGet(safeDecode(options), OfferOptionsTlv.SFrameEnabled)?.[0] === 1;
    if (sframeRequested) {
      console.warn("[webrtc] controller requested SFrame — v1 answers without it");
    }
    if (!this.streamingEnabled) {
      console.log("[webrtc] solicit rejected: streaming disabled");
      return encodeTlv8([
        { type: SolicitOfferTlv.Status, data: uint8(SolicitOfferStatus.PrivacyModeActive) },
      ]).toString("base64");
    }
    try {
      const { sessionId, sdpOffer } = await this.sessions.solicitOffer(
        this.opts.defaultSrc,
      );
      return encodeTlv8([
        { type: SolicitOfferTlv.SessionIdentifier, data: sessionId },
        { type: SolicitOfferTlv.SdpOffer, data: utf8(sdpOffer) },
        { type: SolicitOfferTlv.Status, data: uint8(SolicitOfferStatus.Success) },
      ]).toString("base64");
    } catch (err) {
      const kind = err instanceof BusyError ? "busy" : "engine failure";
      console.error(`[webrtc] solicit failed (${kind}): ${(err as Error).message}`);
      return encodeTlv8([
        { type: SolicitOfferTlv.Status, data: uint8(SolicitOfferStatus.Error) },
      ]).toString("base64");
    }
  }

  private async handleAnswer(raw: Buffer): Promise<string> {
    const entries = safeDecode(raw);
    const sid = tlvGet(entries, 1) ?? Buffer.alloc(0);
    const answer = tlvGet(entries, 2);
    const status =
      answer === undefined
        ? WebRTCStreamingStatus.Error
        : await this.sessions.provideAnswer(sid, answer.toString("utf8"));
    return encodeTlv8([
      { type: 1, data: sid },
      { type: 2, data: uint8(status) },
    ]).toString("base64");
  }

  private async handleControl(raw: Buffer): Promise<string> {
    const entries = safeDecode(raw);
    const sid = tlvGet(entries, 1) ?? Buffer.alloc(0);
    const command = tlvGet(entries, 2)?.[0];
    let status: WebRTCStreamingStatus;
    if (command === 1) {
      status = this.sessions.end(sid);
    } else {
      console.warn(`[webrtc] unknown streaming-control command ${command}`);
      status = WebRTCStreamingStatus.Error;
    }
    return encodeTlv8([
      { type: 1, data: sid },
      { type: 2, data: uint8(status) },
    ]).toString("base64");
  }
}

function safeDecode(buf: Buffer) {
  try {
    return decodeTlv8(buf);
  } catch {
    return [];
  }
}
