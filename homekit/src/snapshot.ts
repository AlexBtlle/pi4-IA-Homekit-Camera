import { spawn } from "child_process";

/**
 * Grabs a single JPEG frame from the RTSP stream via ffmpeg.
 *
 * The Home app is impatient: if a snapshot takes more than a couple of
 * seconds it shows the camera as unresponsive. A cold grab can take ~5 s
 * (RTSP connect + wait for the next keyframe, which only comes every 4 s),
 * so we never make HomeKit wait on one: the cached frame is served
 * immediately — even when stale — while a fresh grab runs in the background
 * (stale-while-revalidate). The cache is primed at startup so the very first
 * request already has a frame to serve.
 */
export class SnapshotProvider {
  private cache?: Buffer;
  private cacheTime = 0;
  private refreshing?: Promise<Buffer>;
  private static readonly FRESH_MS = 4000;

  constructor(private readonly rtspUrl: string) {}

  /** Fill the cache at startup so the first Home-app request is instant. */
  prime(width: number, height: number): void {
    this.refresh(width, height).catch((err) =>
      console.error("[snapshot] prime failed:", err.message),
    );
  }

  async get(width: number, height: number): Promise<Buffer> {
    const now = Date.now();
    if (this.cache && now - this.cacheTime < SnapshotProvider.FRESH_MS) {
      return this.cache;
    }

    const refresh = this.refresh(width, height);
    if (this.cache) {
      // Serve the stale frame right away; the refresh updates the cache for
      // the next request (the Home grid polls every few seconds anyway).
      refresh.catch(() => {});
      return this.cache;
    }
    return refresh;
  }

  private refresh(width: number, height: number): Promise<Buffer> {
    if (!this.refreshing) {
      this.refreshing = this.grab(width, height)
        .then((jpeg) => {
          this.cache = jpeg;
          this.cacheTime = Date.now();
          return jpeg;
        })
        .finally(() => {
          this.refreshing = undefined;
        });
    }
    return this.refreshing;
  }

  private grab(width: number, height: number): Promise<Buffer> {
    const args = [
      "-hide_banner",
      "-loglevel",
      "error",
      "-rtsp_transport",
      "tcp",
      // Wait up to 5 s for the RTSP connection itself.
      "-stimeout",
      "5000000",
      "-i",
      this.rtspUrl,
      "-frames:v",
      "1",
      "-vf",
      `scale=${width}:${height}`,
      "-f",
      "image2",
      "-vcodec",
      "mjpeg",
      "pipe:1",
    ];

    return new Promise<Buffer>((resolve, reject) => {
      const ff = spawn("ffmpeg", args);
      const chunks: Buffer[] = [];
      const errChunks: Buffer[] = [];

      // iperiod = fps×4 → keyframe every 4 s; we need at least one keyframe
      // before ffmpeg can output a frame. Allow 12 s total.
      const timer = setTimeout(() => {
        ff.kill("SIGKILL");
        reject(new Error("snapshot timed out"));
      }, 12000);

      ff.stdout.on("data", (d: Buffer) => chunks.push(d));
      ff.stderr.on("data", (d: Buffer) => errChunks.push(d));
      ff.on("error", (e) => {
        clearTimeout(timer);
        reject(e);
      });
      ff.on("close", (code) => {
        clearTimeout(timer);
        const out = Buffer.concat(chunks);
        if (code === 0 && out.length > 0) {
          resolve(out);
        } else {
          reject(
            new Error(
              `ffmpeg snapshot failed (code ${code}): ${Buffer.concat(
                errChunks,
              ).toString()}`,
            ),
          );
        }
      });
    });
  }
}
