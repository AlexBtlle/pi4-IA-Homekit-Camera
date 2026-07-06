import { describe, it, expect } from "vitest";
import dgram from "dgram";
import { srtpHost } from "../src/streaming";

describe("srtpHost (#44, IPv6 beta)", () => {
  it("passes IPv4 addresses through untouched", () => {
    expect(srtpHost("192.168.1.24", false)).toBe("192.168.1.24");
  });

  it("brackets IPv6 literals for the ffmpeg URL", () => {
    // raw interpolation of fe80::1 into srtp://…:port is an invalid URL —
    // the exact silent-black-tile failure the audit flagged
    expect(srtpHost("fe80::aede:48ff:fe00:1122", true)).toBe(
      "[fe80::aede:48ff:fe00:1122]",
    );
    expect(`srtp://${srtpHost("2a01:cb00::1", true)}:52310?rtcpport=52311`).toBe(
      "srtp://[2a01:cb00::1]:52310?rtcpport=52311",
    );
  });
});

describe("udp6 port reservation (#44)", () => {
  it("binds a udp6 socket where the OS supports IPv6", async () => {
    // Environment-dependent: CI containers often run v4-only (EAFNOSUPPORT).
    // There the accessory fails the prepareStream loudly instead of serving
    // a black tile — which is the designed fallback, so a skip is honest.
    const outcome = await new Promise<string>((resolve) => {
      const s = dgram.createSocket("udp6");
      s.once("error", (e: NodeJS.ErrnoException) => resolve(e.code ?? "error"));
      s.bind(0, () => {
        const port = (s.address() as { port: number }).port;
        s.close(() => resolve(port > 0 ? "ok" : "bad-port"));
      });
    });
    if (outcome === "EAFNOSUPPORT") {
      return; // v4-only environment — nothing to assert
    }
    expect(outcome).toBe("ok");
  });
});
