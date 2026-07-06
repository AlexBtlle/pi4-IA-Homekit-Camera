import { describe, it, expect } from "vitest";
import { mkdtempSync, writeFileSync, utimesSync } from "fs";
import { tmpdir } from "os";
import path from "path";
import http from "http";
import { SnapshotProvider } from "../src/snapshot";
import { MotionService } from "../src/motion";

function tmpSnapshot(ageSec: number): string {
  const dir = mkdtempSync(path.join(tmpdir(), "pi4cam-snap-"));
  const file = path.join(dir, "snapshot.jpg");
  writeFileSync(file, Buffer.from("jpeg-bytes"));
  const t = (Date.now() - ageSec * 1000) / 1000;
  utimesSync(file, t, t);
  return file;
}

describe("SnapshotProvider staleness (#38)", () => {
  it("serves a fresh snapshot", async () => {
    const provider = new SnapshotProvider(tmpSnapshot(2));
    expect((await provider.get()).toString()).toBe("jpeg-bytes");
  });

  it("rejects a stale snapshot instead of serving a lying image", async () => {
    // pi4cam died 5 min ago: the Home app must show unavailable, not a
    // frozen frame passed off as live.
    const provider = new SnapshotProvider(tmpSnapshot(300));
    await expect(provider.get()).rejects.toThrow(/stale/);
  });

  it("still waits for the very first snapshot after boot", async () => {
    const dir = mkdtempSync(path.join(tmpdir(), "pi4cam-snap-"));
    const file = path.join(dir, "snapshot.jpg");
    const provider = new SnapshotProvider(file);
    const pending = provider.get(); // file not written yet → polls
    setTimeout(() => writeFileSync(file, Buffer.from("first")), 150);
    expect((await pending).toString()).toBe("first");
  });
});

describe("MotionService port conflict (#38)", () => {
  it("logs EADDRINUSE instead of crashing the process", async () => {
    // Squat a port, then start the motion endpoint on the same one.
    const squatter = http.createServer(() => {});
    const port = await new Promise<number>((resolve) => {
      squatter.listen(0, "127.0.0.1", () =>
        resolve((squatter.address() as { port: number }).port),
      );
    });
    try {
      const motion = new MotionService(
        {} as ConstructorParameters<typeof MotionService>[0],
        port,
        10,
      );
      motion.start(); // pre-#38: uncaught 'error' event → process crash
      await new Promise((r) => setTimeout(r, 200));
      motion.stop(); // reaching this line means the error was handled
    } finally {
      squatter.close();
    }
  });
});
