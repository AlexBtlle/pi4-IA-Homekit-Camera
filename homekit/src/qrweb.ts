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
    :root {
      --bg:      #0a0a0f;
      --surface: rgba(255,255,255,.05);
      --border:  rgba(255,255,255,.08);
      --text:    #f2f2f7;
      --muted:   rgba(235,235,245,.5);
      --green:   #30d158;
      --yellow:  #ffd60a;
      --red:     #ff453a;
      --radius:  1.5rem;
    }
    body {
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "Segoe UI", sans-serif;
      min-height: 100dvh;
      display: flex;
      flex-direction: column;
      align-items: center;
      padding: 2.5rem 1rem 3rem;
      gap: 1.25rem;
    }
    .name-row {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      width: 100%;
      max-width: 420px;
    }
    .cam-name { font-size: 1.25rem; font-weight: 700; letter-spacing: -.01em; }
    .cam-sub  { font-size: .8rem; color: var(--muted); margin-top: .15rem; }
    .host-chip {
      font-size: .72rem;
      color: var(--muted);
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 2rem;
      padding: .25rem .7rem;
      white-space: nowrap;
    }
    .card {
      width: 100%;
      max-width: 420px;
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      backdrop-filter: blur(20px);
      -webkit-backdrop-filter: blur(20px);
      overflow: hidden;
    }
    .card-section { padding: 1.5rem; }
    .card-section + .card-section { border-top: 1px solid var(--border); }
    .pairing {
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 1.25rem;
    }
    .section-label {
      font-size: .72rem;
      font-weight: 600;
      letter-spacing: .08em;
      text-transform: uppercase;
      color: var(--muted);
      align-self: flex-start;
    }
    .qr-wrap {
      background: #fff;
      border-radius: .875rem;
      padding: .875rem;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      overflow: hidden;
    }
    /* scaleX(0.83): corrects monospace char aspect ratio for block QR art.
       Courier New chars are ~0.6× as wide as tall; each text line = 2 QR rows,
       so the ideal ratio is 0.5. Factor = 0.5/0.601 ≈ 0.83. */
    .qr-wrap pre {
      font-family: "Courier New", monospace;
      font-size: 14px;
      line-height: 1;
      letter-spacing: 0;
      color: #000;
      user-select: none;
      transform: scaleX(0.83);
      transform-origin: center;
      display: block;
    }
    .pin {
      font-size: 2.6rem;
      font-weight: 700;
      letter-spacing: .18em;
      color: var(--yellow);
      font-variant-numeric: tabular-nums;
    }
    .hint {
      font-size: .78rem;
      color: var(--muted);
      text-align: center;
      line-height: 1.6;
      max-width: 280px;
    }
    .hint strong { color: var(--text); font-weight: 600; }
    .status-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: .75rem;
    }
    .stat-cell {
      background: rgba(255,255,255,.04);
      border: 1px solid var(--border);
      border-radius: 1rem;
      padding: .9rem 1rem;
      display: flex;
      flex-direction: column;
      gap: .3rem;
    }
    .stat-cell.wide {
      grid-column: span 2;
      flex-direction: row;
      justify-content: space-between;
      align-items: center;
    }
    .stat-label { font-size: .72rem; color: var(--muted); font-weight: 500; }
    .stat-value { font-size: .92rem; font-weight: 600; color: var(--text); }
    .stat-value.green  { color: var(--green); }
    .stat-value.yellow { color: var(--yellow); }
    .stat-value.red    { color: var(--red); }
    .dot {
      display: inline-block;
      width: 7px; height: 7px;
      border-radius: 50%;
      margin-right: .35rem;
      vertical-align: middle;
    }
    .dot.green { background: var(--green); box-shadow: 0 0 5px var(--green); }
    .dot.red   { background: var(--red);   box-shadow: 0 0 5px var(--red); }
  </style>
</head>
<body>
  <div class="name-row">
    <div>
      <div class="cam-name">${esc(this.cameraName)}</div>
      <div class="cam-sub">HomeKit pairing</div>
    </div>
    <div class="host-chip">${esc(hostname)}.local</div>
  </div>

  <div class="card">
    <div class="card-section pairing">
      <span class="section-label">Pair with Home app</span>
      <div class="qr-wrap">${this._qrBlock}</div>
      <div class="pin">${esc(pin)}</div>
      <p class="hint">
        Open <strong>Home</strong> → <strong>+</strong> → <strong>Add Accessory</strong>,
        scan the QR code or tap <em>More options…</em> and enter the code.
      </p>
    </div>

    <div class="card-section">
      <div class="status-grid">
        <div class="stat-cell">
          <div class="stat-label">pi4cam-homekit</div>
          <div class="stat-value green">${dot(true)}Running</div>
          <div class="stat-label">uptime ${esc(uptime)}</div>
        </div>
        <div class="stat-cell">
          <div class="stat-label">pi4cam</div>
          <div class="stat-value ${snapshotFresh ? "green" : "red"}">${dot(snapshotFresh)}${snapshotFresh ? "Running" : "Stale"}</div>
          <div class="stat-label">${snapshotFresh ? "snapshot fresh" : "snapshot stale"}</div>
        </div>
        <div class="stat-cell">
          <div class="stat-label">mediamtx</div>
          <div class="stat-value ${mediamtxOk ? "green" : "red"}">${dot(mediamtxOk)}${mediamtxOk ? "Running" : "Down"}</div>
          <div class="stat-label">RTSP :8554</div>
        </div>
        <div class="stat-cell">
          <div class="stat-label">CPU temp</div>
          <div class="stat-value yellow">${esc(cpuTemp)}</div>
          <div class="stat-label">nominal</div>
        </div>
        <div class="stat-cell wide">
          <div>
            <div class="stat-label">Motion events</div>
            <div class="stat-value">${motionCount} detection${motionCount !== 1 ? "s" : ""}</div>
          </div>
          ${motionAgo ? `<div style="text-align:right">
            <div class="stat-label">Last trigger</div>
            <div class="stat-value" style="font-size:.82rem;color:var(--muted)">${esc(motionAgo)}</div>
          </div>` : ""}
        </div>
      </div>
    </div>
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
