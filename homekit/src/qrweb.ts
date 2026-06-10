import http from "http";
import os from "os";
import qrcode from "qrcode-terminal";

/**
 * Tiny web server that serves a single HTML page with the HomeKit pairing QR
 * code and PIN — accessible from any browser on the local network at
 * http://<hostname>.local:<port> or http://<ip>:<port>.
 *
 * Lives inside the pi4cam-homekit process (zero extra service, zero extra RAM).
 * The page is built once at startup and served statically from memory.
 */
export class QrWebServer {
  private server?: http.Server;
  private page?: string;

  constructor(
    private readonly setupUri: string,
    private readonly pin: string,
    private readonly cameraName: string,
    private readonly port: number,
  ) {}

  start(): this {
    // Build the page asynchronously (qrcode-terminal uses a callback), then
    // open the server. Any request that arrives before the page is ready gets
    // a 503 — in practice this window is a few milliseconds.
    qrcode.generate(this.setupUri, { small: true }, (ascii: string) => {
      this.page = this.buildPage(ascii);
      this.listen();
    });
    return this;
  }

  stop(): void {
    this.server?.close();
  }

  private listen(): void {
    this.server = http.createServer((_req, res) => {
      if (this.page) {
        res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
        res.end(this.page);
      } else {
        res.writeHead(503);
        res.end("Starting…");
      }
    });

    this.server.listen(this.port, "0.0.0.0", () => {
      const hostname = os.hostname();
      console.log(
        `[qrweb] http://${hostname}.local:${this.port}  |  http://${localIP()}:${this.port}`,
      );
    });
  }

  private buildPage(ascii: string): string {
    const escapedAscii = ascii
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");

    const pin = this.pin.replace(/-/g, "‑"); // non-breaking hyphens

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
    footer { color: #636366; font-size: .75rem; }
  </style>
</head>
<body>
  <header>
    <h1>${esc(this.cameraName)}</h1>
    <p>HomeKit pairing</p>
  </header>
  <div class="card">
    <pre>${escapedAscii}</pre>
    <p class="pin-label">Setup code</p>
    <p class="pin">${esc(pin)}</p>
    <p class="hint">
      Open <strong>Home</strong> → <strong>+</strong> → <strong>Add Accessory</strong>
      and scan the QR code above, or tap <em>More options…</em> and enter the code.
    </p>
  </div>
  <footer>pi4-IA-Homekit-Camera</footer>
</body>
</html>`;
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
