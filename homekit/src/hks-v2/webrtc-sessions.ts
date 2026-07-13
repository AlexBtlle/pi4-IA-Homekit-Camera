/**
 * WebRTC session book-keeping for the Camera WebRTC Stream Management
 * service (§3.7) — the piece between the HAP characteristics and the media
 * engine. Protocol-only: TLV parsing/encoding lives in the service wiring,
 * media lives behind WebRtcEngine.
 *
 * The spec requires ≥ 6 simultaneous sessions; the cap here protects the
 * Pi from a runaway controller, it is not a target to reach.
 */
import crypto from "crypto";

import { EngineSession, WebRtcEngine } from "./webrtc-engine";
import { WebRTCStreamingStatus } from "./spec";

export interface SolicitedOffer {
  /** 16-byte session identifier (the spec exchanges it as opaque data). */
  sessionId: Buffer;
  sdpOffer: string;
}

interface SessionEntry {
  engine: EngineSession;
  tierSrc: string;
  answered: boolean;
}

export class WebRTCSessionManager {
  private readonly sessions = new Map<string, SessionEntry>();
  /** Fired whenever the active-session count changes (drives the
   *  WebRTC Number of Active Sessions characteristic's Notify). */
  onCountChanged?: (count: number) => void;

  constructor(
    private readonly engine: WebRtcEngine,
    private readonly maxSessions = 6,
  ) {}

  get count(): number {
    return this.sessions.size;
  }

  /**
   * §4.17 — controller solicited an offer. Opens an engine session on the
   * given stream source and returns the offer to embed in the
   * write-response. Throws on engine failure or when the cap is reached
   * (caller maps that to Status=Error / Busy).
   */
  async solicitOffer(tierSrc: string): Promise<SolicitedOffer> {
    if (this.sessions.size >= this.maxSessions) {
      throw new BusyError(`session cap reached (${this.maxSessions})`);
    }
    const engine = await this.engine.solicit(tierSrc);
    const sessionId = crypto.randomBytes(16);
    const key = sessionId.toString("hex");
    engine.onClosed = (reason) => {
      if (this.sessions.delete(key)) {
        console.log(`[webrtc] session ${key.slice(0, 8)}… dropped (${reason})`);
        this.onCountChanged?.(this.sessions.size);
      }
    };
    this.sessions.set(key, { engine, tierSrc, answered: false });
    this.onCountChanged?.(this.sessions.size);
    console.log(
      `[webrtc] session ${key.slice(0, 8)}… solicited on ${tierSrc} ` +
        `(${this.sessions.size}/${this.maxSessions})`,
    );
    return { sessionId, sdpOffer: engine.offer };
  }

  /** §4.18 — the controller's SDP answer. */
  async provideAnswer(
    sessionId: Buffer,
    sdpAnswer: string,
  ): Promise<WebRTCStreamingStatus> {
    const entry = this.sessions.get(sessionId.toString("hex"));
    if (!entry) return WebRTCStreamingStatus.UnknownSessionIdentifier;
    try {
      await entry.engine.provideAnswer(sdpAnswer);
      entry.answered = true;
      return WebRTCStreamingStatus.Success;
    } catch (err) {
      console.error(`[webrtc] answer failed: ${(err as Error).message}`);
      return WebRTCStreamingStatus.Error;
    }
  }

  /** §4.19 — Command 1 (End). */
  end(sessionId: Buffer): WebRTCStreamingStatus {
    const key = sessionId.toString("hex");
    const entry = this.sessions.get(key);
    if (!entry) return WebRTCStreamingStatus.UnknownSessionIdentifier;
    this.sessions.delete(key);
    entry.engine.onClosed = undefined; // we initiated: no double-count
    entry.engine.close();
    this.onCountChanged?.(this.sessions.size);
    console.log(`[webrtc] session ${key.slice(0, 8)}… ended by controller`);
    return WebRTCStreamingStatus.Success;
  }

  /** Service teardown (accessory shutdown). */
  closeAll(): void {
    for (const entry of this.sessions.values()) {
      entry.engine.onClosed = undefined;
      entry.engine.close();
    }
    this.sessions.clear();
    this.onCountChanged?.(0);
  }
}

export class BusyError extends Error {}
