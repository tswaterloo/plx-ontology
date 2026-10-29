// Incremental parser for a text/event-stream body (the POST /api/ask answer).
// EventSource only does GET, so the stream is read from fetch().body.

export interface RawSseEvent {
  event: string;
  data: string;
  id?: string;
}

/** Feed decoded text chunks; complete events come back as they finish. */
export class SseParser {
  private buf = "";
  private event = "";
  private data: string[] = [];
  private id: string | undefined;

  push(chunk: string): RawSseEvent[] {
    this.buf += chunk;
    const out: RawSseEvent[] = [];
    let nl: number;
    // Lines end in \n, \r\n or \r.
    while ((nl = this.buf.search(/\r\n|\r|\n/)) >= 0) {
      const sepLen = this.buf.startsWith("\r\n", nl) ? 2 : 1;
      // A lone trailing \r may be the first half of \r\n: wait for more input.
      if (this.buf[nl] === "\r" && nl === this.buf.length - 1) break;
      const line = this.buf.slice(0, nl);
      this.buf = this.buf.slice(nl + sepLen);
      const ev = this.line(line);
      if (ev) out.push(ev);
    }
    return out;
  }

  /** Call at end of stream: a final event without its blank line still counts. */
  end(): RawSseEvent[] {
    const out: RawSseEvent[] = [];
    if (this.buf) {
      const ev = this.line(this.buf);
      this.buf = "";
      if (ev) out.push(ev);
    }
    const ev = this.line("");
    if (ev) out.push(ev);
    return out;
  }

  private line(line: string): RawSseEvent | null {
    if (line === "") {
      if (this.data.length === 0) {
        this.event = "";
        return null;
      }
      const ev: RawSseEvent = { event: this.event || "message", data: this.data.join("\n") };
      if (this.id !== undefined) ev.id = this.id;
      this.event = "";
      this.data = [];
      return ev;
    }
    if (line.startsWith(":")) return null; // comment / keep-alive
    const colon = line.indexOf(":");
    const field = colon < 0 ? line : line.slice(0, colon);
    let value = colon < 0 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") this.event = value;
    else if (field === "data") this.data.push(value);
    else if (field === "id") this.id = value;
    return null;
  }
}
