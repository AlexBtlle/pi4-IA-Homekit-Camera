import http from "http";
import { CameraController, Characteristic } from "@homebridge/hap-nodejs";

/**
 * Tiny HTTP endpoint the Python detector posts to when it sees motion:
 *   POST /motion  → MotionSensor active for `timeoutSec`, then auto-reset.
 *
 * The MotionSensor lives on the CameraController (sensors.motion), so the same
 * trigger that fires the iOS notification also starts the HKSV recording via
 * the recording delegate.
 */
export class MotionService {
  private server?: http.Server;
  private resetTimer?: NodeJS.Timeout;
  private _lastTrigger?: Date;
  private _triggerCount = 0;

  getStats(): { lastTrigger?: Date; triggerCount: number } {
    return { lastTrigger: this._lastTrigger, triggerCount: this._triggerCount };
  }

  constructor(
    private readonly controller: CameraController,
    private readonly port: number,
    private readonly timeoutSec: number,
  ) {}

  start(): void {
    this.server = http.createServer((req, res) => {
      if (req.method === "POST" && req.url === "/motion") {
        // Drain the body, then trigger.
        req.on("data", () => {});
        req.on("end", () => {
          this.trigger();
          res.writeHead(200, { "Content-Type": "application/json" });
          res.end('{"ok":true}');
        });
      } else if (req.method === "GET" && req.url === "/health") {
        res.writeHead(200);
        res.end("ok");
      } else {
        res.writeHead(404);
        res.end();
      }
    });

    // Bind to loopback only — this endpoint must never be reachable off-box.
    this.server.listen(this.port, "127.0.0.1", () => {
      console.log(`[motion] listening on http://127.0.0.1:${this.port}/motion`);
    });
  }

  private trigger(): void {
    const sensor = this.controller.motionService;
    if (!sensor) {
      console.warn("[motion] no motion service on controller");
      return;
    }

    sensor.updateCharacteristic(Characteristic.MotionDetected, true);
    this._lastTrigger = new Date();
    this._triggerCount++;
    console.log("[motion] motion detected → sensor active");

    clearTimeout(this.resetTimer);
    this.resetTimer = setTimeout(() => {
      sensor.updateCharacteristic(Characteristic.MotionDetected, false);
      console.log("[motion] sensor reset");
    }, this.timeoutSec * 1000);
  }

  stop(): void {
    clearTimeout(this.resetTimer);
    this.server?.close();
  }
}
