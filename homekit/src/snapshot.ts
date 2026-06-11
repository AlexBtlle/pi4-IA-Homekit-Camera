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
  readonly snapshotFile: string;
  private stopped = false;

  constructor(
    private readonly rtspUrl: string,
    private readonly width: number,
    private readonly height: number,
  ) {
    this.snapshotFile = path.join(os.tmpdir(), "pi4cam-snapshot.jpg");
  }

  start(): void {
    const loop = () => {
      this.grab().finally(() => {
        if (!this.stopped) setTimeout(loop, 5_000);
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
        const deadline = Date.now() + 10_000;
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
      const ff = spawn("ffmpeg", [
        "-hide_banner",
        "-loglevel",
        "warning",
        "-rtsp_transport",
        "tcp",
        "-i",
        this.rtspUrl,
        "-frames:v",
        "1",
        "-vf",
        `scale=${this.width}:${this.height}`,
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
            console.log("[snapshot] frame captured");
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
