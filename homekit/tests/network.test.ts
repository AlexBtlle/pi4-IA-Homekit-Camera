import { describe, expect, test, vi } from "vitest";

import { hasUsableIPv4, waitForIPv4 } from "../src/network";
import type { InterfaceReader } from "../src/network";

/** Minimal os.networkInterfaces() shapes — only the fields the guard reads. */
const LOOPBACK = { address: "127.0.0.1", family: "IPv4", internal: true };
const WIFI_V4 = { address: "192.168.1.42", family: "IPv4", internal: false };
const WIFI_V6 = { address: "fe80::1", family: "IPv6", internal: false };

const reader = (value: Record<string, unknown[]>): InterfaceReader =>
  (() => value) as unknown as InterfaceReader;

describe("hasUsableIPv4", () => {
  test("true once a non-loopback IPv4 exists", () => {
    expect(hasUsableIPv4(reader({ lo: [LOOPBACK], wlan0: [WIFI_V4] }))).toBe(true);
  });

  test("false with loopback only — the state that broke #65", () => {
    expect(hasUsableIPv4(reader({ lo: [LOOPBACK] }))).toBe(false);
  });

  test("false with a link-local IPv6 but no IPv4", () => {
    // wlan0 is up and has an address, yet HAP still has nothing to announce
    // on: this is exactly the boot window the guard has to catch.
    expect(hasUsableIPv4(reader({ lo: [LOOPBACK], wlan0: [WIFI_V6] }))).toBe(false);
  });

  test("false when the interface exists but holds no address yet", () => {
    expect(hasUsableIPv4(reader({ wlan0: [] }))).toBe(false);
  });

  test("tolerates an interface reported as undefined", () => {
    const read = (() => ({ wlan0: undefined })) as unknown as InterfaceReader;
    expect(hasUsableIPv4(read)).toBe(false);
  });
});

describe("waitForIPv4", () => {
  test("returns immediately when the address is already there", async () => {
    const read = vi.fn(() => ({ wlan0: [WIFI_V4] })) as unknown as InterfaceReader;
    await expect(waitForIPv4({ read, timeoutMs: 1000, pollMs: 50 })).resolves.toBe(true);
    // One probe, no polling loop: startup must not be delayed on a healthy boot.
    expect(read).toHaveBeenCalledTimes(1);
  });

  test("returns true as soon as the address appears mid-wait", async () => {
    let calls = 0;
    const read = (() => {
      calls += 1;
      return calls >= 3 ? { wlan0: [WIFI_V4] } : { lo: [LOOPBACK] };
    }) as unknown as InterfaceReader;
    await expect(waitForIPv4({ read, timeoutMs: 2000, pollMs: 1 })).resolves.toBe(true);
    expect(calls).toBe(3);
  });

  test("returns false on timeout so the caller can exit and let systemd retry", async () => {
    const read = reader({ lo: [LOOPBACK] });
    await expect(waitForIPv4({ read, timeoutMs: 30, pollMs: 1 })).resolves.toBe(false);
  });
});
