import { describe, expect, test, vi } from "vitest";

import {
  EngineSession,
  WebRtcEngine,
} from "../src/hks-v2/webrtc-engine";
import { WebRTCStreamingStatus } from "../src/hks-v2/spec";
import {
  BusyError,
  WebRTCSessionManager,
} from "../src/hks-v2/webrtc-sessions";

class FakeSession implements EngineSession {
  onClosed?: (reason: string) => void;
  answers: string[] = [];
  closed = false;
  constructor(readonly offer: string) {}
  async provideAnswer(sdp: string): Promise<void> {
    this.answers.push(sdp);
  }
  close(): void {
    this.closed = true;
  }
  /** Simulate an engine-side drop (go2rtc died, peer disconnected…). */
  drop(reason: string): void {
    this.onClosed?.(reason);
  }
}

class FakeEngine implements WebRtcEngine {
  sessions: FakeSession[] = [];
  async solicit(src: string): Promise<EngineSession> {
    const s = new FakeSession(`v=0 offer-for-${src}`);
    this.sessions.push(s);
    return s;
  }
}

describe("WebRTCSessionManager", () => {
  test("solicit returns a 16-byte session id and the engine's offer", async () => {
    const mgr = new WebRTCSessionManager(new FakeEngine());
    const { sessionId, sdpOffer } = await mgr.solicitOffer("camera_hevc_high");
    expect(sessionId).toHaveLength(16);
    expect(sdpOffer).toBe("v=0 offer-for-camera_hevc_high");
    expect(mgr.count).toBe(1);
  });

  test("answer routes to the right session; unknown ids are rejected", async () => {
    const engine = new FakeEngine();
    const mgr = new WebRTCSessionManager(engine);
    const { sessionId } = await mgr.solicitOffer("src");
    expect(await mgr.provideAnswer(sessionId, "v=0 answer")).toBe(
      WebRTCStreamingStatus.Success,
    );
    expect(engine.sessions[0].answers).toEqual(["v=0 answer"]);
    expect(await mgr.provideAnswer(Buffer.alloc(16, 9), "v=0")).toBe(
      WebRTCStreamingStatus.UnknownSessionIdentifier,
    );
  });

  test("end closes the engine session and updates the count", async () => {
    const engine = new FakeEngine();
    const mgr = new WebRTCSessionManager(engine);
    const counts: number[] = [];
    mgr.onCountChanged = (n) => counts.push(n);
    const { sessionId } = await mgr.solicitOffer("src");
    expect(mgr.end(sessionId)).toBe(WebRTCStreamingStatus.Success);
    expect(engine.sessions[0].closed).toBe(true);
    expect(mgr.count).toBe(0);
    expect(counts).toEqual([1, 0]);
    expect(mgr.end(sessionId)).toBe(
      WebRTCStreamingStatus.UnknownSessionIdentifier,
    );
  });

  test("the spec's 6-session cap raises BusyError", async () => {
    const mgr = new WebRTCSessionManager(new FakeEngine(), 6);
    for (let i = 0; i < 6; i++) await mgr.solicitOffer("src");
    await expect(mgr.solicitOffer("src")).rejects.toBeInstanceOf(BusyError);
    expect(mgr.count).toBe(6);
  });

  test("an engine-side drop frees the slot exactly once", async () => {
    const engine = new FakeEngine();
    const mgr = new WebRTCSessionManager(engine);
    const counts: number[] = [];
    mgr.onCountChanged = (n) => counts.push(n);
    await mgr.solicitOffer("src");
    engine.sessions[0].drop("peer vanished");
    engine.sessions[0].drop("peer vanished"); // double event must not double-count
    expect(mgr.count).toBe(0);
    expect(counts).toEqual([1, 0]);
  });

  test("closeAll tears everything down silently", async () => {
    const engine = new FakeEngine();
    const mgr = new WebRTCSessionManager(engine);
    await mgr.solicitOffer("a");
    await mgr.solicitOffer("b");
    mgr.closeAll();
    expect(mgr.count).toBe(0);
    expect(engine.sessions.every((s) => s.closed)).toBe(true);
  });
});
