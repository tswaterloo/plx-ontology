import { describe, expect, it } from "vitest";
import { SseParser } from "./sse";

describe("SseParser", () => {
  it("assembles events split at any byte boundary", () => {
    const wire = 'event: token\ndata: {"text":"Hel"}\n\nevent: token\ndata: {"text":"lo"}\n\n';
    for (let cut = 1; cut < wire.length; cut++) {
      const p = new SseParser();
      const evs = [...p.push(wire.slice(0, cut)), ...p.push(wire.slice(cut)), ...p.end()];
      expect(evs.map((e) => e.data)).toEqual(['{"text":"Hel"}', '{"text":"lo"}']);
      expect(evs.every((e) => e.event === "token")).toBe(true);
    }
  });

  it("handles CRLF split between chunks, comments and multi-line data", () => {
    const p = new SseParser();
    const evs = [
      ...p.push(": keep-alive\r"),
      ...p.push("\nevent: status\r\ndata: a\r\ndata: b\r"),
      ...p.push("\n\r\n"),
    ];
    expect(evs).toEqual([{ event: "status", data: "a\nb" }]);
  });

  it("flushes a final event that lacks its blank line", () => {
    const p = new SseParser();
    expect(p.push('event: answer\ndata: {"x":1}')).toEqual([]);
    expect(p.end()).toEqual([{ event: "answer", data: '{"x":1}' }]);
  });

  it("defaults the event name and ignores empty dispatches", () => {
    const p = new SseParser();
    expect(p.push("\n\ndata: hi\n\n")).toEqual([{ event: "message", data: "hi" }]);
  });
});
