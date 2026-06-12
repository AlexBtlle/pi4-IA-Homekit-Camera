import { ChildProcess, spawn } from "child_process";

/** A complete fMP4 fragment (moof + mdat) with the time it was produced. */
export interface Fragment {
  id: number;
  data: Buffer;
  time: number; // epoch ms
}

interface Listener {
  push: (f: Fragment) => void;
}

/**
 * Incremental parser for a stream of top-level MP4 boxes.
 * Each box is `size(4) + type(4) + payload`; `size === 1` means a 64-bit
 * largesize follows the type. We only ever see ftyp/moov/moof/mdat here.
 */
export class Mp4BoxParser {
  private buf: Buffer = Buffer.alloc(0);

  push(chunk: Buffer): { type: string; data: Buffer }[] {
    this.buf = this.buf.length ? Buffer.concat([this.buf, chunk]) : chunk;
    const boxes: { type: string; data: Buffer }[] = [];

    while (this.buf.length >= 8) {
      let size = this.buf.readUInt32BE(0);
      const type = this.buf.toString("ascii", 4, 8);
      let header = 8;

      if (size === 1) {
        if (this.buf.length < 16) {
          break;
        }
        const high = this.buf.readUInt32BE(8);
        const low = this.buf.readUInt32BE(12);
        size = high * 2 ** 32 + low;
        header = 16;
      }

      if (size < header || this.buf.length < size) {
        break;
      }
      boxes.push({ type, data: this.buf.subarray(0, size) });
      this.buf = this.buf.subarray(size);
    }
    return boxes;
  }
}

/**
 * Continuous fragmented-MP4 prebuffer.
 *
 * Runs a single ffmpeg that reads the RTSP stream and muxes a fragmented MP4
 * (`-c:v copy` + a silent AAC track HKSV expects). Top-level boxes are parsed
 * into an initialization segment (ftyp+moov) and a rolling list of recent
 * fragments. When motion fires, the recording delegate pulls the last few
 * seconds (the prebuffer) followed by the live fragments, so the HKSV clip
 * begins *before* the trigger — exactly like a commercial camera.
 */
export class Prebuffer {
  private ff?: ChildProcess;
  private parser = new Mp4BoxParser();

  private initSegment?: Buffer;
  private initWaiters: ((b: Buffer) => void)[] = [];

  private pendingInit: Buffer[] = [];
  private pendingMoof?: Buffer;

  private rolling: Fragment[] = [];
  private readonly listeners = new Set<Listener>();
  private seq = 0;

  private running = false;
  private restartTimer?: NodeJS.Timeout;
  private restartDelay = 1000;

  private static readonly RETAIN_MS = 6000;

  constructor(private readonly rtspUrl: string) {}

  start(): void {
    if (this.running) {
      return;
    }
    this.running = true;
    this.spawn();
  }

  stop(): void {
    this.running = false;
    if (this.restartTimer) {
      clearTimeout(this.restartTimer);
      this.restartTimer = undefined;
    }
    this.ff?.kill("SIGKILL");
    this.ff = undefined;
    this.resetParseState();
  }

  // --------------------------------------------------------------------
  // ffmpeg lifecycle
  // --------------------------------------------------------------------

  private spawn(): void {
    if (!this.running) {
      return;
    }
    const args = [
      "-hide_banner",
      "-loglevel",
      "error",
      "-rtsp_transport",
      "tcp",
      "-i",
      this.rtspUrl,
      // Silent mono AAC track: the Pi camera module has no mic, but HKSV
      // expects an audio track in the recording.
      "-f",
      "lavfi",
      "-i",
      "anullsrc=channel_layout=mono:sample_rate=32000",
      "-map",
      "0:v:0",
      "-map",
      "1:a:0",
      "-c:v",
      "copy",
      "-c:a",
      "aac",
      "-b:a",
      "32k",
      "-ar",
      "32000",
      "-ac",
      "1",
      "-f",
      "mp4",
      "-movflags",
      "frag_keyframe+empty_moov+default_base_moof",
      "pipe:1",
    ];

    const ff = spawn("ffmpeg", args);
    this.ff = ff;

    ff.stdout.on("data", (chunk: Buffer) => {
      for (const box of this.parser.push(chunk)) {
        this.onBox(box.type, box.data);
      }
    });
    ff.stderr.on("data", (d: Buffer) =>
      console.error(`[prebuffer] ${d.toString().trim()}`),
    );
    ff.on("error", (e) => console.error("[prebuffer] ffmpeg error:", e.message));
    ff.on("close", (code) => {
      if (this.ff === ff) {
        this.ff = undefined;
      }
      if (this.running) {
        console.error(
          `[prebuffer] ffmpeg exited (${code}), restarting in ${this.restartDelay}ms`,
        );
        this.resetParseState();
        this.restartTimer = setTimeout(() => this.spawn(), this.restartDelay);
        this.restartDelay = Math.min(this.restartDelay * 2, 15000);
      }
    });

    // ffmpeg started cleanly enough to reset the backoff once data flows.
    ff.stdout.once("data", () => {
      this.restartDelay = 1000;
    });
  }

  private resetParseState(): void {
    this.parser = new Mp4BoxParser();
    this.pendingInit = [];
    this.pendingMoof = undefined;
    // Keep initSegment: it stays valid across reconnects (same encoder params).
  }

  // --------------------------------------------------------------------
  // Box handling
  // --------------------------------------------------------------------

  private onBox(type: string, data: Buffer): void {
    if (type === "ftyp" || type === "moov") {
      this.pendingInit.push(data);
      if (type === "moov") {
        this.initSegment = Buffer.concat(this.pendingInit);
        this.pendingInit = [];
        const waiters = this.initWaiters;
        this.initWaiters = [];
        waiters.forEach((fn) => fn(this.initSegment!));
      }
    } else if (type === "moof") {
      this.pendingMoof = data;
    } else if (type === "mdat") {
      if (this.pendingMoof) {
        this.addFragment({
          id: ++this.seq,
          data: Buffer.concat([this.pendingMoof, data]),
          time: Date.now(),
        });
        this.pendingMoof = undefined;
      }
    }
  }

  private addFragment(frag: Fragment): void {
    this.rolling.push(frag);
    const cutoff = Date.now() - Prebuffer.RETAIN_MS;
    while (this.rolling.length && this.rolling[0].time < cutoff) {
      this.rolling.shift();
    }
    for (const l of this.listeners) {
      l.push(frag);
    }
  }

  private getInit(signal: AbortSignal): Promise<Buffer> {
    if (this.initSegment) {
      return Promise.resolve(this.initSegment);
    }
    return new Promise<Buffer>((resolve, reject) => {
      this.initWaiters.push(resolve);
      signal.addEventListener("abort", () => reject(new Error("aborted")), {
        once: true,
      });
    });
  }

  // --------------------------------------------------------------------
  // Consumption
  // --------------------------------------------------------------------

  /**
   * Yields the init segment, then the buffered fragments from the last
   * `prebufferMs`, then live fragments until `signal` aborts.
   */
  async *segments(
    prebufferMs: number,
    signal: AbortSignal,
  ): AsyncGenerator<Buffer> {
    const init = await this.getInit(signal);
    yield init;

    const queue: Fragment[] = [];
    let wake: (() => void) | undefined;
    const listener: Listener = {
      push: (f) => {
        queue.push(f);
        wake?.();
      },
    };
    this.listeners.add(listener);

    try {
      // Prebuffer: fragments already captured before the trigger.
      const cutoff = Date.now() - prebufferMs;
      let lastId = 0;
      for (const f of this.rolling.filter((f) => f.time >= cutoff)) {
        lastId = f.id;
        yield f.data;
      }

      // Live fragments from here on.
      while (!signal.aborted) {
        if (queue.length === 0) {
          await new Promise<void>((resolve) => {
            wake = resolve;
            signal.addEventListener("abort", () => resolve(), { once: true });
          });
          wake = undefined;
          continue;
        }
        const f = queue.shift()!;
        if (f.id <= lastId) {
          continue; // already emitted from the prebuffer snapshot
        }
        lastId = f.id;
        yield f.data;
      }
    } finally {
      this.listeners.delete(listener);
    }
  }
}
