import { ChildProcess, spawn } from "child_process";
import dgram from "dgram";
import {
  CameraController,
  CameraStreamingDelegate,
  PrepareStreamCallback,
  PrepareStreamRequest,
  PrepareStreamResponse,
  SnapshotRequest,
  SnapshotRequestCallback,
  StreamingRequest,
  StreamRequestCallback,
  StreamRequestTypes,
} from "@homebridge/hap-nodejs";

import { BitrateGovernor } from "./bitrate";
import { SnapshotProvider } from "./snapshot";

interface SessionInfo {
  address: string;
  ipv6: boolean; // controller negotiated IPv6 (request.addressVersion) — #44
  videoPort: number;
  videoReturnPort: number;
  videoSSRC: number;
  videoSRTP: Buffer; // key + salt, used as ffmpeg -srtp_out_params
}

/**
 * Reserve a free UDP port by binding to 0 and reading the assigned port.
 * HomeKit needs a return port for RTCP even though we only push video.
 * The socket family must match the controller's addressVersion: an IPv6
 * controller sends RTCP to a port that only exists if it was bound udp6 (#44).
 */
function reservePort(type: "udp4" | "udp6"): Promise<number> {
  return new Promise((resolve, reject) => {
    const socket = dgram.createSocket(type);
    socket.once("error", reject);
    socket.bind(0, () => {
      const port = (socket.address() as { port: number }).port;
      socket.close(() => resolve(port));
    });
  });
}

/**
 * Format the controller address for an ffmpeg URL: IPv6 literals need
 * brackets (`srtp://[fe80::…]:port`) — raw interpolation produces an invalid
 * URL and a black tile with no usable error (#44, beta).
 */
export function srtpHost(address: string, ipv6: boolean): string {
  return ipv6 ? `[${address}]` : address;
}

/**
 * Live streaming delegate: relays the camera's RTSP stream to HomeKit over
 * SRTP with `-codec:v copy` — no re-encoding, exactly like pi0-Camera-HomeKit.
 * The Pi's hardware H264 is passed straight through, keeping CPU near idle and
 * the stream as fluid as the source.
 */
export class StreamingDelegate implements CameraStreamingDelegate {
  controller?: CameraController;

  private readonly pendingSessions = new Map<string, SessionInfo>();
  private readonly ongoingSessions = new Map<string, ChildProcess>();

  constructor(
    private readonly rtspUrl: string,
    private readonly snapshots: SnapshotProvider,
    private readonly bitrate?: BitrateGovernor,
    private readonly forceKeyframe?: () => void,
    private readonly ffmpegPath: string = "ffmpeg",
  ) {}

  // ----------------------------------------------------------------------
  // Snapshot
  // ----------------------------------------------------------------------

  handleSnapshotRequest(
    request: SnapshotRequest,
    callback: SnapshotRequestCallback,
  ): void {
    this.snapshots
      .get()
      .then((jpeg) => callback(undefined, jpeg))
      .catch((err) => {
        console.error("[stream] snapshot failed:", err.message);
        callback(err as Error);
      });
  }

  // ----------------------------------------------------------------------
  // Stream setup
  // ----------------------------------------------------------------------

  async prepareStream(
    request: PrepareStreamRequest,
    callback: PrepareStreamCallback,
  ): Promise<void> {
    try {
      // IPv6 controllers (IPv6-preferred networks, some ISPs) negotiate an
      // IPv6 target — the return ports must be bound udp6 or RTCP never
      // lands. On a v4-only box the udp6 bind fails fast (EAFNOSUPPORT) and
      // the error is surfaced instead of a silent black tile (#44, beta).
      const ipv6 = request.addressVersion === "ipv6";
      const udpType = ipv6 ? "udp6" : "udp4";
      const videoSSRC = CameraController.generateSynchronisationSource();
      const videoReturnPort = await reservePort(udpType);
      const audioReturnPort = await reservePort(udpType);
      const audioSSRC = CameraController.generateSynchronisationSource();

      this.pendingSessions.set(request.sessionID, {
        address: request.targetAddress,
        ipv6,
        videoPort: request.video.port,
        videoReturnPort,
        videoSSRC,
        videoSRTP: Buffer.concat([
          request.video.srtp_key,
          request.video.srtp_salt,
        ]),
      });

      const response: PrepareStreamResponse = {
        video: {
          port: videoReturnPort,
          ssrc: videoSSRC,
          srtp_key: request.video.srtp_key,
          srtp_salt: request.video.srtp_salt,
        },
        // Audio is video-only on the Pi camera module, but HomeKit still
        // expects an audio block in the response. We echo the keys and never
        // send audio packets — the stream plays as video-only.
        audio: {
          port: audioReturnPort,
          ssrc: audioSSRC,
          srtp_key: request.audio.srtp_key,
          srtp_salt: request.audio.srtp_salt,
        },
      };
      callback(undefined, response);
    } catch (err) {
      console.error("[stream] prepareStream failed:", (err as Error).message);
      callback(err as Error);
    }
  }

  // ----------------------------------------------------------------------
  // Stream lifecycle
  // ----------------------------------------------------------------------

  handleStreamRequest(
    request: StreamingRequest,
    callback: StreamRequestCallback,
  ): void {
    switch (request.type) {
      case StreamRequestTypes.START:
        this.startStream(request, callback);
        break;
      case StreamRequestTypes.RECONFIGURE:
        // Resolution can't change mid-stream (passthrough), but the bitrate
        // can: forward the renegotiated cap to the encoder via the governor
        // — this is how a degrading remote/cellular link gets smooth (#47).
        console.log(
          `[stream ${request.sessionID.slice(0, 8)}] reconfigure → ` +
            `max ${request.video.max_bit_rate} kbps`,
        );
        this.bitrate?.setSession(request.sessionID, request.video.max_bit_rate);
        callback();
        break;
      case StreamRequestTypes.STOP:
        this.stopStream(request.sessionID);
        callback();
        break;
    }
  }

  private startStream(
    request: Extract<StreamingRequest, { type: StreamRequestTypes.START }>,
    callback: StreamRequestCallback,
  ): void {
    const sessionID = request.sessionID;
    const session = this.pendingSessions.get(sessionID);
    if (!session) {
      callback(new Error(`no pending session ${sessionID}`));
      return;
    }
    this.pendingSessions.delete(sessionID);

    // Drive the shared encoder toward what this viewer negotiated — ~2 Mbps
    // on a remote/cellular link, higher on the LAN (#47).
    console.log(
      `[stream ${sessionID.slice(0, 8)}] negotiated ` +
        `${request.video.width}x${request.video.height}@${request.video.fps} ` +
        `max ${request.video.max_bit_rate} kbps` +
        (session.ipv6 ? " over IPv6 (beta)" : ""),
    );
    this.bitrate?.setSession(sessionID, request.video.max_bit_rate);

    const mtu = request.video.mtu || 1316;
    const srtpParams = session.videoSRTP.toString("base64");
    const args = [
      "-hide_banner",
      "-loglevel",
      "error",
      // Low-latency input: mediamtx's RTSP SDP carries the H264 codec params,
      // so ffmpeg needs almost no probing — start copying at the first keyframe
      // instead of buffering/analysing for up to 5 s.
      "-fflags",
      "nobuffer",
      "-flags",
      "low_delay",
      "-analyzeduration",
      "0",
      "-probesize",
      "32",
      // NO "-timeout" here: field-measured, it adds 2-4 s to the RTSP
      // connection setup on the Pi's ffmpeg (#43). A hung source is covered
      // by iOS closing the session (HAP teardown kills this process).
      "-rtsp_transport",
      "tcp",
      "-i",
      this.rtspUrl,
      "-an",
      "-sn",
      "-dn",
      "-codec:v",
      "copy",
      "-f",
      "rtp",
      "-payload_type",
      String(request.video.pt),
      "-ssrc",
      String(session.videoSSRC),
      "-srtp_out_suite",
      "AES_CM_128_HMAC_SHA1_80",
      "-srtp_out_params",
      srtpParams,
      `srtp://${srtpHost(session.address, session.ipv6)}:${session.videoPort}` +
        `?rtcpport=${session.videoReturnPort}&pkt_size=${mtu}`,
    ];

    const t0 = performance.now();
    const ff = spawn(this.ffmpegPath, args);
    this.ongoingSessions.set(sessionID, ff);

    // Instrumentation (#43): splits the START→mediamtx-subscribe gap into
    // "node queued the process" (this line) vs "exec + linking + RTSP
    // handshake" (mediamtx's 'is reading' line). Field data 2026-07-05
    // showed the full gap at 5-17 s on a busy Zero 2 W — this tells which
    // half to attack next.
    ff.once("spawn", () =>
      console.log(
        `[stream ${sessionID.slice(0, 8)}] ffmpeg spawned ` +
          `+${Math.round(performance.now() - t0)} ms`,
      ),
    );

    // Ack the START immediately: iOS shows its spinner until SRTP packets
    // arrive regardless — the old wait-for-progress (with a 1.5 s fallback
    // that fired on nearly every cold start) only delayed the moment iOS
    // starts listening (#43, audit L2).
    callback();

    // The instant-startup trick (#43): ask the encoder for an immediate IDR
    // instead of letting the viewer wait out the GOP. A salvo, because the
    // keyframe only helps once OUR ffmpeg is subscribed to mediamtx — and
    // field logs (2026-07-05) show that under load the subscribe lands
    // anywhere from 4 to 17 s after START, not the ~2 s first assumed: the
    // salvo now covers that whole window. Each keyframe is a ~100 KB
    // one-off — eight of them spread over 20 s is invisible, and any that
    // fire after the session closed are cancelled below.
    this.forceKeyframe?.();
    const keyframeSalvo = [1000, 2000, 3000, 5000, 8000, 12000, 16000, 20000].map(
      (ms) => setTimeout(() => this.forceKeyframe?.(), ms),
    );

    ff.stderr.on("data", (d: Buffer) =>
      console.error(`[stream ${sessionID.slice(0, 8)}] ${d.toString().trim()}`),
    );
    ff.on("error", (e) =>
      console.error("[stream] ffmpeg spawn error:", e.message),
    );
    ff.on("close", (code) => {
      keyframeSalvo.forEach(clearTimeout);
      const wasOngoing = this.ongoingSessions.delete(sessionID);
      if (code !== 0 && code !== null) {
        console.error(`[stream ${sessionID.slice(0, 8)}] ffmpeg exited ${code}`);
        if (wasOngoing) {
          // ffmpeg died mid-session (a HAP-initiated stop removes the session
          // first, then SIGKILLs → code null). Tell the controller, or iOS
          // keeps a frozen tile until the user closes it by hand (#38).
          this.controller?.forceStopStreamingSession(sessionID);
        }
      }
    });
  }

  private stopStream(sessionID: string): void {
    const ff = this.ongoingSessions.get(sessionID);
    if (ff) {
      ff.kill("SIGKILL");
      this.ongoingSessions.delete(sessionID);
    }
    this.pendingSessions.delete(sessionID);
    // Last viewer gone → the governor restores full quality (debounced).
    this.bitrate?.clearSession(sessionID);
  }
}
