import { describe, it, expect } from "vitest";
import { Prebuffer, Fragment } from "../src/prebuffer";

/**
 * segments() consumer tests (#36) — no ffmpeg. The prebuffer is fed by
 * calling the (compile-time) private addFragment directly, exactly what the
 * mdat handler does.
 */

function makePrebuffer(): Prebuffer {
  const p = new Prebuffer("rtsp://unused");
  // Init segment already known (as after a first ffmpeg connection):
  // getInit() resolves immediately, no waiter registered on the signal.
  (p as unknown as { initSegment: Buffer }).initSegment = Buffer.from("init");
  return p;
}

function feed(p: Prebuffer, id: number): void {
  (p as unknown as { addFragment(f: Fragment): void }).addFragment({
    id,
    data: Buffer.of(id),
    time: performance.now(),
  });
}

describe("Prebuffer.segments", () => {
  it("yields init then live fragments", async () => {
    const p = makePrebuffer();
    const gen = p.segments(0, new AbortController().signal);
    expect((await gen.next()).value!.toString()).toBe("init");

    const pending = gen.next(); // suspends on the empty queue
    feed(p, 1);
    expect([...(await pending).value!]).toEqual([1]);
  });

  it("abandons the stream when the consumer stops draining (bounded queue)", async () => {
    const p = makePrebuffer();
    const gen = p.segments(0, new AbortController().signal);
    await gen.next(); // init

    const pending = gen.next(); // consumer parked on the empty queue
    // A stalled hub: fragments keep arriving, nothing is drained. Push past
    // the cap before the consumer gets a chance to resume.
    for (let i = 1; i <= 62; i++) {
      feed(p, i);
    }
    await expect(pending).rejects.toThrow(/consumer too slow/);
  });

  it("registers ONE abort listener for the generator's whole life", async () => {
    const p = makePrebuffer();
    const ac = new AbortController();
    let abortListeners = 0;
    const origAdd = ac.signal.addEventListener.bind(ac.signal);
    ac.signal.addEventListener = ((type: string, ...rest: unknown[]) => {
      if (type === "abort") {
        abortListeners++;
      }
      return (origAdd as (...a: unknown[]) => void)(type, ...rest);
    }) as typeof ac.signal.addEventListener;

    const gen = p.segments(0, ac.signal);
    await gen.next(); // init

    // Five empty-queue wait cycles — the old code added one {once:true}
    // listener per cycle (MaxListenersExceededWarning past ten).
    for (let i = 1; i <= 5; i++) {
      const pending = gen.next();
      feed(p, i);
      await pending;
    }
    expect(abortListeners).toBe(1);

    ac.abort();
    expect((await gen.next()).done).toBe(true);
  });

  it("abort while parked on an empty queue ends the generator", async () => {
    const p = makePrebuffer();
    const ac = new AbortController();
    const gen = p.segments(0, ac.signal);
    await gen.next(); // init

    const pending = gen.next(); // parked
    ac.abort();
    expect((await pending).done).toBe(true);
  });
});
