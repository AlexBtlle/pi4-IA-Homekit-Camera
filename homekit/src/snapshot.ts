import { spawn } from "child_process";
import fs from "fs/promises";
import os from "os";
import path from "path";

/**
 * On-demand snapshot with stale-while-revalidate via a temp file.
 *
 * No periodic timer — grabs happen only when HomeKit requests a snapshot
 * AND the cached file is older than STALE_MS.  When the Home app is closed
 * the Pi uses 0 % CPU for snapshots.
 *
 * The file persists across service restarts, so a stale-but-valid JPEG is
 * always available after the first successful grab.
 */
export class SnapshotProvider {
  readonly snapshotFile: string;
  private grabbing = false;
  private static readonly STALE_MS = 10_000;
  private static readonly SNAP_W = 640;
  private static readonly SNAP_H = 360;

  constructor(private readonly rtspUrl: string) {
    this.snapshotFile = path.join(os.tmpdir(), "pi4cam-snapshot.jpg");
  }

  /** Trigger the first grab so the cache is warm before the first request. */
  start(): void {
    this.grabIfIdle();
  }

  stop(): void {
    this.grabbing = false; // let the current grab finish but don't restart
  }

  async get(): Promise<Buffer> {
    try {
      const [buf, stat] = await Promise.all([
        fs.readFile(this.snapshotFile),
        fs.stat(this.snapshotFile),
      ]);
      if (Date.now() - stat.mtimeMs > SnapshotProvider.STALE_MS) {
        this.grabIfIdle(); // refresh in background, serve stale now
      }
      return buf;
    } catch {
      // No cached file yet — wait for the first grab (up to 20 s).
      return this.waitForFirst();
    }
  }

  private grabIfIdle(): void {
    if (this.grabbing) return;
    this.grab();
  }

  private waitForFirst(): Promise<Buffer> {
    this.grabIfIdle();
    return new Promise((resolve, reject) => {
      const deadline = Date.now() + 20_000;
      const id = setInterval(async () => {
        try {
          const buf = await fs.readFile(this.snapshotFile);
          clearInterval(id);
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

  private grab(): void {
    this.grabbing = true;
    const tmp = this.snapshotFile + ".tmp";
    const t0 = Date.now();

    const ff = spawn("ffmpeg", [
      "-hide_banner", "-loglevel", "warning",
      "-fflags", "nobuffer",
      "-flags", "low_delay",
      "-rtsp_transport", "tcp",
      "-i", this.rtspUrl,
      "-frames:v", "1",
      "-vf", `scale=${SnapshotProvider.SNAP_W}:${SnapshotProvider.SNAP_H}`,
      "-f", "image2", "-y", tmp,
    ]);

    const errLines: string[] = [];
    ff.stderr.on("data", (d: Buffer) => errLines.push(d.toString()));

    ff.on("error", (e) => {
      console.error("[snapshot] spawn error:", e.message);
      this.grabbing = false;
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
        console.error(`[snapshot] grab failed (code ${code})${msg ? ": " + msg : ""}`);
      }
      this.grabbing = false;
    });
  }
}
