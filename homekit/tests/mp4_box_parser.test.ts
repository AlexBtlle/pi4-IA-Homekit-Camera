import { describe, it, expect, beforeEach } from "vitest";
import { Mp4BoxParser } from "../src/prebuffer";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Minimal MP4 box: 4-byte big-endian size + 4-byte ASCII type + payload. */
function box(type: string, payloadSize = 0): Buffer {
  const size = 8 + payloadSize;
  const buf = Buffer.alloc(size);
  buf.writeUInt32BE(size, 0);
  buf.write(type, 4, "ascii");
  return buf;
}

/** Box with a 64-bit largesize header (size field = 1, then 8-byte actual size). */
function largeBox(type: string, payloadSize = 0): Buffer {
  const total = 16 + payloadSize; // 4 (size=1) + 4 (type) + 8 (largesize) + payload
  const buf = Buffer.alloc(total);
  buf.writeUInt32BE(1, 0);
  buf.write(type, 4, "ascii");
  buf.writeUInt32BE(0, 8);      // high 32 bits of largesize
  buf.writeUInt32BE(total, 12); // low 32 bits
  return buf;
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("Mp4BoxParser", () => {
  let parser: Mp4BoxParser;

  beforeEach(() => {
    parser = new Mp4BoxParser();
  });

  it("parses a single complete box", () => {
    const result = parser.push(box("ftyp", 4));
    expect(result).toHaveLength(1);
    expect(result[0].type).toBe("ftyp");
    expect(result[0].data.length).toBe(12);
  });

  it("returns empty array when buffer is too short for a header", () => {
    expect(parser.push(Buffer.alloc(4))).toHaveLength(0);
  });

  it("returns empty array when declared box size exceeds available data", () => {
    const full = box("moov", 8); // 16 bytes total
    expect(parser.push(full.subarray(0, 10))).toHaveLength(0);
  });

  it("accumulates partial data across multiple pushes", () => {
    const full = box("moov", 8); // 16 bytes
    expect(parser.push(full.subarray(0, 8))).toHaveLength(0);
    const result = parser.push(full.subarray(8));
    expect(result).toHaveLength(1);
    expect(result[0].type).toBe("moov");
  });

  it("parses multiple boxes delivered in one push", () => {
    const combined = Buffer.concat([box("ftyp"), box("moov"), box("moof")]);
    const result = parser.push(combined);
    expect(result.map((b) => b.type)).toEqual(["ftyp", "moov", "moof"]);
  });

  it("parses boxes split across three pushes", () => {
    const full = Buffer.concat([box("ftyp"), box("mdat")]);
    const [a, b, c] = [full.subarray(0, 5), full.subarray(5, 11), full.subarray(11)];
    expect(parser.push(a)).toHaveLength(0);
    expect(parser.push(b)).toHaveLength(1);
    const result = parser.push(c);
    expect(result).toHaveLength(1);
    expect(result[0].type).toBe("mdat");
  });

  it("handles a 64-bit largesize header", () => {
    const result = parser.push(largeBox("moof", 4));
    expect(result).toHaveLength(1);
    expect(result[0].type).toBe("moof");
    expect(result[0].data.length).toBe(20);
  });

  it("waits for full largesize header before parsing", () => {
    const full = largeBox("mdat"); // exactly 16 bytes
    // 12 bytes delivered — largesize not yet complete (needs 16 for header)
    expect(parser.push(full.subarray(0, 12))).toHaveLength(0);
    expect(parser.push(full.subarray(12))).toHaveLength(1);
  });

  it("preserves exact payload bytes", () => {
    const payload = Buffer.from([0xde, 0xad, 0xbe, 0xef]);
    const b = Buffer.concat([Buffer.alloc(8), payload]);
    b.writeUInt32BE(12, 0);
    b.write("mdat", 4, "ascii");
    const [result] = parser.push(b);
    expect(result.data.subarray(8)).toEqual(payload);
  });

  it("handles moof immediately followed by mdat", () => {
    const buf = Buffer.concat([box("moof", 4), box("mdat", 8)]);
    const result = parser.push(buf);
    expect(result).toHaveLength(2);
    expect(result[0].type).toBe("moof");
    expect(result[1].type).toBe("mdat");
  });

  it("continues parsing after a previous complete sequence", () => {
    parser.push(box("ftyp"));
    const result = parser.push(box("moov"));
    expect(result).toHaveLength(1);
    expect(result[0].type).toBe("moov");
  });

  // Corrupt input (#36): the old parser silently stopped consuming and let
  // its buffer grow unbounded on every chunk — throwing lets the Prebuffer
  // recycle ffmpeg instead.

  it("throws on a zero-size box (extends-to-EOF, nonsensical mid-stream)", () => {
    const b = box("mdat", 4);
    b.writeUInt32BE(0, 0);
    expect(() => parser.push(b)).toThrow(/corrupt MP4 box/);
  });

  it("throws on an absurdly large box size", () => {
    const b = Buffer.alloc(8);
    b.writeUInt32BE(0x7fffffff, 0);
    b.write("mdat", 4, "ascii");
    expect(() => parser.push(b)).toThrow(/corrupt MP4 box/);
  });

  it("throws on a size smaller than its own header", () => {
    const b = Buffer.alloc(8);
    b.writeUInt32BE(3, 0);
    b.write("mdat", 4, "ascii");
    expect(() => parser.push(b)).toThrow(/corrupt MP4 box/);
  });

  it("throws on a corrupt 64-bit largesize", () => {
    const b = largeBox("mdat");
    b.writeUInt32BE(0x10, 8); // high 32 bits → size ≈ 64 GB
    expect(() => parser.push(b)).toThrow(/corrupt MP4 box/);
  });
});
