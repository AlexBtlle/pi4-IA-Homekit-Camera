/**
 * Vendored TLV8 codec (HAP type-length-value, 1-byte type + 1-byte length).
 *
 * hap-nodejs has one internally (dist/lib/util/tlv.js) but does not export
 * it — importing that path would couple us to an internal layout that may
 * move on any version bump. ~100 lines is cheaper to own (#59 Volet 2).
 *
 * Rules implemented (HAP spec R17 §18.1):
 * - Values longer than 255 bytes are split into consecutive fragments of
 *   the same type; a decoder merges adjacent same-type entries when the
 *   previous fragment was exactly 255 bytes long.
 * - Repeated items of the same type (lists) are separated by a zero-length
 *   TLV of type 0x00 so they cannot be mistaken for fragments.
 */

export interface TlvEntry {
  type: number;
  data: Buffer;
}

/** Encode one entry, fragmenting values > 255 bytes. */
function encodeEntry(type: number, data: Buffer): Buffer {
  if (data.length === 0) {
    return Buffer.from([type, 0]);
  }
  const chunks: Buffer[] = [];
  for (let off = 0; off < data.length; off += 255) {
    const chunk = data.subarray(off, off + 255);
    chunks.push(Buffer.from([type, chunk.length]), chunk);
  }
  return Buffer.concat(chunks);
}

/** Encode a sequence of entries in order (repeats allowed). */
export function encodeTlv8(entries: TlvEntry[]): Buffer {
  return Buffer.concat(entries.map((e) => encodeEntry(e.type, e.data)));
}

/** Zero-length separator used between same-type list items. */
export const SEPARATOR: TlvEntry = { type: 0x00, data: Buffer.alloc(0) };

/**
 * Join already-encoded TLV8 items with 0x00 separators — the convention for
 * "Repeated. A list of X TLV8s" fields throughout the HKSV spec.
 */
export function joinTlv8List(items: Buffer[]): Buffer {
  const parts: Buffer[] = [];
  items.forEach((item, i) => {
    if (i > 0) parts.push(Buffer.from([0x00, 0x00]));
    parts.push(item);
  });
  return Buffer.concat(parts);
}

/**
 * Decode a TLV8 buffer into an ordered entry list. Adjacent same-type
 * entries merge only when the previous fragment was exactly 255 bytes
 * (fragmentation); zero-length type-0 entries (list separators) are kept
 * so callers can split lists, they just carry no data.
 */
export function decodeTlv8(buf: Buffer): TlvEntry[] {
  const out: TlvEntry[] = [];
  let i = 0;
  let prevLen = -1;
  while (i < buf.length) {
    if (i + 2 > buf.length) {
      throw new Error(`truncated TLV header at offset ${i}`);
    }
    const type = buf[i];
    const len = buf[i + 1];
    if (i + 2 + len > buf.length) {
      throw new Error(`truncated TLV value at offset ${i} (type ${type})`);
    }
    const data = buf.subarray(i + 2, i + 2 + len);
    const prev = out[out.length - 1];
    if (prev !== undefined && prev.type === type && prevLen === 255) {
      prev.data = Buffer.concat([prev.data, data]); // fragment continuation
    } else {
      out.push({ type, data: Buffer.from(data) });
    }
    prevLen = len;
    i += 2 + len;
  }
  return out;
}

/** First entry of a given type, or undefined. */
export function tlvGet(entries: TlvEntry[], type: number): Buffer | undefined {
  return entries.find((e) => e.type === type)?.data;
}

// ---------------------------------------------------------------------------
// Fixed-width little-endian integer helpers. The spec declares an explicit
// width for every numeric field (uint8/uint16/uint32/uint64) — encode exactly
// that width rather than HAP's "minimal bytes" habit, so payloads are
// unambiguous.
// ---------------------------------------------------------------------------

export function uint8(n: number): Buffer {
  const b = Buffer.alloc(1);
  b.writeUInt8(n);
  return b;
}

export function uint16(n: number): Buffer {
  const b = Buffer.alloc(2);
  b.writeUInt16LE(n);
  return b;
}

export function uint32(n: number): Buffer {
  const b = Buffer.alloc(4);
  b.writeUInt32LE(n);
  return b;
}

export function uint64(n: bigint): Buffer {
  const b = Buffer.alloc(8);
  b.writeBigUInt64LE(n);
  return b;
}

export function utf8(s: string): Buffer {
  return Buffer.from(s, "utf8");
}

/** Hex dump for probe logging: "01 06 aa bb …" capped for readability. */
export function hexDump(buf: Buffer, max = 512): string {
  const shown = buf.subarray(0, max);
  const hex = shown.toString("hex").replace(/(..)/g, "$1 ").trimEnd();
  return buf.length > max ? `${hex} … (+${buf.length - max} bytes)` : hex;
}
