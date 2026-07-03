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

import { SnapshotProvider } from "./snapshot";

interface SessionInfo {
  address: string;
  videoPort: number;
  videoReturnPort: number;
  videoSSRC: number;
  videoSRTP: Buffer; // key + salt, used as ffmpeg -srtp_out_params
}

/**
 * Reserve a free UDP port by binding to 0 and reading the assigned port.
 * HomeKit needs a return port for RTCP even though we only push video.
 */
function reservePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const socket = dgram.createSocket("udp4");
    socket.once("error", reject);
    socket.bind(0, () => {
      const port = (socket.address() as { port: number }).port;
      socket.close(() => resolve(port));
    });
  });
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
      const videoSSRC = CameraController.generateSynchronisationSource();
      const videoReturnPort = await reservePort();
      const audioReturnPort = await reservePort();
      const audioSSRC = CameraController.generateSynchronisationSource();

      this.pendingSessions.set(request.sessionID, {
        address: request.targetAddress,
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
        // Passthrough copy can't change bitrate/resolution mid-stream; the
        // source is fixed. Acknowledge so HomeKit keeps the existing stream.
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

    const mtu = request.video.mtu || 1316;
    const srtpParams = session.videoSRTP.toString("base64");
    const args = [
      "-hide_banner",
      "-loglevel",
      "error",
      // Program-friendly progress on stdout → we know when frames start flowing.
      "-progress",
      "pipe:1",
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
      // Socket I/O timeout (µs): a hung RTSP source makes ffmpeg exit
      // instead of leaving a silently frozen live session behind (#34).
      "-timeout",
      "10000000",
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
      `srtp://${session.address}:${session.videoPort}` +
        `?rtcpport=${session.videoReturnPort}&pkt_size=${mtu}`,
    ];

    const ff = spawn("ffmpeg", args);
    this.ongoingSessions.set(sessionID, ff);

    let started = false;
    const ready = () => {
      if (!started) {
        started = true;
        callback();
      }
    };
    // ffmpeg writes progress lines once frames flow → the stream is live.
    ff.stdout.on("data", ready);
    // Fallback: if no progress arrives quickly, ack anyway so HomeKit proceeds.
    const readyTimer = setTimeout(ready, 1500);

    ff.stderr.on("data", (d: Buffer) =>
      console.error(`[stream ${sessionID.slice(0, 8)}] ${d.toString().trim()}`),
    );
    ff.on("error", (e) => {
      console.error("[stream] ffmpeg spawn error:", e.message);
      clearTimeout(readyTimer);
      if (!started) {
        started = true;
        callback(e);
      }
    });
    ff.on("close", (code) => {
      clearTimeout(readyTimer);
      this.ongoingSessions.delete(sessionID);
      if (code !== 0 && code !== null) {
        console.error(`[stream ${sessionID.slice(0, 8)}] ffmpeg exited ${code}`);
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
  }
}
