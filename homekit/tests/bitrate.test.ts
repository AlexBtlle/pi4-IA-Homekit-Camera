import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { BitrateGovernor, computeTargetKbps, MIN_KBPS } from "../src/bitrate";

const CONFIGURED = 8000; // kbps

describe("computeTargetKbps", () => {
  test("no sessions → configured (full quality for recordings)", () => {
    expect(computeTargetKbps([], CONFIGURED)).toBe(CONFIGURED);
  });

  test("remote viewer → its negotiated cap", () => {
    expect(computeTargetKbps([2000], CONFIGURED)).toBe(2000);
  });

  test("several viewers → the most demanding one wins", () => {
    expect(computeTargetKbps([2000, 4000], CONFIGURED)).toBe(4000);
  });

  test("clamped to the floor", () => {
    expect(computeTargetKbps([300], CONFIGURED)).toBe(MIN_KBPS);
  });

  test("clamped to the configured ceiling", () => {
    expect(computeTargetKbps([50_000], CONFIGURED)).toBe(CONFIGURED);
  });
});

describe("BitrateGovernor", () => {
  let sent: number[];
  let gov: BitrateGovernor;

  beforeEach(() => {
    vi.useFakeTimers();
    sent = [];
    gov = new BitrateGovernor((kbps) => sent.push(kbps), CONFIGURED);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  test("lowering is immediate (a viewer is stuttering)", () => {
    gov.setSession("a", 2000);
    expect(sent).toEqual([2000]);
  });

  test("raising is debounced", () => {
    gov.setSession("a", 2000);
    gov.clearSession("a");
    expect(sent).toEqual([2000]); // nothing yet
    vi.advanceTimersByTime(BitrateGovernor.RAISE_DELAY_MS);
    expect(sent).toEqual([2000, CONFIGURED]);
  });

  test("quick close/reopen does not yo-yo the encoder", () => {
    gov.setSession("a", 2000);
    gov.clearSession("a"); // raise scheduled…
    vi.advanceTimersByTime(3000);
    gov.setSession("b", 2000); // …but a new remote viewer arrives
    vi.advanceTimersByTime(BitrateGovernor.RAISE_DELAY_MS * 2);
    expect(sent).toEqual([2000]); // still at 2000, no bump in between
  });

  test("RECONFIGURE to a lower cap acts immediately", () => {
    gov.setSession("a", 5000);
    gov.setSession("a", 2000); // network degraded mid-stream
    expect(sent).toEqual([5000, 2000]);
  });

  test("a LAN viewer at the ceiling changes nothing", () => {
    gov.setSession("a", CONFIGURED);
    expect(sent).toEqual([]);
  });

  test("sessions without a negotiated bitrate are ignored", () => {
    gov.setSession("a", undefined);
    gov.setSession("b", 0);
    expect(sent).toEqual([]);
  });
});
