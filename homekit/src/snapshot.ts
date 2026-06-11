import { spawn } from "child_process";
import fs from "fs/promises";
import os from "os";
import path from "path";

/**
 * Maintains a fresh JPEG snapshot from the RTSP stream.
 *
 * A grab loop runs continuously: one ffmpeg invocation grabs a single frame,
 * writes it to a temp file, renames it atomically into place, then waits 5 s
 * before the next grab.  Renaming is atomic on Linux so readers always see a
 * complete JPEG.
 *
 * HomeKit snapshot requests are served immediately from the on-disk file —
 * no pipeline, no MJPEG parsing, no Buffer pool gymnastics.
 */
export class SnapshotProvider {
  // Snapshot resolution: kept small so JPEG encoding is fast on the Pi Zero.
  private static readonly SNAP_W = 640;
  private static readonly SNAP_H = 360;
  private stopped = false;

  readonly snapshotFile: string;

  constructor(private readonly rtspUrl: string) {
    this.snapshotFile = path.join(os.tmpdir(), "pi4cam-snapshot.jpg");
  }

  start(): void {
    const loop = () => {
      this.grab().finally(() => {
        if (!this.stopped) setTimeout(loop, 60_000);
      });
    };
    loop();
  }

  stop(): void {
    this.stopped = true;
  }

  /** Returns the latest cached JPEG, waiting up to 10 s if none yet. */
  async get(): Promise<Buffer> {
    try {
      const buf = await fs.readFile(this.snapshotFile);
      console.log(`[snapshot] served ${buf.length} bytes`);
      return buf;
    } catch {
      // File not yet created (first grab still in progress).
      return new Promise((resolve, reject) => {
        const deadline = Date.now() + 20_000;
        const id = setInterval(async () => {
          try {
            const buf = await fs.readFile(this.snapshotFile);
            clearInterval(id);
            console.log(`[snapshot] served ${buf.length} bytes (after wait)`);
            resolve(buf);
          } catch {
            if (Date.now() >= deadline) {
              clearInterval(id);
              reject(new Error("snapshot not yet available"));
            }
          }
        }, 200);
      });
    }
  }

  private grab(): Promise<void> {
    const tmp = this.snapshotFile + ".tmp";

    return new Promise((resolve) => {
      const t0 = Date.now();
      const ff = spawn("ffmpeg", [
        "-hide_banner",
        "-loglevel",
        "warning",
        // Suppress buffering so the first video frame is output immediately
        // instead of waiting for several seconds of stream data.
        "-fflags",
        "nobuffer",
        "-flags",
        "low_delay",
        "-rtsp_transport",
        "tcp",
        "-i",
        this.rtspUrl,
        "-frames:v",
        "1",
        "-vf",
        `scale=${SnapshotProvider.SNAP_W}:${SnapshotProvider.SNAP_H}`,
        "-f",
        "image2",
        "-y",
        tmp,
      ]);

      const errLines: string[] = [];
      ff.stderr.on("data", (d: Buffer) => errLines.push(d.toString()));

      ff.on("error", (e) => {
        console.error("[snapshot] spawn error:", e.message);
        resolve();
      });

      ff.on("close", async (code) => {
        if (code === 0) {
          try {
            await fs.rename(tmp, this.snapshotFile);
            console.log(`[snapshot] frame captured in ${Date.now() - t0} ms`);
          } catch (e) {
            console.error("[snapshot] rename failed:", e);
          }
        } else {
          const msg = errLines.join("").trim().slice(-300);
          console.error(
            `[snapshot] grab failed (code ${code})${msg ? ": " + msg : ""}`,
          );
        }
        resolve();
      });
    });
  }
}
