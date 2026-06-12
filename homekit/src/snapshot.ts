import fs from "fs/promises";
import os from "os";
import path from "path";

/**
 * Reads the JPEG snapshot written by the Python camera service (picamera2).
 *
 * The Python side captures directly from the YUV420 main stream every
 * ~2 s and writes /tmp/pi4cam-snapshot.jpg atomically. Node just reads it —
 * no ffmpeg, no H264 decode, near-zero latency.
 */
export class SnapshotProvider {
  readonly snapshotFile: string;

  constructor() {
    this.snapshotFile = path.join(os.tmpdir(), "pi4cam-snapshot.jpg");
  }

  start(): void {}
  stop(): void {}

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
      const id = setInterval(async () => {
        try {
          resolve(await fs.readFile(this.snapshotFile));
          clearInterval(id);
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
