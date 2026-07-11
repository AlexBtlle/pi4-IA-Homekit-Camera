import { describe, expect, test, vi } from "vitest";
import { RecordingDelegate } from "../src/recording";
import type { CameraRecordingConfiguration } from "@homebridge/hap-nodejs";

// #57 root cause: the home hub arms recording LAZILY — updateRecordingActive
// (true) lands at the exact second the motion stream opens, (false) when the
// clip ends. A prebuffer gated on Active therefore never captures the
// pre-motion seconds ("serving 0 pre-roll — ring holds 0"). Its lifetime must
// follow the recording CONFIGURATION (enabled/disabled in the Home app),
// which HAP-NodeJS replays at boot and clears on disable.

function make() {
  const rd = new RecordingDelegate("rtsp://127.0.0.1:8554/camera");
  const prebuffer = { start: vi.fn(), stop: vi.fn() };
  (rd as any).prebuffer = prebuffer;
  return { rd, prebuffer };
}

const config = { prebufferLength: 4000 } as CameraRecordingConfiguration;

describe("RecordingDelegate prebuffer lifecycle", () => {
  test("a selected configuration starts the prebuffer; removing it stops it", () => {
    const { rd, prebuffer } = make();
    rd.updateRecordingConfiguration(config);
    expect(prebuffer.start).toHaveBeenCalledTimes(1);
    expect(prebuffer.stop).not.toHaveBeenCalled();

    rd.updateRecordingConfiguration(undefined); // recording disabled in Home
    expect(prebuffer.stop).toHaveBeenCalledTimes(1);
  });

  test("disarming does NOT stop the prebuffer (lazy-arming hub)", () => {
    const { rd, prebuffer } = make();
    rd.updateRecordingConfiguration(config); // enabled in the Home app
    rd.updateRecordingActive(true); // hub arms at the motion event…
    rd.updateRecordingActive(false); // …and disarms right after the clip
    expect(prebuffer.stop).not.toHaveBeenCalled(); // the ring must stay warm
    expect(rd.recordingActive).toBe(false); // dashboard state still tracked
  });

  test("arming still starts the prebuffer as a belt-and-braces", () => {
    const { rd, prebuffer } = make();
    rd.updateRecordingActive(true); // no configuration event seen (edge case)
    expect(prebuffer.start).toHaveBeenCalledTimes(1);
  });
});
