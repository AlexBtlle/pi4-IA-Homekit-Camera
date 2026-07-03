import { describe, expect, test } from "vitest";
import { Prebuffer } from "../src/prebuffer";

// checkStale() is the Node counterpart of the Python publisher's FIONREAD
// stall detector: an ffmpeg that is alive but silent must be killed so the
// existing close-handler restart logic can take over. Private fields are
// reached through `as any` — TS privacy is compile-time only.

function make(): any {
  return new Prebuffer("rtsp://127.0.0.1:8554/camera") as any;
}

describe("prebuffer stale watchdog", () => {
  test("silent-but-alive ffmpeg gets killed", () => {
    const pb = make();
    let killed: string | undefined;
    pb.running = true;
    pb.ff = { kill: (sig: string) => { killed = sig; } };
    pb.lastActivity = performance.now() - 60_000;
    pb.checkStale();
    expect(killed).toBe("SIGKILL");
  });

  test("recently active ffmpeg is left alone", () => {
    const pb = make();
    let killed = false;
    pb.running = true;
    pb.ff = { kill: () => { killed = true; } };
    pb.lastActivity = performance.now();
    pb.checkStale();
    expect(killed).toBe(false);
  });

  test("no-op while stopped or between restarts", () => {
    const pb = make();
    pb.running = false;
    pb.ff = undefined;
    pb.lastActivity = 0;
    expect(() => pb.checkStale()).not.toThrow();

    pb.running = true; // restarting: ff not yet respawned
    expect(() => pb.checkStale()).not.toThrow();
  });

  test("a fresh spawn gets the full grace period", () => {
    // lastActivity is reset at spawn time, so a cold ffmpeg (RTSP handshake
    // + first keyframe) is not killed before STALE_MS elapses
    const pb = make();
    let killed = false;
    pb.running = true;
    pb.ff = { kill: () => { killed = true; } };
    pb.lastActivity = performance.now() - ((Prebuffer as any).STALE_MS - 2_000);
    pb.checkStale();
    expect(killed).toBe(false);
  });
});
