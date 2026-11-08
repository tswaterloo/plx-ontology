import { describe, expect, it } from "vitest";
import { ApiError, GENERIC_ERROR, apiErrorFrom, plainError } from "./errors";

const RAW = /503|500|\{|\}|traceback|stack|undefined|http_/i;

describe("plainError", () => {
  it("never shows raw status, JSON or stack text for a failed response", async () => {
    const bodies: Array<[number, string]> = [
      [503, '{"error":{"code":"unavailable","message":"Traceback (most recent call last): boom"}}'],
      [500, "<html>Internal Server Error at /app/server.js:12</html>"],
      [504, ""],
      [500, '{"error":{"code":"internal","message":"internal error, incident 0123abcd"}}'],
    ];
    for (const [status, body] of bodies) {
      const err = await apiErrorFrom(new Response(body, { status }));
      const said = plainError(err);
      expect(said.replace(/incident [0-9a-f]+/, "")).not.toMatch(RAW);
      expect(said.length).toBeGreaterThan(20);
    }
  });

  it("maps a fetch failure and an unknown thrown value to a plain sentence", () => {
    expect(plainError(new TypeError("Failed to fetch"))).toMatch(/connection/i);
    expect(plainError(new Error("ECONNRESET at foo.js:1"))).toBe(GENERIC_ERROR);
    expect(plainError(new ApiError(503, "http_503"))).toMatch(/try again/i);
  });
});
