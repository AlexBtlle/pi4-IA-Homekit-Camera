import {
  CameraRecordingConfiguration,
  CameraRecordingDelegate,
  HDSProtocolSpecificErrorReason,
  RecordingPacket,
} from "@homebridge/hap-nodejs";

import { Prebuffer } from "./prebuffer";

/**
 * HomeKit Secure Video recording delegate.
 *
 * HomeKit arms recording via updateRecordingActive(true); we then keep a
 * fragmented-MP4 prebuffer running. When the motion sensor fires, HomeKit
 * opens a recording stream and we feed it the prebuffered fragments followed
 * by live ones, so the iCloud clip starts a few seconds before the motion.
 * The Apple TV / HomePod does the People / Animals / Vehicles classification.
 */
export class RecordingDelegate implements CameraRecordingDelegate {
  private readonly prebuffer: Prebuffer;
  private configuration?: CameraRecordingConfiguration;
  private readonly streams = new Map<number, AbortController>();
  private _active = false;

  // Diagnostic only (#57 investigation): how long after the motion webhook
  // fired did the home hub actually open this recording stream. Wired from
  // main.ts to MotionService.msSinceLastTrigger — if this consistently
  // approaches or exceeds prebufferLength, the pre-roll window is fully
  // consumed by round-trip latency before the hub ever asks for it, and the
  // clip starts at the motion even though the Pi served a full prebuffer.
  motionAgeProvider?: () => number | undefined;

  constructor(rtspUrl: string, ffmpegPath?: string) {
    this.prebuffer = new Prebuffer(rtspUrl, ffmpegPath);
  }

  /** Whether HomeKit currently has recording armed (drives the status page). */
  get recordingActive(): boolean {
    return this._active;
  }

  updateRecordingActive(active: boolean): void {
    this._active = active;
    if (active) {
      this.prebuffer.start(); // idempotent — belt and braces
    }
    // Deliberately NOT stopping on disarm. Field log (#57): the home hub arms
    // recording LAZILY — "recording armed" lands at the exact second the
    // motion stream opens, "disarmed" a minute later when the clip ends.
    // Gating the prebuffer on Active therefore spawned its ffmpeg AT the
    // motion: the pre-motion seconds were never captured and every clip
    // started at the trigger ("serving 0 pre-roll — ring holds 0"). The
    // prebuffer's lifetime is tied to the recording CONFIGURATION below —
    // set while recording is enabled in the Home app, cleared when disabled.
    console.log(`[hksv] recording ${active ? "armed" : "disarmed"}`);
  }

  updateRecordingConfiguration(
    configuration: CameraRecordingConfiguration | undefined,
  ): void {
    this.configuration = configuration;
    // Pre-roll only exists if the prebuffer was already running BEFORE the
    // motion, so run it whenever recording is enabled in the Home app:
    // HAP-NodeJS delivers the selected configuration here on change AND
    // replays the persisted one at boot; it delivers `undefined` when the
    // user disables recording (RecordingManagement.js: deserialize replays
    // updateRecordingConfiguration; disable calls it with undefined).
    if (configuration) {
      this.prebuffer.start();
      console.log("[hksv] recording configured — prebuffer running");
    } else {
      this.prebuffer.stop();
      console.log("[hksv] recording configuration removed — prebuffer stopped");
    }
  }

  async *handleRecordingStreamRequest(
    streamId: number,
    signal?: AbortSignal,
  ): AsyncGenerator<RecordingPacket> {
    const prebufferMs = this.configuration?.prebufferLength ?? 4000;
    const ac = new AbortController();
    if (signal?.aborted) {
      ac.abort();
    } else {
      signal?.addEventListener("abort", () => ac.abort(), { once: true });
    }
    this.streams.set(streamId, ac);
    const motionAge = this.motionAgeProvider?.();
    const ageNote =
      motionAge === undefined ? "" : `, ${Math.round(motionAge)}ms after motion trigger`;
    console.log(`[hksv] stream ${streamId} started (prebuffer ${prebufferMs}ms${ageNote})`);

    try {
      // Stream fragments as they are produced until the home hub closes the
      // session (reason NORMAL once motion ends). Holding fragments back to
      // mark the last one with isLast would delay every fragment by one GOP
      // and lose the held fragment when HAP-NodeJS returns the generator on
      // close — truncating each clip. Ending on the hub's close is the path
      // the HAP spec describes, so isLast stays false throughout.
      for await (const data of this.prebuffer.segments(prebufferMs, ac.signal)) {
        yield { data, isLast: false };
      }
    } finally {
      this.streams.delete(streamId);
      console.log(`[hksv] stream ${streamId} ended`);
    }
  }

  acknowledgeStream(streamId: number): void {
    this.streams.get(streamId)?.abort();
  }

  closeRecordingStream(
    streamId: number,
    reason: HDSProtocolSpecificErrorReason | undefined,
  ): void {
    this.streams.get(streamId)?.abort();
    if (reason !== undefined) {
      console.log(`[hksv] stream ${streamId} closed (reason ${reason})`);
    }
  }
}
