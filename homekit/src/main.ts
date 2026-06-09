import fs from "fs";
import path from "path";
import qrcode from "qrcode-terminal";
import {
  Accessory,
  AudioStreamingCodecType,
  AudioStreamingSamplerate,
  CameraController,
  CameraControllerOptions,
  Categories,
  Characteristic,
  H264Level,
  H264Profile,
  HAPStorage,
  Service,
  SRTPCryptoSuites,
  uuid,
} from "hap-nodejs";

import { homekitDir, loadConfig, loadPairing } from "./config";
import { SnapshotProvider } from "./snapshot";
import { StreamingDelegate } from "./streaming";
import { MotionService } from "./motion";

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
        resolutions: [
          [config.width, config.height, config.fps],
          [1280, 720, config.fps],
          [640, 360, config.fps],
          [320, 240, config.fps],
        ],
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
    // Motion sensor managed by the controller. In Jalon 2 the same sensor will
    // be tied to the HKSV recording delegate.
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

  motion.start();
  printPairing(accessory, config.cameraName, pairing.pincode);

  const shutdown = () => {
    console.log("\n[pi4cam-homekit] shutting down…");
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
