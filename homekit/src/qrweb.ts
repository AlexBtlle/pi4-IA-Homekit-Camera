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

    const uptime = formatUptime(process.uptime());
    const motionStats = this.motionService?.getStats();

    const dot = (ok: boolean) => ok ? "🟢" : "🔴";

    let motionLine = "Motion events: 0";
    if (motionStats && motionStats.triggerCount > 0) {
      const ago = formatAgo(motionStats.lastTrigger!);
      motionLine = `Motion events: ${motionStats.triggerCount} — last ${ago}`;
    }

    return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>${esc(this.cameraName)} — HomeKit Pairing</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: #1c1c1e;
      color: #f2f2f7;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      min-height: 100dvh;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      padding: 2rem 1rem;
      gap: 2rem;
    }
    header { text-align: center; }
    header h1 { font-size: 1.5rem; font-weight: 600; }
    header p  { margin-top: .4rem; color: #aeaeb2; font-size: .9rem; }
    .card {
      background: #2c2c2e;
      border-radius: 1.25rem;
      padding: 2rem;
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 1.5rem;
      max-width: 420px;
      width: 100%;
    }
    pre {
      font-family: "Courier New", Courier, monospace;
      font-size: .65rem;
      line-height: 1;
      letter-spacing: 0;
      background: #fff;
      color: #000;
      padding: .75rem;
      border-radius: .75rem;
      user-select: none;
    }
    .pin-label { color: #aeaeb2; font-size: .8rem; text-transform: uppercase; letter-spacing: .08em; }
    .pin {
      font-size: 2.2rem;
      font-weight: 700;
      letter-spacing: .15em;
      color: #ffd60a;
    }
    .hint { color: #aeaeb2; font-size: .82rem; text-align: center; line-height: 1.5; }
    .hint strong { color: #f2f2f7; }
    .status {
      width: 100%;
      display: flex;
      flex-direction: column;
      gap: .55rem;
      border-top: 1px solid #3a3a3c;
      padding-top: 1.25rem;
    }
    .status-row {
      display: flex;
      justify-content: space-between;
      font-size: .85rem;
    }
    .status-row .label { color: #aeaeb2; }
    .status-row .value { color: #f2f2f7; }
    .divider { border: none; border-top: 1px solid #3a3a3c; margin: .25rem 0; }
    .motion-line {
      font-size: .82rem;
      color: #aeaeb2;
      text-align: center;
    }
    footer { color: #636366; font-size: .75rem; }
  </style>
</head>
<body>
  <header>
    <h1>${esc(this.cameraName)}</h1>
    <p>HomeKit pairing</p>
  </header>
  <div class="card">
    ${this._qrBlock}
    <p class="pin-label">Setup code</p>
    <p class="pin">${esc(pin)}</p>
    <p class="hint">
      Open <strong>Home</strong> → <strong>+</strong> → <strong>Add Accessory</strong>
      and scan the QR code above, or tap <em>More options…</em> and enter the code.
    </p>
    <div class="status">
      <div class="status-row"><span class="label">${dot(true)} pi4cam-homekit</span><span class="value">uptime ${esc(uptime)}</span></div>
      <div class="status-row"><span class="label">${dot(snapshotFresh)} pi4cam</span><span class="value">${snapshotFresh ? "snapshot fresh" : "snapshot stale"}</span></div>
      <div class="status-row"><span class="label">${dot(mediamtxOk)} mediamtx</span><span class="value">RTSP :8554</span></div>
      <hr class="divider">
      <div class="status-row"><span class="label">🌡 CPU</span><span class="value">${esc(cpuTemp)}</span></div>
      <p class="motion-line">${esc(motionLine)}</p>
    </div>
  </div>
  <footer>pi4-IA-Homekit-Camera</footer>
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
