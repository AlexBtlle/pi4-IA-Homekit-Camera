import { describe, expect, test } from "vitest";

import {
  buildAudioStreamTiers,
  buildCameraCapabilities,
  buildVideoStreamTiers,
  cannedSdpOffer,
  defaultTiers2K,
} from "../src/hks-v2/payloads";
import { VideoCodec } from "../src/hks-v2/spec";
import {
  decodeTlv8,
  encodeTlv8,
  tlvGet,
  uint16,
  uint32,
  uint8,
  utf8,
} from "../src/hks-v2/tlv8";

describe("vendored TLV8 codec", () => {
  test("round-trips simple entries", () => {
    const buf = encodeTlv8([
      { type: 1, data: uint8(42) },
      { type: 2, data: utf8("hello") },
      { type: 3, data: uint32(70_000) },
    ]);
    const entries = decodeTlv8(buf);
    expect(entries).toHaveLength(3);
    expect(tlvGet(entries, 1)![0]).toBe(42);
    expect(tlvGet(entries, 2)!.toString()).toBe("hello");
    expect(tlvGet(entries, 3)!.readUInt32LE()).toBe(70_000);
  });

  test("fragments values over 255 bytes and merges them back", () => {
    const big = Buffer.alloc(600, 0xab);
    const buf = encodeTlv8([{ type: 7, data: big }]);
    // 255 + 255 + 90 → three fragments on the wire
    expect(buf.length).toBe(600 + 3 * 2);
    const entries = decodeTlv8(buf);
    expect(entries).toHaveLength(1);
    expect(entries[0].data.equals(big)).toBe(true);
  });

  test("a value of exactly 255 bytes does not swallow the next entry", () => {
    const exact = Buffer.alloc(255, 0x01);
    // Encoder must emit a zero-length continuation so the decoder's
    // 255-merge rule terminates correctly before an unrelated type.
    const buf = Buffer.concat([
      encodeTlv8([{ type: 5, data: exact }]),
      encodeTlv8([{ type: 5, data: Buffer.alloc(0) }]), // explicit terminator
      encodeTlv8([{ type: 6, data: uint8(9) }]),
    ]);
    const entries = decodeTlv8(buf);
    expect(entries[0].data.length).toBe(255);
    expect(tlvGet(entries, 6)![0]).toBe(9);
  });

  test("same-type list items separated by 0x00 stay distinct", () => {
    const buf = Buffer.concat([
      encodeTlv8([{ type: 3, data: uint16(100) }]),
      Buffer.from([0x00, 0x00]),
      encodeTlv8([{ type: 3, data: uint16(200) }]),
    ]);
    const items = decodeTlv8(buf).filter((e) => e.type === 3);
    expect(items).toHaveLength(2);
    expect(items[0].data.readUInt16LE()).toBe(100);
    expect(items[1].data.readUInt16LE()).toBe(200);
  });

  test("rejects truncated input", () => {
    expect(() => decodeTlv8(Buffer.from([1, 5, 0xaa]))).toThrow(/truncated/);
  });
});

describe("spec payload builders", () => {
  const sensorUuid = Buffer.alloc(16, 0x42);

  test("Camera Capabilities carries version, sensor and 3 stream capabilities", () => {
    const buf = buildCameraCapabilities(sensorUuid, 4608, 2592, defaultTiers2K());
    const top = decodeTlv8(buf);
    expect(tlvGet(top, 1)![0]).toBe(1); // Version
    const sensors = decodeTlv8(tlvGet(top, 2)!);
    const cfg = decodeTlv8(tlvGet(sensors, 1)!);
    const dims = decodeTlv8(tlvGet(cfg, 1)!);
    expect(tlvGet(dims, 1)!.readUInt16LE()).toBe(4608);
    expect(tlvGet(dims, 2)!.readUInt16LE()).toBe(2592);
    expect(tlvGet(cfg, 2)!.equals(sensorUuid)).toBe(true);
    const caps = cfg.filter((e) => e.type === 5);
    expect(caps).toHaveLength(3);
    // High tier: 2560x1440@30, 2800/3000 kbps
    const high = decodeTlv8(caps[0].data);
    expect(tlvGet(high, 3)!.readUInt16LE()).toBe(2560);
    expect(tlvGet(high, 4)!.readUInt16LE()).toBe(1440);
    expect(tlvGet(high, 5)![0]).toBe(30);
    expect(tlvGet(high, 6)!.readUInt32LE()).toBe(2800);
    expect(tlvGet(high, 7)!.readUInt32LE()).toBe(3000);
  });

  test("video tiers advertise H.265 with the spec's 2K ladder", () => {
    const buf = buildVideoStreamTiers(VideoCodec.H265, 96, defaultTiers2K());
    const top = decodeTlv8(buf);
    expect(tlvGet(top, 1)![0]).toBe(2); // codec enum: 2 = H.265
    expect(tlvGet(top, 2)![0]).toBe(96);
    const tiers = top.filter((e) => e.type === 3).map((e) => decodeTlv8(e.data));
    expect(tiers).toHaveLength(3);
    const widths = tiers.map((t) => tlvGet(t, 4)!.readUInt16LE());
    expect(widths).toEqual([2560, 1920, 640]);
    // Low tier runs at 15 fps per the spec's ladder
    expect(tlvGet(tiers[2], 6)![0]).toBe(15);
  });

  test("audio tiers advertise a single Opus 48 kHz mono tier", () => {
    const top = decodeTlv8(buildAudioStreamTiers(97));
    expect(tlvGet(top, 1)![0]).toBe(3); // codec enum: 3 = Opus
    const tiers = top.filter((e) => e.type === 3);
    expect(tiers).toHaveLength(1); // exactly one allowed by the spec
    const tier = decodeTlv8(tiers[0].data);
    expect(tier.find((e) => e.type === 3)!.data[0]).toBe(4); // 48 kHz
    expect(tier.find((e) => e.type === 5)!.data[0]).toBe(20); // 20 ms packets
    expect(tier.find((e) => e.type === 6)!.data[0]).toBe(1); // mono
  });

  test("canned SDP offer is sendonly H.265 + Opus", () => {
    const sdp = cannedSdpOffer("192.168.1.50", 1234n);
    expect(sdp).toContain("m=video");
    expect(sdp).toContain("H265/90000");
    expect(sdp).toContain("opus/48000");
    expect(sdp).toContain("a=sendonly");
    expect(sdp.endsWith("\r\n")).toBe(true);
  });
});
