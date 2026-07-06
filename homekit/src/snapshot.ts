import fs from "fs/promises";

/** tmpfs path the Python service writes the snapshot to — see config.yaml. */
const DEFAULT_SNAPSHOT_PATH = "/dev/shm/pi4cam-snapshot.jpg";

/**
 * Reads the JPEG snapshot written by the Python camera service (picamera2).
 *
 * The Python side captures directly from the YUV420 main stream every
 * ~2 s and writes the snapshot file atomically to a tmpfs path (RAM-backed,
 * keeps the 24/7 rewrites off the SD card). Node just reads it — no ffmpeg,
 * no H264 decode, near-zero latency. Both services share snapshot_path.
 */
export class SnapshotProvider {
  readonly snapshotFile: string;

  // Python rewrites the file every snapshot_interval (2-5 s). Way past that,
  // pi4cam is down and the picture is a lie: better to error — the Home app
  // then shows the camera as unavailable instead of a frozen frame (#38).
  private static readonly MAX_AGE_MS = 30_000;

  constructor(snapshotFile: string = DEFAULT_SNAPSHOT_PATH) {
    this.snapshotFile = snapshotFile;
  }

  async get(): Promise<Buffer> {
    const stat = await fs.stat(this.snapshotFile).catch(() => null);
    if (!stat) {
      return this.waitForFirst(); // fresh boot: not written yet
    }
    const ageMs = Date.now() - stat.mtimeMs;
    if (ageMs > SnapshotProvider.MAX_AGE_MS) {
      throw new Error(
        `snapshot is stale (${Math.round(ageMs / 1000)}s old) — is pi4cam running?`,
      );
    }
    return fs.readFile(this.snapshotFile);
  }

  private waitForFirst(): Promise<Buffer> {
    return new Promise((resolve, reject) => {
      const deadline = Date.now() + 20_000;
      let delay = 100;
      const attempt = async () => {
        try {
          resolve(await fs.readFile(this.snapshotFile));
        } catch {
          if (Date.now() >= deadline) {
            reject(new Error("snapshot not yet available"));
          } else {
            delay = Math.min(delay * 1.5, 2_000);
            setTimeout(attempt, delay);
          }
        }
      };
      attempt();
    });
  }
}
