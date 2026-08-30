import { describe, expect, test } from "vitest";

import { announceHealth, sameAddresses } from "../src/qrweb";
import { ipv4Addresses } from "../src/network";
import type { InterfaceReader } from "../src/network";

const reader = (value: Record<string, unknown[]>): InterfaceReader =>
  (() => value) as unknown as InterfaceReader;

describe("ipv4Addresses", () => {
  test("keeps non-loopback IPv4 only, sorted for comparison", () => {
    const read = reader({
      wlan0: [
        { address: "192.168.1.42", family: "IPv4", internal: false },
        { address: "fe80::1", family: "IPv6", internal: false },
      ],
      lo: [{ address: "127.0.0.1", family: "IPv4", internal: true }],
      eth0: [{ address: "10.0.0.7", family: "IPv4", internal: false }],
    });
    expect(ipv4Addresses(read)).toEqual(["10.0.0.7", "192.168.1.42"]);
  });
});

describe("sameAddresses", () => {
  test("compares sorted lists element-wise", () => {
    expect(sameAddresses(["10.0.0.7"], ["10.0.0.7"])).toBe(true);
    expect(sameAddresses(["10.0.0.7"], ["10.0.0.8"])).toBe(false);
    expect(sameAddresses(["10.0.0.7"], ["10.0.0.7", "10.0.0.8"])).toBe(false);
  });
});

describe("announceHealth", () => {
  test("crit when the host has no address at all", () => {
    expect(announceHealth(["192.168.1.42"], [])).toBe("crit");
    expect(announceHealth([], [])).toBe("crit");
  });

  test("muted when the announced set is unknown — nothing to claim", () => {
    expect(announceHealth([], ["192.168.1.42"])).toBe("muted");
  });

  test("ok while the announcement still matches reality", () => {
    expect(announceHealth(["192.168.1.42"], ["192.168.1.42"])).toBe("ok");
  });

  // The failure this signal exists for: the accessory was announced on an
  // address it no longer holds, so HomeKit cannot reach it while every other
  // probe on the page stays green (#66).
  test("warn once the address changed under a running accessory", () => {
    expect(announceHealth(["192.168.1.42"], ["192.168.1.55"])).toBe("warn");
  });

  test("warn when an interface appears or disappears", () => {
    expect(announceHealth(["192.168.1.42"], ["10.0.0.7", "192.168.1.42"])).toBe("warn");
    expect(announceHealth(["10.0.0.7", "192.168.1.42"], ["192.168.1.42"])).toBe("warn");
  });
});
