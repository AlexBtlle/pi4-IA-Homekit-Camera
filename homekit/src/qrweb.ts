import { promises as fs } from "fs";
import http from "http";
import net from "net";
import os from "os";
import qrcode from "qrcode-terminal";
import type { MotionService } from "./motion";

export class QrWebServer {
  private server?: http.Server;
  private _qrBlock?: string;

  constructor(
    private readonly setupUri: string,
    private readonly pin: string,
    private readonly cameraName: string,
    private readonly port: number,
    private readonly motionService?: MotionService,
  ) {}

  start(): this {
    qrcode.generate(this.setupUri, { small: true }, (ascii: string) => {
      this._qrBlock = `<pre>${esc(ascii)}</pre>`;
      this.listen();
    });
    return this;
  }

  stop(): void {
    this.server?.close();
  }

  private listen(): void {
    this.server = http.createServer(async (req, res) => {
      if (!this._qrBlock) {
        res.writeHead(503);
        res.end("Starting…");
        return;
      }
      const page = await this.buildPage();
      res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
      res.end(page);
    });

    this.server.listen(this.port, "0.0.0.0", () => {
      const hostname = os.hostname();
      console.log(
        `[qrweb] http://${hostname}.local:${this.port}  |  http://${localIP()}:${this.port}`,
      );
    });
  }

  private async buildPage(): Promise<string> {
    const pin = this.pin.replace(/-/g, "‑");

    const [mediamtxOk, snapshotFresh, cpuTemp] = await Promise.all([
      this._mediamtxOk(),
      this._snapshotFresh(),
      this._cpuTemp(),
    ]);

    const uptime  = formatUptime(process.uptime());
    const motionStats = this.motionService?.getStats();
    const hostname = os.hostname();

    const dot = (ok: boolean) =>
      `<span class="dot ${ok ? "green" : "red"}"></span>`;

    const motionCount = motionStats?.triggerCount ?? 0;
    const motionAgo   = motionStats?.lastTrigger
      ? formatAgo(motionStats.lastTrigger)
      : null;

    return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>${esc(this.cameraName)} — HomeKit</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: #fff;
      color: #1a1a1a;
      font-family: -apple-system, BlinkMacSystemFont, "Inter", "Segoe UI", sans-serif;
      min-height: 100dvh;
      display: flex;
      flex-direction: column;
      align-items: center;
      padding: 2rem 1.5rem 4rem;
      gap: 2rem;
    }
    .header { text-align: center; }
    .header h1 { font-size: .9rem; font-weight: 500; color: #1a1a1a; letter-spacing: -.01em; }
    .header h1 span { color: #bbb; font-weight: 400; }
    .pairing {
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 1.75rem;
      width: 100%;
      max-width: 320px;
    }
    .qr-wrap {
      background: #fff;
      border: 1px solid #e5e5e5;
      border-radius: .5rem;
      padding: .875rem;
      display: inline-flex;
    }
    /* scaleX(0.83): corrects monospace char aspect ratio for block QR art.
       Courier New chars are ~0.6× as wide as tall; each text line = 2 QR rows,
       so the ideal ratio is 0.5. Factor = 0.5/0.601 ≈ 0.83. */
    .qr-wrap pre {
      font-family: "Courier New", monospace;
      font-size: 13px;
      line-height: 1;
      letter-spacing: 0;
      color: #000;
      user-select: none;
      transform: scaleX(0.83);
      transform-origin: center;
      display: block;
    }
    .pin-block { text-align: center; }
    .pin-block .label {
      font-size: .7rem; color: #aaa;
      letter-spacing: .08em; text-transform: uppercase; margin-bottom: .5rem;
    }
    .pin-block .pin {
      font-size: 2rem; font-weight: 600; letter-spacing: .18em;
      color: #1a1a1a; font-variant-numeric: tabular-nums;
    }
    .pin-block .hint { margin-top: .65rem; font-size: .75rem; color: #aaa; line-height: 1.6; }
    .status {
      width: 100%; max-width: 320px;
      border-top: 1px solid #ebebeb;
      padding-top: 2rem;
    }
    .status-row {
      display: flex; justify-content: space-between; align-items: center;
      padding: .6rem 0; border-bottom: 1px solid #ebebeb; font-size: .82rem;
    }
    .status-row:last-child { border-bottom: none; }
    .status-row .name { color: #888; }
    .status-row .val  { color: #1a1a1a; display: flex; align-items: center; gap: .45rem; }
    .dot { width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0; }
    .dot.green  { background: #22c55e; }
    .dot.red    { background: #ef4444; }
    .dot.yellow { background: #f59e0b; }
    .footer { padding-top: 1.5rem; font-size: .72rem; color: #ccc; text-align: center; }
  </style>
</head>
<body>
  <div class="header">
    <h1>${esc(this.cameraName)} <span>· ${esc(hostname)}.local</span></h1>
  </div>

  <div class="pairing">
    <div class="qr-wrap">${this._qrBlock}</div>
    <div class="pin-block">
      <div class="label">Setup code</div>
      <div class="pin">${esc(pin)}</div>
      <div class="hint">Home → + → Add Accessory → scan or enter code</div>
    </div>
  </div>

  <div class="status">
    <div class="status-row">
      <span class="name">pi4cam-homekit</span>
      <span class="val"><span class="dot green"></span>uptime ${esc(uptime)}</span>
    </div>
    <div class="status-row">
      <span class="name">pi4cam</span>
      <span class="val"><span class="dot ${snapshotFresh ? "green" : "red"}"></span>${snapshotFresh ? "snapshot fresh" : "snapshot stale"}</span>
    </div>
    <div class="status-row">
      <span class="name">mediamtx</span>
      <span class="val"><span class="dot ${mediamtxOk ? "green" : "red"}"></span>${mediamtxOk ? "RTSP :8554" : "down"}</span>
    </div>
    <div class="status-row">
      <span class="name">CPU</span>
      <span class="val"><span class="dot yellow"></span>${esc(cpuTemp)}</span>
    </div>
    <div class="status-row">
      <span class="name">Motion</span>
      <span class="val">${motionCount} event${motionCount !== 1 ? "s" : ""}${motionAgo ? ` — last ${esc(motionAgo)}` : ""}</span>
    </div>
    <div class="footer">pi4-IA-Homekit-Camera</div>
  </div>
</body>
</html>`;
  }

  private async _cpuTemp(): Promise<string> {
    try {
      const raw = await fs.readFile("/sys/class/thermal/thermal_zone0/temp", "utf8");
      return (parseInt(raw, 10) / 1000).toFixed(1) + "°C";
    } catch {
      return "N/A";
    }
  }

  private _mediamtxOk(): Promise<boolean> {
    return new Promise((resolve) => {
      const sock = new net.Socket();
      sock.setTimeout(500);
      sock.connect(8554, "127.0.0.1", () => { sock.destroy(); resolve(true); });
      sock.on("error", () => resolve(false));
      sock.on("timeout", () => { sock.destroy(); resolve(false); });
    });
  }

  private async _snapshotFresh(): Promise<boolean> {
    try {
      const stat = await fs.stat("/tmp/pi4cam-snapshot.jpg");
      return Date.now() - stat.mtimeMs < 15_000;
    } catch {
      return false;
    }
  }
}

function esc(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function localIP(): string {
  for (const ifaces of Object.values(os.networkInterfaces())) {
    for (const iface of ifaces ?? []) {
      if (iface.family === "IPv4" && !iface.internal) {
        return iface.address;
      }
    }
  }
  return "127.0.0.1";
}

function formatUptime(seconds: number): string {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m`;
  return `${Math.floor(seconds)}s`;
}

function formatAgo(date: Date): string {
  const sec = Math.floor((Date.now() - date.getTime()) / 1000);
  if (sec < 60) return `${sec}s ago`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min} min ago`;
  const h = Math.floor(min / 60);
  return `${h}h ago`;
}
