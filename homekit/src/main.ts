import fs from "fs";
import path from "path";
import qrcode from "qrcode-terminal";
import {
  Accessory,
  AudioBitrate,
  AudioRecordingCodecType,
  AudioRecordingSamplerate,
  AudioStreamingCodecType,
  AudioStreamingSamplerate,
  CameraController,
  CameraControllerOptions,
  Categories,
  Characteristic,
  H264Level,
  H264Profile,
  HAPStorage,
  MediaContainerType,
  Service,
  SRTPCryptoSuites,
  uuid,
  VideoCodecType,
} from "hap-nodejs";

import { homekitDir, loadConfig, loadPairing } from "./config";
import { SnapshotProvider } from "./snapshot";
import { StreamingDelegate } from "./streaming";
import { RecordingDelegate } from "./recording";
import { MotionService } from "./motion";
import { QrWebServer } from "./qrweb";

function main(): void {
  const config = loadConfig();
  const pairing = loadPairing();

  // HAP-NodeJS persists pairing state here; keep it next to pairing.json so a
  // single directory holds all the accessory's identity.
  const persistDir = path.join(homekitDir(), "persist");
  fs.mkdirSync(persistDir, { recursive: true });
  HAPStorage.setCustomStoragePath(persistDir);

  const accessoryUUID = uuid.generate(`pi4cam:${pairing.username}`);
  const accessory = new Accessory(config.cameraName, accessoryUUID);

  accessory
    .getService(Service.AccessoryInformation)!
    .setCharacteristic(Characteristic.Manufacturer, "pi4-IA-Homekit-Camera")
    .setCharacteristic(Characteristic.Model, "Raspberry Pi 4 Camera")
    .setCharacteristic(Characteristic.SerialNumber, pairing.username)
    .setCharacteristic(Characteristic.FirmwareRevision, "2.0.0");

  const snapshots = new SnapshotProvider(config.rtspUrl);
  const streamingDelegate = new StreamingDelegate(config.rtspUrl, snapshots);
  const recordingDelegate = new RecordingDelegate(config.rtspUrl);

  const videoResolutions: [number, number, number][] = [
    [config.width, config.height, config.fps],
    [1280, 720, config.fps],
    [640, 360, config.fps],
    [320, 240, config.fps],
  ];

  const options: CameraControllerOptions = {
    cameraStreamCount: 2, // allow a couple of simultaneous viewers
    delegate: streamingDelegate,
    streamingOptions: {
      supportedCryptoSuites: [SRTPCryptoSuites.AES_CM_128_HMAC_SHA1_80],
      video: {
        codec: {
          profiles: [H264Profile.BASELINE, H264Profile.MAIN, H264Profile.HIGH],
          levels: [H264Level.LEVEL3_1, H264Level.LEVEL3_2, H264Level.LEVEL4_0],
        },
        // Cap at what the Pi actually produces so HomeKit never asks for more.
        resolutions: videoResolutions,
      },
      // Video-only camera module: we declare an audio codec because HomeKit
      // requires one, but never emit audio packets.
      audio: {
        twoWayAudio: false,
        codecs: [
          {
            type: AudioStreamingCodecType.AAC_ELD,
            samplerate: AudioStreamingSamplerate.KHZ_16,
          },
        ],
      },
    },
    // HomeKit Secure Video. The motion sensor below is the trigger; on motion
    // (while recording is enabled in Home) the delegate streams fragmented MP4
    // to the home hub, which records to iCloud and classifies the activity.
    recording: {
      options: {
        prebufferLength: 4000, // ms kept before the trigger
        // EventTriggerOption.MOTION is derived automatically from sensors.motion.
        mediaContainerConfiguration: {
          type: MediaContainerType.FRAGMENTED_MP4,
          fragmentLength: 4000,
        },
        video: {
          type: VideoCodecType.H264,
          parameters: {
            profiles: [
              H264Profile.BASELINE,
              H264Profile.MAIN,
              H264Profile.HIGH,
            ],
            levels: [H264Level.LEVEL3_1, H264Level.LEVEL3_2, H264Level.LEVEL4_0],
          },
          // Recording is pure passthrough (-c:v copy): only advertise the
          // resolution the camera actually produces, so the hub can never
          // select one we cannot deliver.
          resolutions: [[config.width, config.height, config.fps]],
        },
        audio: {
          codecs: [
            {
              type: AudioRecordingCodecType.AAC_LC,
              audioChannels: 1,
              samplerate: AudioRecordingSamplerate.KHZ_32,
              bitrateMode: AudioBitrate.VARIABLE,
            },
          ],
        },
      },
      delegate: recordingDelegate,
    },
    // Motion sensor managed by the controller; it is the HKSV recording trigger.
    sensors: {
      motion: true,
    },
  };

  const controller = new CameraController(options);
  streamingDelegate.controller = controller;
  accessory.configureController(controller);

  const motion = new MotionService(
    controller,
    config.motionPort,
    config.motionTimeout,
  );

  accessory.publish({
    username: pairing.username,
    pincode: pairing.pincode as `${number}${number}${number}-${number}${number}-${number}${number}${number}`,
    category: Categories.IP_CAMERA,
    setupID: pairing.setupID,
    addIdentifyingMaterial: true,
  });

  const qrWeb = new QrWebServer(
    accessory.setupURI(),
    pairing.pincode,
    config.cameraName,
    config.qrWebPort,
  ).start();

  motion.start();
  snapshots.start();
  printPairing(accessory, config.cameraName, pairing.pincode);

  const shutdown = () => {
    console.log("\n[pi4cam-homekit] shutting down…");
    qrWeb.stop();
    snapshots.stop();
    motion.stop();
    accessory.unpublish();
    process.exit(0);
  };
  process.on("SIGINT", shutdown);
  process.on("SIGTERM", shutdown);
}

function printPairing(
  accessory: Accessory,
  name: string,
  pincode: string,
): void {
  const uri = accessory.setupURI();
  console.log("");
  console.log("==========================================================");
  console.log(`  ${name} — ready to pair in the Home app`);
  console.log("");
  qrcode.generate(uri, { small: true });
  console.log(`  PIN: ${pincode}`);
  console.log("");
  console.log("  Home app → + → Add Accessory → scan the QR code");
  console.log("  (or 'More options…' → enter the PIN manually)");
  console.log("==========================================================");
  console.log("");
}

main();
