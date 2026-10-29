import { describe, expect, it } from "vitest";
import { ApiError, apiErrorFrom, GENERIC_ERROR, incidentOf, plainError } from "./errors";

describe("plainError", () => {
  it("maps known codes and statuses to plain sentences", () => {
    expect(plainError(new ApiError(401, "sign_in_required"))).toMatch(/Sign in again/);
    expect(plainError(new ApiError(403, "no_workbench_access"))).toMatch(/doesn't have Workbench access/);
    expect(plainError(new ApiError(500, "internal", "9f3a01bc"))).toMatch(/incident 9f3a01bc/);
    expect(plainError(new ApiError(403, "http_403"))).toMatch(/don't have access/);
    expect(plainError(new ApiError(504, "weird"))).toMatch(/too long/);
    expect(plainError(new TypeError("Failed to fetch"))).toMatch(/Can't reach/);
  });

  it("never echoes server text, codes or stacks", () => {
    const nasty = 'KeyError: \'po\'\n  File "/srv/operator_api/app.py", line 12';
    for (const e of [new ApiError(500, nasty), new Error(nasty), { code: nasty }, nasty, null, undefined]) {
      const msg = plainError(e);
      expect(msg).toBe(GENERIC_ERROR);
      expect(msg).not.toContain("KeyError");
    }
  });
});

describe("apiErrorFrom", () => {
  it("reads operator_api's {error:{code,message}} and keeps only a hex incident id", async () => {
    const e = await apiErrorFrom(
      new Response(JSON.stringify({ error: { code: "internal", message: "Something went wrong; quote incident 0a1b2c3d." } }), { status: 500 }),
    );
    expect(e).toMatchObject({ status: 500, code: "internal", incident: "0a1b2c3d" });
    expect(incidentOf("quote incident <script>")).toBeUndefined();
  });

  it("survives a non-JSON body", async () => {
    expect(await apiErrorFrom(new Response("<html>502</html>", { status: 502 }))).toMatchObject({ code: "http_502" });
  });
});
