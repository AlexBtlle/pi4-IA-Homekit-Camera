import { execFile } from "child_process";
import { promises as fs } from "fs";
import http from "http";
import net from "net";
import os from "os";
import qrcode from "qrcode-terminal";
import type { MotionService } from "./motion";
import type { RecordingDelegate } from "./recording";

type Health = "ok" | "warn" | "crit" | "muted";

export class QrWebServer {
  private server?: http.Server;
  private _qrBlock?: string;
  // Page cache (#38): every request used to rebuild the whole page and spawn
  // vcgencmd + systemctl — an aggressive scanner could load the Zero 2 W.
  // The promise (not the string) is cached so concurrent hits share ONE build.
  private _page?: { promise: Promise<string>; at: number };
  private static readonly PAGE_TTL_MS = 3_000;

  constructor(
    private readonly setupUri: string,
    private readonly pin: string,
    private readonly cameraName: string,
    private readonly port: number,
    private readonly snapshotPath: string,
    private readonly motionService?: MotionService,
    private readonly recording?: RecordingDelegate,
    // Same default as config.ts's rtsp.port fallback — kept optional so the
    // constructor stays compatible, but main.ts passes the configured value.
    private readonly rtspPort: number = 8554,
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
      req.on("error", () => res.destroy()); // aborted request ≠ process crash
      // Only the dashboard itself gets the (probed) page; favicon requests
      // and scanners get a cheap 404 instead of two execFile spawns (#38).
      if (req.method !== "GET" || (req.url ?? "").split("?")[0] !== "/") {
        res.writeHead(404);
        res.end();
        return;
      }
      if (!this._qrBlock) {
        res.writeHead(503);
        res.end("Starting…");
        return;
      }
      // The handler is async: without this guard a rejecting buildPage()
      // would be an unhandled rejection — fatal on modern Node — instead of
      // a 500. Every probe inside buildPage currently swallows its own
      // errors, so this is a belt for the day one of them stops doing so.
      try {
        const page = await this.cachedPage();
        res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
        res.end(page);
      } catch (e) {
        console.error(`[qrweb] page build failed: ${(e as Error).message}`);
        if (!res.headersSent) res.writeHead(500);
        res.end("status page error");
      }
    });

    // A taken port (8080 is popular) must log, not crash-loop the service.
    this.server.on("error", (e) => {
      console.error(`[qrweb] server error: ${e.message} — status page unavailable`);
    });

    this.server.listen(this.port, "0.0.0.0", () => {
      const hostname = os.hostname();
      console.log(
        `[qrweb] http://${hostname}.local:${this.port}  |  http://${localIP()}:${this.port}`,
      );
    });
  }

  private cachedPage(): Promise<string> {
    const now = performance.now();
    if (!this._page || now - this._page.at >= QrWebServer.PAGE_TTL_MS) {
      const promise = this.buildPage();
      // A failed build must not be served for a whole TTL: drop it from the
      // cache on rejection so the next request rebuilds immediately. (The
      // identity check keeps a newer entry from being evicted by an old
      // failure racing in late.)
      promise.catch(() => {
        if (this._page?.promise === promise) this._page = undefined;
      });
      this._page = { promise, at: now };
    }
    return this._page.promise;
  }

  private async buildPage(): Promise<string> {
    const pin = esc(this.pin.replace(/-/g, "‑"));
    const hostname = esc(os.hostname());
    const name = esc(this.cameraName);

    // All probes run on request only — no background polling on the Zero 2 W.
    const [mediamtxOk, snap, tempC, throttle, mem, pi4camActive] =
      await Promise.all([
        this._mediamtxOk(),
        this._snapshot(),
        this._cpuTempC(),
        this._throttle(),
        this._mem(),
        this._serviceActive("pi4cam"),
      ]);

    const load1 = os.loadavg()[0];
    const cores = os.cpus().length;
    const uptime = esc(formatUptime(process.uptime()));
    const hksv = this.recording?.recordingActive ?? null;
    const motion = this.motionService?.getStats();
    const motionCount = motion?.triggerCount ?? 0;
    const motionAgo = motion?.lastTrigger ? formatAgo(motion.lastTrigger) : null;

    // ---- derive per-item health --------------------------------------
    const tempLevel: Health =
      tempC === null ? "muted" : tempC >= 80 ? "crit" : tempC >= 70 ? "warn" : "ok";
    const swapLevel: Health =
      !mem || mem.swapTotal === 0
        ? "ok"
        : mem.swapUsed / mem.swapTotal >= 0.9
          ? "crit"
          : mem.swapUsed / mem.swapTotal >= 0.75
            ? "warn"
            : "ok";
    const mediamtxLevel: Health = mediamtxOk ? "ok" : "crit";
    const pi4camLevel: Health =
      pi4camActive === null ? "muted" : pi4camActive ? "ok" : "crit";
    const snapLevel: Health = snap.fresh ? "ok" : "crit";

    // Overall pill: worst of the meaningful signals (swap stays informational).
    const worst = worstOf([
      tempLevel,
      throttle.level,
      mediamtxLevel,
      pi4camLevel,
      snapLevel,
    ]);
    const pill =
      worst === "crit"
        ? { cls: "crit", dot: "red", text: "Attention needed" }
        : worst === "warn"
          ? { cls: "warn", dot: "amber", text: "Warning — check below" }
          : { cls: "ok", dot: "green", text: "All systems normal" };

    const dot = (h: Health) => `<span class="dot ${dotClass(h)}"></span>`;
    const tempStr = tempC === null ? "n/a" : `${tempC.toFixed(1)}<small>°C</small>`;
    const ram = mem
      ? `${mem.ramUsed}<small>/ ${mem.ramTotal} MB</small>`
      : "n/a";
    const swap = mem
      ? mem.swapTotal === 0
        ? "0<small>MB</small>"
        : `${mem.swapUsed}<small>MB</small>`
      : "n/a";
    const snapVal = snap.fresh
      ? `fresh${snap.ageSec !== null ? ` <small>· ${snap.ageSec}s ago</small>` : ""}`
      : "stale";
    const hksvVal =
      hksv === null
        ? `${dot("muted")}n/a`
        : hksv
          ? `${dot("ok")}armed`
          : `${dot("muted")}idle`;

    return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>${name} — HomeKit</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    :root {
      --line: #ebebeb; --line-strong: #e5e5e5;
      --ink: #1a1a1a; --muted: #888; --faint: #aaa; --tile: #fafafa;
      --green: #22c55e; --amber: #f59e0b; --red: #ef4444;
    }
    body {
      background: #fff; color: var(--ink);
      font-family: -apple-system, BlinkMacSystemFont, "Inter", "Segoe UI", sans-serif;
      min-height: 100dvh; display: flex; flex-direction: column; align-items: center;
      padding: 2rem 1.5rem 3rem; gap: 1.75rem;
    }
    .header h1 { font-size: .9rem; font-weight: 500; letter-spacing: -.01em; text-align: center; }
    .header h1 span { color: #bbb; font-weight: 400; }
    .wrap { width: 100%; max-width: 340px; display: flex; flex-direction: column; gap: 1.75rem; }
    .pairing { display: flex; flex-direction: column; align-items: center; gap: 1.5rem; }
    .qr-wrap {
      background: #fff; border: 1px solid var(--line-strong);
      border-radius: .625rem; padding: .875rem; display: inline-flex;
    }
    /* scaleX(0.83): corrects monospace char aspect ratio for block QR art. */
    .qr-wrap pre {
      font-family: "Courier New", monospace; font-size: 13px; line-height: 1;
      letter-spacing: 0; color: #000; user-select: none;
      transform: scaleX(0.83); transform-origin: center; display: block;
    }
    .pin-block { text-align: center; }
    .pin-block .label {
      font-size: .7rem; color: var(--faint);
      letter-spacing: .08em; text-transform: uppercase; margin-bottom: .5rem;
    }
    .pin-block .pin {
      font-size: 2rem; font-weight: 600; letter-spacing: .18em;
      font-variant-numeric: tabular-nums;
    }
    .pin-block .hint { margin-top: .6rem; font-size: .75rem; color: var(--faint); line-height: 1.6; }
    .health {
      display: flex; align-items: center; justify-content: center; gap: .5rem;
      font-size: .8rem; font-weight: 500; padding: .55rem; border-radius: .5rem;
      background: var(--tile); border: 1px solid var(--line);
    }
    .health.ok { color: #15803d; }
    .health.warn { color: #b45309; }
    .health.crit { color: #b91c1c; }
    .section-label {
      font-size: .66rem; color: var(--faint); letter-spacing: .09em;
      text-transform: uppercase; margin: 0 0 .55rem .1rem;
    }
    .tiles { display: grid; grid-template-columns: 1fr 1fr; gap: .5rem; }
    .tile {
      background: var(--tile); border: 1px solid var(--line);
      border-radius: .55rem; padding: .7rem .8rem;
    }
    .tile .k { font-size: .68rem; color: var(--muted); margin-bottom: .3rem; }
    .tile .v {
      font-size: 1.05rem; font-weight: 600; font-variant-numeric: tabular-nums;
      display: flex; align-items: center; gap: .4rem;
    }
    .tile .v small { font-size: .72rem; font-weight: 400; color: var(--faint); }
    .rows { border: 1px solid var(--line); border-radius: .55rem; overflow: hidden; }
    .row {
      display: flex; justify-content: space-between; align-items: center;
      padding: .62rem .8rem; font-size: .82rem; border-bottom: 1px solid var(--line);
    }
    .row:last-child { border-bottom: none; }
    .row .name { color: var(--muted); }
    .row .val { color: var(--ink); display: flex; align-items: center; gap: .45rem; font-variant-numeric: tabular-nums; }
    .row .val small { color: var(--faint); }
    .dot { width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0; }
    .dot.green { background: var(--green); }
    .dot.amber { background: var(--amber); }
    .dot.red { background: var(--red); }
    .dot.gray { background: #ccc; }
    .footer { font-size: .72rem; color: #ccc; text-align: center; padding-top: .5rem; }
  </style>
</head>
<body>
  <div class="header"><h1>${name} <span>· ${hostname}.local</span></h1></div>

  <div class="wrap">
    <div class="pairing">
      <div class="qr-wrap">${this._qrBlock}</div>
      <div class="pin-block">
        <div class="label">Setup code</div>
        <div class="pin">${pin}</div>
        <div class="hint">Home → + → Add Accessory → scan or enter code</div>
      </div>
    </div>

    <div class="health ${pill.cls}"><span class="dot ${pill.dot}"></span>${pill.text}</div>

    <div>
      <div class="section-label">System</div>
      <div class="tiles">
        <div class="tile"><div class="k">Temperature</div><div class="v">${dot(tempLevel)}${tempStr}</div></div>
        <div class="tile"><div class="k">Throttle</div><div class="v">${dot(throttle.level)}${esc(throttle.label)}</div></div>
        <div class="tile"><div class="k">CPU load</div><div class="v">${load1.toFixed(2)}<small>/ ${cores} cores</small></div></div>
        <div class="tile"><div class="k">Uptime</div><div class="v">${uptime}</div></div>
        <div class="tile"><div class="k">RAM</div><div class="v">${ram}</div></div>
        <div class="tile"><div class="k">Swap</div><div class="v">${dot(swapLevel)}${swap}</div></div>
      </div>
    </div>

    <div>
      <div class="section-label">Services</div>
      <div class="rows">
        <div class="row"><span class="name">pi4cam-homekit</span><span class="val">${dot("ok")}running</span></div>
        <div class="row"><span class="name">pi4cam</span><span class="val">${dot(pi4camLevel)}${pi4camActive === null ? "n/a" : pi4camActive ? "running" : "stopped"}</span></div>
        <div class="row"><span class="name">mediamtx</span><span class="val">${dot(mediamtxLevel)}${mediamtxOk ? `RTSP :${this.rtspPort}` : "down"}</span></div>
      </div>
    </div>

    <div>
      <div class="section-label">Camera &amp; detection</div>
      <div class="rows">
        <div class="row"><span class="name">Snapshot</span><span class="val">${dot(snapLevel)}${snapVal}</span></div>
        <div class="row"><span class="name">HKSV recording</span><span class="val">${hksvVal}</span></div>
        <div class="row"><span class="name">Last motion</span><span class="val">${motionCount} event${motionCount !== 1 ? "s" : ""}${motionAgo ? ` <small>· ${esc(motionAgo)}</small>` : ""}</span></div>
      </div>
    </div>

    <div class="footer">pi4-IA-Homekit-Camera</div>
  </div>
</body>
</html>`;
  }

  // ------------------------------------------------------------------
  // Probes (all best-effort; failures degrade to "n/a")
  // ------------------------------------------------------------------

  private async _cpuTempC(): Promise<number | null> {
    try {
      const raw = await fs.readFile("/sys/class/thermal/thermal_zone0/temp", "utf8");
      return parseInt(raw, 10) / 1000;
    } catch {
      return null;
    }
  }

  private _throttle(): Promise<{ label: string; level: Health }> {
    return new Promise((resolve) => {
      execFile("vcgencmd", ["get_throttled"], { timeout: 800 }, (err, stdout) => {
        const m = /0x[0-9a-fA-F]+/.exec(String(stdout));
        if (err || !m) {
          resolve({ label: "n/a", level: "muted" });
          return;
        }
        const v = parseInt(m[0], 16);
        if (v === 0) {
          resolve({ label: "OK", level: "ok" });
        } else if (v & 0x1 || v & 0x4 || v & 0x8) {
          // currently under-voltage / throttled / soft-temp-limited
          resolve({ label: v & 0x1 ? "under-voltage" : "throttling", level: "crit" });
        } else if (v & 0x10000 || v & 0x40000 || v & 0x20000 || v & 0x80000) {
          // occurred since boot (sticky)
          resolve({ label: v & 0x10000 ? "under-voltage (past)" : "throttled (past)", level: "warn" });
        } else {
          resolve({ label: "OK", level: "ok" });
        }
      });
    });
  }

  private async _mem(): Promise<
    { ramUsed: number; ramTotal: number; swapUsed: number; swapTotal: number } | null
  > {
    try {
      const raw = await fs.readFile("/proc/meminfo", "utf8");
      const kb = (key: string): number => {
        const m = new RegExp(`^${key}:\\s+(\\d+)`, "m").exec(raw);
        return m ? parseInt(m[1], 10) : 0;
      };
      const toMB = (x: number) => Math.round(x / 1024);
      const memTotal = kb("MemTotal");
      const swapTotal = kb("SwapTotal");
      return {
        ramUsed: toMB(memTotal - kb("MemAvailable")),
        ramTotal: toMB(memTotal),
        swapUsed: toMB(swapTotal - kb("SwapFree")),
        swapTotal: toMB(swapTotal),
      };
    } catch {
      return null;
    }
  }

  private _serviceActive(name: string): Promise<boolean | null> {
    return new Promise((resolve) => {
      execFile("systemctl", ["is-active", name], { timeout: 800 }, (_err, stdout) => {
        const s = String(stdout).trim();
        if (s === "active") resolve(true);
        else if (s === "") resolve(null); // systemctl unavailable (e.g. dev machine)
        else resolve(false);
      });
    });
  }

  private _mediamtxOk(): Promise<boolean> {
    return new Promise((resolve) => {
      const sock = new net.Socket();
      sock.setTimeout(500);
      sock.connect(this.rtspPort, "127.0.0.1", () => { sock.destroy(); resolve(true); });
      sock.on("error", () => resolve(false));
      sock.on("timeout", () => { sock.destroy(); resolve(false); });
    });
  }

  private async _snapshot(): Promise<{ fresh: boolean; ageSec: number | null }> {
    try {
      const stat = await fs.stat(this.snapshotPath);
      const ageMs = Date.now() - stat.mtimeMs;
      return { fresh: ageMs < 15_000, ageSec: Math.round(ageMs / 1000) };
    } catch {
      return { fresh: false, ageSec: null };
    }
  }
}

/** HTML-escape for ELEMENT TEXT content only (&, <, > — sufficient there).
 *  Never use inside an attribute value: quotes are deliberately not escaped
 *  because no call site puts user data in attributes — a test pins this
 *  contract. Exported for that test. */
export function esc(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function dotClass(h: Health): string {
  return h === "ok" ? "green" : h === "warn" ? "amber" : h === "crit" ? "red" : "gray";
}

function worstOf(levels: Health[]): Health {
  if (levels.includes("crit")) return "crit";
  if (levels.includes("warn")) return "warn";
  return "ok";
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
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (d > 0) return `${d}d ${h}h`;
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
