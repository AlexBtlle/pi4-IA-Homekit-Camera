import fs from "fs";
import path from "path";
import { describe, expect, test } from "vitest";

import { esc } from "../src/qrweb";

describe("qrweb esc() — HTML text-content escaping", () => {
  test("escapes the three characters that matter in text content", () => {
    expect(esc("<script>alert(1)</script>")).toBe(
      "&lt;script&gt;alert(1)&lt;/script&gt;",
    );
    expect(esc("a & b < c > d")).toBe("a &amp; b &lt; c &gt; d");
  });

  test("ampersand is escaped first (no double-escaping)", () => {
    expect(esc("&lt;")).toBe("&amp;lt;");
  });

  test("leaves quotes alone — the documented text-content-only contract", () => {
    // Quotes only matter inside attribute values; esc() is contractually for
    // element text. The companion test below keeps that contract honest.
    expect(esc(`"quoted" & 'single'`)).toBe(`"quoted" &amp; 'single'`);
  });

  test("no call site uses esc() inside an HTML attribute", () => {
    // The contract that makes quote-passthrough safe: if someone interpolates
    // an esc'd value into ="${...}", this fails and forces a real decision.
    const src = fs.readFileSync(
      path.resolve(__dirname, "..", "src", "qrweb.ts"),
      "utf8",
    );
    expect(src).not.toMatch(/="[^"\n]*\$\{esc\(/);
    expect(src).not.toMatch(/='[^'\n]*\$\{esc\(/);
  });
});
