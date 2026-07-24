import { EventEmitter } from "events";
import { beforeEach, describe, expect, test, vi } from "vitest";

// #38 field bug: when the live ffmpeg dies mid-session, iOS keeps a frozen
// tile until the user closes it by hand — UNLESS the accessory calls
// forceStopStreamingSession. A deliberate stop (HAP STOP → SIGKILL → close
// with code null) must NOT trigger it: the session was already removed, and
// telling the controller to stop a stream it just stopped is a glitch loop.
// This is the crash-recovery path of streaming.ts startStream()'s close
// handler — untested until now despite being a real field fix.

/** Fake ChildProcess: enough surface for startStream (spawn/stderr/close). */
class FakeFfmpeg extends EventEmitter {
  stderr = new EventEmitter();
  killed = false;
  kill(): boolean {
    this.killed = true;
    return true;
  }
}

let lastSpawned: FakeFfmpeg;
vi.mock("child_process", () => ({
  spawn: vi.fn(() => {
    lastSpawned = new FakeFfmpeg();
    return lastSpawned;
  }),
}));

import { StreamingDelegate } from "../src/streaming";
import { StreamRequestTypes } from "@homebridge/hap-nodejs";

const SESSION = "11111111-2222-3333-4444-555555555555";

function seededDelegate() {
  const sd = new StreamingDelegate(
    "rtsp://127.0.0.1:8554/camera",
    { get: async () => Buffer.alloc(0) } as never,
  );
  const forceStop = vi.fn();
  sd.controller = { forceStopStreamingSession: forceStop } as never;
  // Same private-seeding style as the prebuffer tests: spawning a real
  // prepareStream needs UDP sockets; the crash path under test starts after.
  (sd as unknown as { pendingSessions: Map<string, unknown> }).pendingSessions.set(
    SESSION,
    {
      address: "192.168.1.10",
      ipv6: false,
      videoPort: 50000,
      videoReturnPort: 50001,
      videoSSRC: 1,
      videoSRTP: Buffer.alloc(30),
    },
  );
  return { sd, forceStop };
}

function startRequest() {
  return {
    type: StreamRequestTypes.START,
    sessionID: SESSION,
    video: { width: 1920, height: 1080, fps: 30, max_bit_rate: 2000, pt: 99, mtu: 1316 },
  } as never;
}

beforeEach(() => vi.clearAllMocks());

describe("streaming crash recovery (#38)", () => {
  test("unexpected ffmpeg death tells the controller to stop the session", () => {
    const { sd, forceStop } = seededDelegate();
    sd.handleStreamRequest(startRequest(), () => {});
    lastSpawned.emit("close", 1); // ffmpeg died mid-session
    expect(forceStop).toHaveBeenCalledWith(SESSION);
    const ongoing = (sd as unknown as { ongoingSessions: Map<string, unknown> })
      .ongoingSessions;
    expect(ongoing.has(SESSION)).toBe(false);
  });

  test("a deliberate STOP does not re-notify the controller", () => {
    const { sd, forceStop } = seededDelegate();
    sd.handleStreamRequest(startRequest(), () => {});
    const ff = lastSpawned;
    // HAP-initiated stop: session removed first, then the SIGKILLed process
    // closes with code null — the wasOngoing guard must swallow it.
    sd.handleStreamRequest(
      { type: StreamRequestTypes.STOP, sessionID: SESSION } as never,
      () => {},
    );
    expect(ff.killed).toBe(true);
    ff.emit("close", null);
    expect(forceStop).not.toHaveBeenCalled();
  });

  test("a clean exit (code 0) after teardown stays silent too", () => {
    const { sd, forceStop } = seededDelegate();
    sd.handleStreamRequest(startRequest(), () => {});
    lastSpawned.emit("close", 0);
    expect(forceStop).not.toHaveBeenCalled();
  });
});
