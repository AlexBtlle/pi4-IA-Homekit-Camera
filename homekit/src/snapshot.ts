import { spawn, ChildProcess } from "child_process";

/**
 * Keeps a persistent ffmpeg process running that continuously grabs one JPEG
 * frame every 5 seconds from the RTSP stream and stores it in memory.
 *
 * HomeKit snapshot requests are answered instantly from the in-memory cache.
 * If ffmpeg dies (RTSP disconnect, etc.) it is restarted automatically after
 * 2 s so the cache is always kept fresh.
 *
 * This replaces the on-demand grab approach which had two failure modes:
 *  - cold start: first grab took 5-12 s (RTSP connect + keyframe wait) →
 *    HomeKit timed out and showed a black tile
 *  - silent failure: background refreshes failed without updating the cache,
 *    so the tile froze on whatever frame was captured last
 */
export class SnapshotProvider {
  private cache?: Buffer;
  private ff?: ChildProcess;
  private stopped = false;

  constructor(
    private readonly rtspUrl: string,
    private readonly width: number,
    private readonly height: number,
  ) {}

  start(): void {
    this.launch();
  }

  stop(): void {
    this.stopped = true;
    this.ff?.kill("SIGKILL");
  }

  get(): Promise<Buffer> {
    if (this.cache) return Promise.resolve(this.cache);

    // No frame yet (first few seconds after startup): poll briefly.
    return new Promise((resolve, reject) => {
      const deadline = Date.now() + 10_000;
      const id = setInterval(() => {
        if (this.cache) {
          clearInterval(id);
          resolve(this.cache!);
        } else if (Date.now() >= deadline) {
          clearInterval(id);
          reject(new Error("snapshot not yet available"));
        }
      }, 100);
    });
  }

  private launch(): void {
    if (this.stopped) return;

    const ff = spawn("ffmpeg", [
      "-hide_banner", "-loglevel", "error",
      "-rtsp_transport", "tcp",
      "-stimeout", "10000000",
      "-i", this.rtspUrl,
      "-vf", `fps=1/5,scale=${this.width}:${this.height}`,
      "-f", "image2pipe",
      "-vcodec", "mjpeg",
      "pipe:1",
    ]);

    this.ff = ff;
    const chunks: Buffer[] = [];

    ff.stdout.on("data", (chunk: Buffer) => {
      chunks.push(chunk);
      const buf = Buffer.concat(chunks);
      chunks.length = 0;
      const remaining = this.extractFrames(buf);
      if (remaining.length > 0) chunks.push(remaining);
    });

    ff.on("close", () => {
      if (!this.stopped) {
        console.log("[snapshot] ffmpeg exited, restarting in 2 s…");
        setTimeout(() => this.launch(), 2_000);
      }
    });
  }

  // JPEG frames are delimited by 0xFF 0xD8 (SOI) … 0xFF 0xD9 (EOI).
  // Extract every complete frame from the accumulated buffer and return
  // whatever bytes remain after the last complete frame.
  private extractFrames(buf: Buffer): Buffer {
    let offset = 0;
    for (;;) {
      const soi = buf.indexOf(Buffer.from([0xff, 0xd8]), offset);
      if (soi === -1) break;
      const eoi = buf.indexOf(Buffer.from([0xff, 0xd9]), soi + 2);
      if (eoi === -1) break;
      this.cache = Buffer.from(buf.buffer, buf.byteOffset + soi, eoi + 2 - soi);
      offset = eoi + 2;
    }
    return offset > 0 ? Buffer.from(buf.buffer, buf.byteOffset + offset) : buf;
  }
}
