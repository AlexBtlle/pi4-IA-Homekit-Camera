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

  constructor(snapshotFile: string = DEFAULT_SNAPSHOT_PATH) {
    this.snapshotFile = snapshotFile;
  }

  async get(): Promise<Buffer> {
    try {
      return await fs.readFile(this.snapshotFile);
    } catch {
      return this.waitForFirst();
    }
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
