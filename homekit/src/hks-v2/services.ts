/**
 * HAP Service/Characteristic subclasses for the new HKSV spec (#59).
 *
 * Same pattern hap-nodejs uses internally for every stock definition
 * (class extends Characteristic/Service, static UUID, addCharacteristic in
 * the constructor) — written by us because upstream has no knowledge of
 * these UUIDs yet. Built on public exports only: no fork, no internal
 * imports, survives hap-nodejs version bumps.
 */
import {
  Access,
  Characteristic,
  Formats,
  Perms,
  Service,
} from "@homebridge/hap-nodejs";

import { CharacteristicUuid, ServiceUuid } from "./spec";

// ---------------------------------------------------------------------------
// Characteristics
// ---------------------------------------------------------------------------

/** tlv8, Paired Read + Paired Write + Write Response — the §4.16-4.22
 *  command characteristics all share this shape. */
function commandProps() {
  return {
    format: Formats.TLV8,
    perms: [Perms.PAIRED_READ, Perms.PAIRED_WRITE, Perms.WRITE_RESPONSE],
  };
}

export class CameraCapabilitiesCharacteristic extends Characteristic {
  static readonly UUID = CharacteristicUuid.CameraCapabilities;
  constructor() {
    super("Camera Capabilities", CameraCapabilitiesCharacteristic.UUID, {
      format: Formats.TLV8,
      perms: [Perms.PAIRED_READ],
    });
  }
}

export class SensorUuidCharacteristic extends Characteristic {
  static readonly UUID = CharacteristicUuid.SensorUuid;
  constructor() {
    super("Sensor UUID", SensorUuidCharacteristic.UUID, {
      format: Formats.DATA,
      perms: [Perms.PAIRED_READ],
    });
  }
}

export class StreamingEnabledCharacteristic extends Characteristic {
  static readonly UUID = CharacteristicUuid.StreamingEnabled;
  constructor() {
    super("Streaming Enabled", StreamingEnabledCharacteristic.UUID, {
      format: Formats.BOOL,
      perms: [
        Perms.PAIRED_READ,
        Perms.PAIRED_WRITE,
        Perms.NOTIFY,
        Perms.TIMED_WRITE,
      ],
      // spec: AdminOnly — restrict every access kind to admin controllers
      adminOnlyAccess: [Access.READ, Access.WRITE, Access.NOTIFY],
    });
  }
}

export class WebRTCSolicitOfferCharacteristic extends Characteristic {
  static readonly UUID = CharacteristicUuid.WebRTCSolicitOffer;
  constructor() {
    super("WebRTC Solicit Offer", WebRTCSolicitOfferCharacteristic.UUID, commandProps());
  }
}

export class WebRTCProvideAnswerCharacteristic extends Characteristic {
  static readonly UUID = CharacteristicUuid.WebRTCProvideAnswer;
  constructor() {
    super("WebRTC Provide Answer", WebRTCProvideAnswerCharacteristic.UUID, commandProps());
  }
}

export class WebRTCStreamingControlCharacteristic extends Characteristic {
  static readonly UUID = CharacteristicUuid.WebRTCStreamingControl;
  constructor() {
    super("WebRTC Streaming Control", WebRTCStreamingControlCharacteristic.UUID, commandProps());
  }
}

export class WebRTCNumberOfActiveSessionsCharacteristic extends Characteristic {
  static readonly UUID = CharacteristicUuid.WebRTCNumberOfActiveSessions;
  constructor() {
    super(
      "WebRTC Number of Active Sessions",
      WebRTCNumberOfActiveSessionsCharacteristic.UUID,
      {
        format: Formats.UINT8,
        perms: [Perms.PAIRED_READ, Perms.NOTIFY],
        minValue: 0,
        maxValue: 255,
      },
    );
  }
}

export class WebRTCReofferCharacteristic extends Characteristic {
  static readonly UUID = CharacteristicUuid.WebRTCReoffer;
  constructor() {
    super("WebRTC Reoffer", WebRTCReofferCharacteristic.UUID, commandProps());
  }
}

export class WebRTCUpdateSessionCharacteristic extends Characteristic {
  static readonly UUID = CharacteristicUuid.WebRTCUpdateSession;
  constructor() {
    super("WebRTC Update Session", WebRTCUpdateSessionCharacteristic.UUID, commandProps());
  }
}

export class WebRTCSupportedVideoStreamTiersCharacteristic extends Characteristic {
  static readonly UUID = CharacteristicUuid.WebRTCSupportedVideoStreamTiers;
  constructor() {
    super(
      "WebRTC Supported Video Stream Tiers",
      WebRTCSupportedVideoStreamTiersCharacteristic.UUID,
      { format: Formats.TLV8, perms: [Perms.PAIRED_READ, Perms.NOTIFY] },
    );
  }
}

export class WebRTCSupportedAudioStreamTiersCharacteristic extends Characteristic {
  static readonly UUID = CharacteristicUuid.WebRTCSupportedAudioStreamTiers;
  constructor() {
    super(
      "WebRTC Supported Audio Stream Tiers",
      WebRTCSupportedAudioStreamTiersCharacteristic.UUID,
      { format: Formats.TLV8, perms: [Perms.PAIRED_READ, Perms.NOTIFY] },
    );
  }
}

export class RtpStreamingControlCharacteristic extends Characteristic {
  static readonly UUID = CharacteristicUuid.RtpStreamingControl;
  constructor() {
    super("RTP Streaming Control", RtpStreamingControlCharacteristic.UUID, commandProps());
  }
}

export class SupportedVideoStreamTiersCharacteristic extends Characteristic {
  static readonly UUID = CharacteristicUuid.SupportedVideoStreamTiers;
  constructor() {
    super(
      "Supported Video Stream Tiers",
      SupportedVideoStreamTiersCharacteristic.UUID,
      { format: Formats.TLV8, perms: [Perms.PAIRED_READ, Perms.NOTIFY] },
    );
  }
}

export class SupportedAudioStreamTiersCharacteristic extends Characteristic {
  static readonly UUID = CharacteristicUuid.SupportedAudioStreamTiers;
  constructor() {
    super(
      "Supported Audio Stream Tiers",
      SupportedAudioStreamTiersCharacteristic.UUID,
      { format: Formats.TLV8, perms: [Perms.PAIRED_READ, Perms.NOTIFY] },
    );
  }
}

// ---------------------------------------------------------------------------
// Services
// ---------------------------------------------------------------------------

/**
 * §3.1 Camera Capabilities — "the primary mechanism by which an accessory
 * will advertise its capabilities"; its presence with the spec's Version
 * string is what tells a controller this camera speaks the new protocol.
 * Required: Version (stock HAP characteristic), Camera Capabilities.
 */
export class CameraCapabilitiesService extends Service {
  static readonly UUID = ServiceUuid.CameraCapabilities;
  constructor(subtype?: string) {
    super("Camera Capabilities", CameraCapabilitiesService.UUID, subtype);
    this.addCharacteristic(Characteristic.Version);
    this.addCharacteristic(CameraCapabilitiesCharacteristic);
  }
}

/**
 * §3.2 Camera Global Operating Mode — required: HomeKit Camera Active and
 * Camera Operating Mode Indicator (both stock R17 characteristics) plus the
 * new Streaming Enabled.
 */
export class CameraGlobalOperatingModeService extends Service {
  static readonly UUID = ServiceUuid.CameraGlobalOperatingMode;
  constructor(subtype?: string) {
    super(
      "Camera Global Operating Mode",
      CameraGlobalOperatingModeService.UUID,
      subtype,
    );
    this.addCharacteristic(Characteristic.HomeKitCameraActive);
    this.addCharacteristic(StreamingEnabledCharacteristic);
    this.addCharacteristic(Characteristic.CameraOperatingModeIndicator);
  }
}

/**
 * §3.6 Camera Multi-Tier RTP Stream Management — the new spec's LAN
 * streaming service (≥ 5 simultaneous RTP sessions). Field observation
 * (tvOS 27 beta, 2026-07): with only the WebRTC service present the
 * controller never solicited an offer, so this service is likely the
 * preferred live path — the probe carries it to confirm.
 */
export class CameraMultiTierRtpStreamManagementService extends Service {
  static readonly UUID = ServiceUuid.CameraMultiTierRtpStreamManagement;
  constructor(subtype?: string) {
    super(
      "Camera Multi-Tier RTP Stream Management",
      CameraMultiTierRtpStreamManagementService.UUID,
      subtype,
    );
    this.addCharacteristic(StreamingEnabledCharacteristic);
    this.addCharacteristic(Characteristic.StatusActive);
    this.addCharacteristic(SupportedVideoStreamTiersCharacteristic);
    this.addCharacteristic(SupportedAudioStreamTiersCharacteristic);
    this.addCharacteristic(Characteristic.SupportedRTPConfiguration);
    this.addCharacteristic(Characteristic.SetupEndpoints);
    this.addCharacteristic(RtpStreamingControlCharacteristic);
    this.addCharacteristic(SensorUuidCharacteristic);
  }
}

/**
 * §3.7 Camera WebRTC Stream Management — all characteristics required.
 * ≥ 6 simultaneous WebRTC sessions must be supported by the accessory.
 */
export class CameraWebRTCStreamManagementService extends Service {
  static readonly UUID = ServiceUuid.CameraWebRTCStreamManagement;
  constructor(subtype?: string) {
    super(
      "Camera WebRTC Stream Management",
      CameraWebRTCStreamManagementService.UUID,
      subtype,
    );
    this.addCharacteristic(WebRTCSolicitOfferCharacteristic);
    this.addCharacteristic(WebRTCProvideAnswerCharacteristic);
    this.addCharacteristic(WebRTCStreamingControlCharacteristic);
    this.addCharacteristic(WebRTCNumberOfActiveSessionsCharacteristic);
    this.addCharacteristic(WebRTCReofferCharacteristic);
    this.addCharacteristic(WebRTCUpdateSessionCharacteristic);
    this.addCharacteristic(WebRTCSupportedVideoStreamTiersCharacteristic);
    this.addCharacteristic(WebRTCSupportedAudioStreamTiersCharacteristic);
    this.addCharacteristic(StreamingEnabledCharacteristic);
    this.addCharacteristic(SensorUuidCharacteristic);
  }
}
