import { AddressInfo } from "net";
import { afterEach, describe, expect, test } from "vitest";
import WebSocket, { WebSocketServer } from "ws";

import { Go2RtcEngine } from "../src/hks-v2/webrtc-engine";

/** Minimal stand-in for the PATCHED go2rtc WS endpoint. */
function fakeGo2rtc(
  onMessage: (msg: { type: string; value?: string }, sock: WebSocket) => void,
): Promise<{ url: string; close: () => void; sockets: WebSocket[] }> {
  return new Promise((resolve) => {
    const wss = new WebSocketServer({ port: 0, path: "/api/ws" });
    const sockets: WebSocket[] = [];
    wss.on("connection", (sock) => {
      sockets.push(sock);
      sock.on("message", (data) => onMessage(JSON.parse(String(data)), sock));
    });
    wss.on("listening", () => {
      const { port } = wss.address() as AddressInfo;
      resolve({
        url: `http://127.0.0.1:${port}`,
        close: () => wss.close(),
        sockets,
      });
    });
  });
}

let cleanup: (() => void) | undefined;
afterEach(() => cleanup?.());

describe("Go2RtcEngine (patched-protocol client)", () => {
  test("solicit → offer, answer forwarded on the same socket", async () => {
    const received: { type: string; value?: string }[] = [];
    const srv = await fakeGo2rtc((msg, sock) => {
      received.push(msg);
      if (msg.type === "webrtc/solicit") {
        // Early candidate first — the client must keep waiting for the offer.
        sock.send(JSON.stringify({ type: "webrtc/candidate", value: "candidate:1" }));
        sock.send(JSON.stringify({ type: "webrtc/offer", value: "v=0 fake-offer" }));
      }
    });
    cleanup = srv.close;

    const session = await new Go2RtcEngine(srv.url).solicit("camera_hevc_high");
    expect(session.offer).toBe("v=0 fake-offer");

    await session.provideAnswer("v=0 fake-answer");
    await new Promise((r) => setTimeout(r, 50));
    expect(received).toEqual([
      { type: "webrtc/solicit" },
      { type: "webrtc/answer", value: "v=0 fake-answer" },
    ]);
    session.close();
  });

  test("a go2rtc error before the offer rejects the solicit", async () => {
    const srv = await fakeGo2rtc((msg, sock) => {
      sock.send(JSON.stringify({ type: "error", value: "stream not found" }));
    });
    cleanup = srv.close;
    await expect(
      new Go2RtcEngine(srv.url).solicit("nope"),
    ).rejects.toThrow(/stream not found/);
  });

  test("a go2rtc error after establishment closes the session", async () => {
    const srv = await fakeGo2rtc((msg, sock) => {
      if (msg.type === "webrtc/solicit") {
        sock.send(JSON.stringify({ type: "webrtc/offer", value: "v=0" }));
      } else if (msg.type === "webrtc/answer") {
        sock.send(JSON.stringify({ type: "error", value: "sdp: syntax error" }));
      }
    });
    cleanup = srv.close;

    const session = await new Go2RtcEngine(srv.url).solicit("src");
    const closed = new Promise<string>((r) => (session.onClosed = r));
    await session.provideAnswer("not-sdp");
    await expect(closed).resolves.toMatch(/sdp: syntax error/);
  });

  test("unreachable go2rtc rejects with a clear error", async () => {
    await expect(
      new Go2RtcEngine("http://127.0.0.1:1").solicit("src"),
    ).rejects.toThrow(/unreachable/);
  });
});
