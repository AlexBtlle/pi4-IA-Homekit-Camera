/**
 * HomeKit-driven dynamic bitrate (#47).
 *
 * The hardware encoder is shared by every consumer (live viewers, HKSV
 * recordings), so this is a global policy:
 *   - no live session   → configured bitrate (full quality for recordings)
 *   - ≥1 live session   → the highest requested max_bit_rate, clamped to
 *                         [MIN_KBPS, configured]
 * Lowering is applied immediately (a viewer is stuttering right now);
 * raising is debounced so a quick close/reopen doesn't yo-yo the encoder.
 *
 * The camera side confirmed (phase-0 gate, scripts/test_dynamic_bitrate.py)
 * that the rate control follows a live change within one second, with no
 * keyframe disruption — transparent for every -c:v copy consumer.
 */

/**
 * Floor: below this the image degrades faster than networks improve.
 * Field-calibrated: HomeKit negotiates far lower (132–299 kbps observed),
 * which would be pixel mush at 1080p; 1000 kbps stays watchable on a phone
 * screen while giving constrained links (and the Zero 2 W's stream startup)
 * the lightest first GOP we're willing to serve.
 */
export const MIN_KBPS = 1000;

export function computeTargetKbps(
  sessions: number[],
  configuredKbps: number,
): number {
  if (sessions.length === 0) {
    return configuredKbps;
  }
  const wanted = Math.max(...sessions);
  return Math.min(Math.max(wanted, MIN_KBPS), configuredKbps);
}

export class BitrateGovernor {
  static readonly RAISE_DELAY_MS = 10_000;

  private readonly sessions = new Map<string, number>();
  private current: number;
  private raiseTimer?: NodeJS.Timeout;

  constructor(
    private readonly send: (kbps: number) => void,
    private readonly configuredKbps: number,
  ) {
    this.current = configuredKbps;
  }

  /** Register/refresh a live session's negotiated max bitrate (START or RECONFIGURE). */
  setSession(id: string, kbps: number | undefined): void {
    if (!kbps || kbps <= 0) {
      return; // nothing negotiated — leave the policy untouched
    }
    this.sessions.set(id, kbps);
    this.apply();
  }

  clearSession(id: string): void {
    if (this.sessions.delete(id)) {
      this.apply();
    }
  }

  private apply(): void {
    if (this.raiseTimer) {
      clearTimeout(this.raiseTimer);
      this.raiseTimer = undefined;
    }
    const target = computeTargetKbps(
      [...this.sessions.values()],
      this.configuredKbps,
    );
    if (target === this.current) {
      return;
    }
    if (target < this.current) {
      // someone is stuttering right now: act immediately
      this.current = target;
      this.send(target);
      return;
    }
    // debounced raise — recompute at fire time (sessions may have changed)
    this.raiseTimer = setTimeout(() => {
      this.raiseTimer = undefined;
      const late = computeTargetKbps(
        [...this.sessions.values()],
        this.configuredKbps,
      );
      if (late > this.current) {
        this.current = late;
        this.send(late);
      }
    }, BitrateGovernor.RAISE_DELAY_MS);
  }
}
