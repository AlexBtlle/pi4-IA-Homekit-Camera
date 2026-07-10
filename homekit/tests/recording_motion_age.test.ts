import { describe, expect, test, vi } from "vitest";
import { RecordingDelegate } from "../src/recording";

// #57 investigation: correlate how long after the motion webhook the home
// hub actually opened the recording stream, to tell apart "the Pi failed to
// serve pre-roll" from "round-trip latency ate the whole pre-roll window
// before the hub ever asked for it".

function make(): RecordingDelegate {
  return new RecordingDelegate("rtsp://127.0.0.1:8554/camera");
}

describe("RecordingDelegate motion-age logging", () => {
  test("logs the age reported by motionAgeProvider and does not crash without one", async () => {
    const rd = make();
    const logs: string[] = [];
    const spy = vi.spyOn(console, "log").mockImplementation((m: string) => {
      logs.push(m);
    });

    // An already-aborted signal makes getInit() reject immediately (rather
    // than hang forever — see the getInit fix); the "stream started" log
    // still runs first since it happens synchronously before that await.
    const ac1 = new AbortController();
    ac1.abort();
    const gen1 = rd.handleRecordingStreamRequest(1, ac1.signal);
    await expect(gen1.next()).rejects.toThrow("aborted");
    expect(logs.some((l) => l.includes("stream 1 started"))).toBe(true);
    expect(logs.some((l) => l.includes("after motion trigger"))).toBe(false);

    // Provider wired: the elapsed ms is included verbatim.
    logs.length = 0;
    rd.motionAgeProvider = () => 4200;
    const ac2 = new AbortController();
    ac2.abort();
    const gen2 = rd.handleRecordingStreamRequest(2, ac2.signal);
    await expect(gen2.next()).rejects.toThrow("aborted");
    expect(logs.some((l) => l.includes("4200ms after motion trigger"))).toBe(true);

    spy.mockRestore();
  });
});
