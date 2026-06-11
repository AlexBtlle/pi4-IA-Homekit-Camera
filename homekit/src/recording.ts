import {
  CameraRecordingConfiguration,
  CameraRecordingDelegate,
  HDSProtocolSpecificErrorReason,
  RecordingPacket,
} from "hap-nodejs";

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

  constructor(rtspUrl: string) {
    this.prebuffer = new Prebuffer(rtspUrl);
  }

  updateRecordingActive(active: boolean): void {
    if (active) {
      this.prebuffer.start();
    } else {
      this.prebuffer.stop();
    }
    console.log(`[hksv] recording ${active ? "armed" : "disarmed"}`);
  }

  updateRecordingConfiguration(
    configuration: CameraRecordingConfiguration | undefined,
  ): void {
    this.configuration = configuration;
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
    console.log(`[hksv] stream ${streamId} started (prebuffer ${prebufferMs}ms)`);

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
