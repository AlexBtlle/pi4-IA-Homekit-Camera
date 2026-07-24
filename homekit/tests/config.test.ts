import fs from "fs";
import { afterEach, describe, expect, test, vi } from "vitest";

import { loadConfig, resolveFfmpeg } from "../src/config";

afterEach(() => vi.restoreAllMocks());

describe("loadConfig (against the shipped config.yaml)", () => {
  // In a dev checkout, loadConfig resolves the repo's own config.yaml — so
  // this doubles as an integration check that the SHIPPED file parses into a
  // sane AppConfig (a malformed default config would break every install).
  test("shipped defaults produce a coherent AppConfig", () => {
    const cfg = loadConfig();
    expect(cfg.rtspPort).toBe(8554);
    expect(cfg.rtspUrl).toBe("rtsp://127.0.0.1:8554/camera");
    // config.yaml stores bit/s (8_000_000); the governor thinks in kbps.
    expect(cfg.bitrateKbps).toBe(8000);
    expect(cfg.width).toBe(1920);
    expect(cfg.height).toBe(1080);
    expect(cfg.fps).toBe(30);
    expect(cfg.motionPort).toBe(8989);
    expect(cfg.qrWebPort).toBe(8080);
    expect(cfg.snapshotPath).toBe("/dev/shm/pi4cam-snapshot.jpg");
    expect(cfg.cameraControlUrl).toBe("http://127.0.0.1:8990");
    expect(cfg.cameraName.length).toBeGreaterThan(0);
    expect(cfg.ffmpegPath.length).toBeGreaterThan(0);
  });
});

describe("resolveFfmpeg", () => {
  test("an explicit config value always wins", () => {
    expect(resolveFfmpeg("/usr/local/bin/myffmpeg")).toBe("/usr/local/bin/myffmpeg");
  });

  test("falls back to the static build when present", () => {
    vi.spyOn(fs, "existsSync").mockReturnValue(true);
    expect(resolveFfmpeg(undefined)).toBe("/opt/pi4cam/bin/ffmpeg-static");
  });

  test("falls back to system ffmpeg when the static build is absent", () => {
    vi.spyOn(fs, "existsSync").mockReturnValue(false);
    expect(resolveFfmpeg(undefined)).toBe("ffmpeg");
  });

  test("empty/undefined configured values are not taken literally", () => {
    vi.spyOn(fs, "existsSync").mockReturnValue(false);
    expect(resolveFfmpeg("")).toBe("ffmpeg");
    expect(resolveFfmpeg(null)).toBe("ffmpeg");
  });
});
