import { networkInterfaces } from "os";

/** Injectable for tests; matches os.networkInterfaces(). */
export type InterfaceReader = typeof networkInterfaces;

export const IPV4_WAIT_TIMEOUT_MS = 120_000;
export const IPV4_POLL_MS = 1_000;

/**
 * True when the host holds at least one non-loopback IPv4 address.
 *
 * HAP announces the accessory over mDNS on the interfaces that exist when it
 * publishes. Publish with none and the announcement is never redone once an
 * address appears: the process stays perfectly healthy while HomeKit shows
 * "Not Responding" until someone restarts the service (#65).
 */
export function hasUsableIPv4(read: InterfaceReader = networkInterfaces): boolean {
  for (const addresses of Object.values(read())) {
    for (const address of addresses ?? []) {
      if (address.family === "IPv4" && !address.internal) {
        return true;
      }
    }
  }
  return false;
}

export interface WaitForIPv4Options {
  timeoutMs?: number;
  pollMs?: number;
  read?: InterfaceReader;
}

/**
 * Block until the host has a usable IPv4 address, or the timeout expires.
 *
 * Waiting in-process rather than exiting straight away is deliberate: a Pi
 * Zero 2 W needs ~9 s to load Node and HAP-NodeJS, so a tight exit/restart
 * loop would burn most of the boot re-doing that work. Field measurement on
 * the incident this guards against (#65): the service started at 18:11:49 and
 * NetworkManager only associated Wi-Fi at 18:12:29 — 40 s of polling here,
 * against eight full restarts otherwise.
 *
 * Returns false on timeout, leaving the caller to exit non-zero so systemd
 * retries from scratch.
 */
export async function waitForIPv4({
  timeoutMs = IPV4_WAIT_TIMEOUT_MS,
  pollMs = IPV4_POLL_MS,
  read = networkInterfaces,
}: WaitForIPv4Options = {}): Promise<boolean> {
  if (hasUsableIPv4(read)) {
    return true;
  }
  console.warn("[network] no non-loopback IPv4 yet — waiting for the network…");
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, pollMs));
    if (hasUsableIPv4(read)) {
      return true;
    }
  }
  return false;
}
