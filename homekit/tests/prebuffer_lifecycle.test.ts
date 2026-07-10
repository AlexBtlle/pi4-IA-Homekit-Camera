import { describe, expect, test } from "vitest";
import { Prebuffer } from "../src/prebuffer";

// ffmpeg lifecycle races. The close/data handlers of a SUPERSEDED process
// (killed by stop(), then start() spawned a new one before 'close' fired —
// 'close' is always async) must not touch the live instance: the old guard
// only checked `running`, so a quick HKSV disarm→re-arm let the dead
// process reset the live parser and schedule a second spawn — two ffmpeg
// feeding one parser, corrupt fragments. Private fields via `as any`.

function make(): any {
  return new Prebuffer("rtsp://127.0.0.1:8554/camera") as any;
}

function box(type: string, payload = 0): Buffer {
  const b = Buffer.alloc(8 + payload);
  b.writeUInt32BE(8 + payload, 0);
  b.write(type, 4, "ascii");
  return b;
}

describe("prebuffer ffmpeg lifecycle", () => {
  test("close of a superseded ffmpeg neither restarts nor resets", () => {
    const pb = make();
    const oldFf = { kill: () => {} };
    const newFf = { kill: () => {} };
    pb.running = true;
    pb.ff = newFf; // start() already spawned a replacement
    pb.pendingMoof = Buffer.from("moof-in-flight");

    pb.onFfClose(oldFf, 137); // the SIGKILLed old process finally closes

    expect(pb.ff).toBe(newFf); // live instance untouched
    expect(pb.pendingMoof).toBeDefined(); // parse state not reset
    expect(pb.restartTimer).toBeUndefined(); // no second spawn scheduled
  });

  test("close of the current ffmpeg while running schedules a restart", () => {
    const pb = make();
    const ff = { kill: () => {} };
    pb.running = true;
    pb.ff = ff;

    pb.onFfClose(ff, 1);

    expect(pb.ff).toBeUndefined();
    expect(pb.restartTimer).toBeDefined();
    pb.stop(); // clear the pending timer so no real ffmpeg spawns after the test
  });

  test("late stdout from a superseded ffmpeg is ignored", () => {
    const pb = make();
    const oldFf = { kill: () => {} };
    pb.ff = { kill: () => {} }; // a newer process owns the parser
    pb.lastActivity = 12345;

    // Garbage that would throw in the parser if it were consumed.
    pb.onFfData(oldFf, Buffer.from("this is not an mp4 box, size is huge"));

    expect(pb.lastActivity).toBe(12345); // not even counted as activity
  });

  test("stdout from the current ffmpeg is parsed into the init segment", () => {
    const pb = make();
    const ff = { kill: () => {} };
    pb.ff = ff;

    pb.onFfData(ff, Buffer.concat([box("ftyp"), box("moov")]));

    expect(pb.initSegment).toBeDefined();
    expect(pb.lastActivity).toBeGreaterThan(0);
  });

  test("resetParseState drops the cached init segment", () => {
    // Kept across reconnects before — stale SPS/PPS after a camera
    // restart with new encoder params made every new recording unreadable.
    const pb = make();
    pb.initSegment = Buffer.from("stale moov");
    pb.resetParseState();
    expect(pb.initSegment).toBeUndefined();
  });

  test("an aborted init waiter leaves the waiters array", async () => {
    const pb = make();
    const ac = new AbortController();
    const p = pb.getInit(ac.signal);
    expect(pb.initWaiters).toHaveLength(1);

    ac.abort();
    await expect(p).rejects.toThrow("aborted");
    expect(pb.initWaiters).toHaveLength(0); // used to linger forever
  });
});
