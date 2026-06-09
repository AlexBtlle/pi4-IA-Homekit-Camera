import { spawn } from "child_process";

/**
 * Grabs a single JPEG frame from the RTSP stream via ffmpeg.
 *
 * The most recent snapshot is cached for a few seconds: HomeKit can ask for
 * snapshots fairly often (lock screen, notifications, the Home grid) and
 * spawning ffmpeg every time would hammer the Pi for no benefit.
 */
export class SnapshotProvider {
  private cache?: Buffer;
  private cacheTime = 0;
  private static readonly CACHE_MS = 4000;

  constructor(private readonly rtspUrl: string) {}

  async get(width: number, height: number): Promise<Buffer> {
    const now = Date.now();
    if (this.cache && now - this.cacheTime < SnapshotProvider.CACHE_MS) {
      return this.cache;
    }

    const jpeg = await this.grab(width, height);
    this.cache = jpeg;
    this.cacheTime = Date.now();
    return jpeg;
  }

  private grab(width: number, height: number): Promise<Buffer> {
    const args = [
      "-hide_banner",
      "-loglevel",
      "error",
      "-rtsp_transport",
      "tcp",
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

      const timer = setTimeout(() => {
        ff.kill("SIGKILL");
        reject(new Error("snapshot timed out"));
      }, 5000);

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
