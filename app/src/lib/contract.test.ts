// @vitest-environment node
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { generate } from "../../scripts/gen-api.mjs";

describe("operator_api contract", () => {
  it("src/lib/openapi.d.ts is generated from the committed operator_api/openapi.json", async () => {
    const committed = readFileSync(fileURLToPath(new URL("./openapi.d.ts", import.meta.url)), "utf8");
    expect(committed === (await generate())).toBe(true); // stale: run `npm run gen:api`
  });

  it("the ask stream's events are the ones the app handles", () => {
    const spec = JSON.parse(readFileSync(fileURLToPath(new URL("../../../operator_api/openapi.json", import.meta.url)), "utf8"));
    const events = spec.paths["/api/ask"].post.responses["200"].content["text/event-stream"].schema["x-sse-events"];
    expect(Object.keys(events).sort()).toEqual(["answer", "error", "sources", "status", "token", "understanding"]);
  });
});
