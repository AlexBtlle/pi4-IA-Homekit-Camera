import { ChildProcess, spawn } from "child_process";

/** A complete fMP4 fragment (moof + mdat) with the time it was produced. */
export interface Fragment {
  id: number;
  data: Buffer;
  time: number; // monotonic ms (performance.now) — the Pi has no RTC, so the
  // wall clock can jump (NTP sync at boot) and must never age the ring (#36)
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

  // Largest plausible top-level box: a 1 s GOP at 8 Mbps is ~1 MB of mdat, so
  // 32 MB is far beyond anything legitimate. A size above it — or size 0
  // ("box extends to EOF", legal ISO-BMFF but nonsensical mid-stream) — used
  // to make the parser stop consuming while `buf` grew unbounded on every
  // chunk: a silent OOM march on a 512 MB board (#36). Throw instead; the
  // Prebuffer catches it and recycles ffmpeg.
  static readonly MAX_BOX_SIZE = 32 * 1024 * 1024;

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

      if (size < header || size > Mp4BoxParser.MAX_BOX_SIZE) {
        throw new Error(`corrupt MP4 box: type "${type}", size ${size}`);
      }
      if (this.buf.length < size) {
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
  private staleTimer?: NodeJS.Timeout;
  private lastActivity = 0;

  private static readonly RETAIN_MS = 6000;
  // Cap on fragments queued for one recording stream. A stalled home hub
  // stops draining while ffmpeg keeps producing ~1 MB/s — unbounded, that
  // buries a 512 MB board in minutes. 60 fragments ≈ 1 min of video: far
  // beyond any transient hiccup, so past it the stream is abandoned (#36).
  private static readonly MAX_QUEUE = 60;
  // ffmpeg alive but no stdout bytes for this long → hung RTSP session. The
  // 'close' handler only covers an ffmpeg that *exits*; field-tested on the
  // Python publisher, a SIGSTOPped mediamtx leaves ffmpeg blocked forever.
  // Generous enough for a cold spawn (handshake + first keyframe).
  private static readonly STALE_MS = 10_000;

  constructor(private readonly rtspUrl: string) {}

  start(): void {
    if (this.running) {
      return;
    }
    this.running = true;
    // Zombie watchdog: a silent-but-alive ffmpeg would otherwise starve HKSV
    // without a single log line.
    this.staleTimer = setInterval(() => this.checkStale(), 2_000);
    this.spawn();
  }

  stop(): void {
    this.running = false;
    if (this.restartTimer) {
      clearTimeout(this.restartTimer);
      this.restartTimer = undefined;
    }
    if (this.staleTimer) {
      clearInterval(this.staleTimer);
      this.staleTimer = undefined;
    }
    this.ff?.kill("SIGKILL");
    this.ff = undefined;
    this.resetParseState();
  }

  /** Kill an ffmpeg that is alive but producing nothing (hung transport). */
  private checkStale(): void {
    if (!this.running || !this.ff) {
      return; // stopped, or between restarts
    }
    const silentMs = performance.now() - this.lastActivity;
    if (silentMs < Prebuffer.STALE_MS) {
      return;
    }
    console.error(
      `[prebuffer] ffmpeg alive but silent for ${Math.round(silentMs / 1000)}s — killing it`,
    );
    this.ff.kill("SIGKILL"); // 'close' fires → parse reset + backoff respawn
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
      // NO "-timeout" here: field-measured, it adds 2-4 s to the RTSP
      // connection setup (#43). The checkStale watchdog above is the real
      // zombie protection (#34).
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
    this.lastActivity = performance.now(); // spawn counts as activity

    ff.stdout.on("data", (chunk: Buffer) => {
      this.lastActivity = performance.now();
      let boxes;
      try {
        boxes = this.parser.push(chunk);
      } catch (e) {
        // Corrupt/EOF-sized box: the stream can't be re-synced — recycle
        // ffmpeg ('close' resets the parse state and respawns with backoff).
        console.error(`[prebuffer] ${(e as Error).message} — recycling ffmpeg`);
        ff.kill("SIGKILL");
        return;
      }
      for (const box of boxes) {
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
          time: performance.now(),
        });
        this.pendingMoof = undefined;
      }
    }
  }

  private addFragment(frag: Fragment): void {
    this.rolling.push(frag);
    const cutoff = performance.now() - Prebuffer.RETAIN_MS;
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
    let overflowed = false;
    let wake: (() => void) | undefined;
    const listener: Listener = {
      push: (f) => {
        if (queue.length >= Prebuffer.MAX_QUEUE) {
          overflowed = true; // consumer loop turns this into a thrown error
        } else {
          queue.push(f);
        }
        wake?.();
      },
    };
    this.listeners.add(listener);

    // ONE abort listener for the generator's whole life. The old code added
    // a fresh {once:true} listener on every empty-queue wait (~1×/s), piling
    // them up on the same signal until the abort: MaxListenersExceededWarning
    // spam and memory held for the entire clip (#36).
    const onAbort = () => wake?.();
    signal.addEventListener("abort", onAbort, { once: true });

    try {
      // Prebuffer: fragments already captured before the trigger.
      const cutoff = performance.now() - prebufferMs;
      let lastId = 0;
      for (const f of this.rolling.filter((f) => f.time >= cutoff)) {
        lastId = f.id;
        yield f.data;
      }

      // Live fragments from here on.
      while (!signal.aborted) {
        if (overflowed) {
          // A stalled hub stopped draining: abandoning beats burying the
          // board. The recording delegate surfaces the error and HomeKit
          // closes the stream; the prebuffer itself keeps running.
          console.error(
            `[prebuffer] recording stream stalled — ${queue.length} fragments` +
              ` queued (~${Math.round((queue.length * Prebuffer.RETAIN_MS) / 6000)}s), abandoning`,
          );
          throw new Error("recording consumer too slow — stream abandoned");
        }
        if (queue.length === 0) {
          await new Promise<void>((resolve) => {
            wake = resolve;
            if (signal.aborted) {
              resolve(); // raced: aborted between the while check and here
            }
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
      signal.removeEventListener("abort", onAbort);
      this.listeners.delete(listener);
    }
  }
}
