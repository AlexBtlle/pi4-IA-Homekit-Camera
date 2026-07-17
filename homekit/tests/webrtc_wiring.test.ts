import { describe, expect, test } from "vitest";

import {
  buildAudioStreamTiers,
  buildVideoStreamTiers,
  defaultTiers2K,
} from "../src/hks-v2/payloads";
import {
  SolicitOfferStatus,
  VideoCodec,
  WebRTCStreamingStatus,
} from "../src/hks-v2/spec";
import {
  WebRTCNumberOfActiveSessionsCharacteristic,
  WebRTCProvideAnswerCharacteristic,
  WebRTCSolicitOfferCharacteristic,
  WebRTCStreamingControlCharacteristic,
} from "../src/hks-v2/services";
import { decodeTlv8, encodeTlv8, tlvGet, uint8, utf8 } from "../src/hks-v2/tlv8";
import { EngineSession, WebRtcEngine } from "../src/hks-v2/webrtc-engine";
import { WebRTCSessionManager } from "../src/hks-v2/webrtc-sessions";
import { WebRTCManagementWiring } from "../src/hks-v2/webrtc-wiring";

class FakeSession implements EngineSession {
  onClosed?: (reason: string) => void;
  answers: string[] = [];
  constructor(readonly offer: string) {}
  async provideAnswer(sdp: string): Promise<void> {
    this.answers.push(sdp);
  }
  close(): void {}
}

class FakeEngine implements WebRtcEngine {
  last?: FakeSession;
  srcs: string[] = [];
  async solicit(src: string): Promise<EngineSession> {
    this.srcs.push(src);
    this.last = new FakeSession("v=0 the-offer");
    return this.last;
  }
}

function makeWiring() {
  const engine = new FakeEngine();
  const wiring = new WebRTCManagementWiring(new WebRTCSessionManager(engine), {
    videoTiersPayload: buildVideoStreamTiers(VideoCodec.H265, 96, defaultTiers2K()),
    audioTiersPayload: buildAudioStreamTiers(97),
    sensorUuid: Buffer.alloc(16, 0x42),
    defaultSrc: "camera_high",
  });
  return { engine, wiring };
}

/** Drive a characteristic exactly like a HAP write-response request. */
async function writeChar(
  wiring: WebRTCManagementWiring,
  charClass: { UUID: string },
  payload: Buffer,
): Promise<ReturnType<typeof decodeTlv8>> {
  const char = wiring.service.getCharacteristic(charClass as never)!;
  const resp = await (char as unknown as {
    handleSetRequest(v: string): Promise<string>;
  }).handleSetRequest(payload.toString("base64"));
  return decodeTlv8(Buffer.from(resp, "base64"));
}

describe("WebRTCManagementWiring", () => {
  test("solicit write-response carries session id + SDP offer + Success", async () => {
    const { engine, wiring } = makeWiring();
    const resp = await writeChar(
      wiring,
      WebRTCSolicitOfferCharacteristic,
      encodeTlv8([{ type: 1, data: encodeTlv8([{ type: 1, data: uint8(0) }]) }]),
    );
    expect(tlvGet(resp, 1)).toHaveLength(16);
    expect(tlvGet(resp, 2)!.toString()).toBe("v=0 the-offer");
    expect(tlvGet(resp, 4)![0]).toBe(SolicitOfferStatus.Success);
    expect(engine.srcs).toEqual(["camera_high"]);
  });

  test("full happy path: solicit → answer → end, count follows", async () => {
    const { engine, wiring } = makeWiring();
    const active = wiring.service.getCharacteristic(
      WebRTCNumberOfActiveSessionsCharacteristic,
    )!;

    const offerResp = await writeChar(
      wiring,
      WebRTCSolicitOfferCharacteristic,
      Buffer.alloc(0),
    );
    const sid = tlvGet(offerResp, 1)!;
    expect(active.value).toBe(1);

    const answerResp = await writeChar(
      wiring,
      WebRTCProvideAnswerCharacteristic,
      encodeTlv8([
        { type: 1, data: sid },
        { type: 2, data: utf8("v=0 controller-answer") },
      ]),
    );
    expect(tlvGet(answerResp, 2)![0]).toBe(WebRTCStreamingStatus.Success);
    expect(engine.last!.answers).toEqual(["v=0 controller-answer"]);

    const endResp = await writeChar(
      wiring,
      WebRTCStreamingControlCharacteristic,
      encodeTlv8([
        { type: 1, data: sid },
        { type: 2, data: uint8(1) }, // Command: End
      ]),
    );
    expect(tlvGet(endResp, 2)![0]).toBe(WebRTCStreamingStatus.Success);
    expect(active.value).toBe(0);
  });

  test("answer for an unknown session returns UnknownSessionIdentifier", async () => {
    const { wiring } = makeWiring();
    const resp = await writeChar(
      wiring,
      WebRTCProvideAnswerCharacteristic,
      encodeTlv8([
        { type: 1, data: Buffer.alloc(16, 7) },
        { type: 2, data: utf8("v=0") },
      ]),
    );
    expect(tlvGet(resp, 2)![0]).toBe(
      WebRTCStreamingStatus.UnknownSessionIdentifier,
    );
  });

  test("streaming disabled → PrivacyModeActive, no engine session", async () => {
    const { engine, wiring } = makeWiring();
    wiring.streamingEnabled = false;
    const resp = await writeChar(
      wiring,
      WebRTCSolicitOfferCharacteristic,
      Buffer.alloc(0),
    );
    expect(tlvGet(resp, 4)![0]).toBe(SolicitOfferStatus.PrivacyModeActive);
    expect(engine.srcs).toEqual([]);
  });

  test("engine failure maps to Status=Error", async () => {
    const wiring = new WebRTCManagementWiring(
      new WebRTCSessionManager({
        solicit: async () => {
          throw new Error("go2rtc down");
        },
      }),
      {
        videoTiersPayload: Buffer.alloc(0),
        audioTiersPayload: Buffer.alloc(0),
        sensorUuid: Buffer.alloc(16),
        defaultSrc: "src",
      },
    );
    const resp = await writeChar(
      wiring,
      WebRTCSolicitOfferCharacteristic,
      Buffer.alloc(0),
    );
    expect(tlvGet(resp, 4)![0]).toBe(SolicitOfferStatus.Error);
  });
});
