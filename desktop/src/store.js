"use strict";
// The durable tier of the desktop app's page store (SPEED-SPECS 10.9): the answers the
// server already sent THIS person, kept on this machine so the first screen after a launch
// draws at once. It holds nothing the server did not send this person, decides nothing, and
// never parses a body (bodies are opaque strings).
//
// Layout under `dir` (mode 0700; every file 0600):
//   key.bin       the random 32-byte data key, sealed by the OS (safeStorage, async API)
//   index.bin     AES-256-GCM: { door, entries: { <name>: { k, at, used, bytes, release, door } } }
//   e/<name>.bin  AES-256-GCM: one entry's body. <name> = HMAC-SHA256(dataKey, key), so no
//                 record id is ever in a file name. The slot name is the cipher's AAD, so a
//                 file moved to another slot does not open.
// One file per entry, not one per person: a rewrite per answer and a 113 ms decrypt of 64 MB
// (measured, 10.9) are both off the table.
//
// Rules, each with a test (test/store.test.js):
//  - one store, one door value (the server's X-PLX-Door of the session's own reader): a
//    different value wipes everything BEFORE the answer that carried it is delivered
//    (`observe`, called by the one webRequest listener in main.js);
//  - a refused session (X-PLX-Session: refused), sign-out and "Change server" wipe
//    (`wipe`): the data key goes first (crypto-shred), then the files;
//  - an entry is put only under the store's current door, and is returned only under it;
//  - a 404 drops that entry; entries older than TTL_MS are dropped at open; the total is
//    bounded by bytes (least recently used go first);
//  - no sealing available: the durable tier is off and says so (`durable: false`).

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const VERSION = 1;
const TTL_MS = 7 * 24 * 3600 * 1000;
const MAX_ENTRY_BYTES = 12 * 1024 * 1024;
const MAX_TOTAL_BYTES = 256 * 1024 * 1024;
const MAX_KEY_CHARS = 2048;
const FIRST_SCREEN_ENTRIES = 8;
const FIRST_SCREEN_BYTES = 4 * 1024 * 1024;
const INDEX_DELAY_MS = 1500;

/** A key is a path inside the Workbench's API, as the page asks for it: "/api/...?query". */
const NEVER_KEPT = ["/api/auth/", "/api/ask", "/api/events", "/api/view-as/"];
function validKey(key) {
  return typeof key === "string" && key.length <= MAX_KEY_CHARS && /^\/api\/(?!.*\.\.\/)[\x21-\x7e]*$/.test(key) && !key.includes("\\") && !NEVER_KEPT.some((p) => key.startsWith(p));
}

function encrypt(dataKey, aad, plain) {
  const iv = crypto.randomBytes(12);
  const c = crypto.createCipheriv("aes-256-gcm", dataKey, iv);
  c.setAAD(Buffer.from(aad));
  const ct = Buffer.concat([c.update(plain), c.final()]);
  return Buffer.concat([Buffer.from([VERSION]), iv, c.getAuthTag(), ct]);
}

function decrypt(dataKey, aad, blob) {
  if (!Buffer.isBuffer(blob) || blob.length < 29 || blob[0] !== VERSION) throw new Error("bad_blob");
  const d = crypto.createDecipheriv("aes-256-gcm", dataKey, blob.subarray(1, 13));
  d.setAAD(Buffer.from(aad));
  d.setAuthTag(blob.subarray(13, 29));
  return Buffer.concat([d.update(blob.subarray(29)), d.final()]);
}

/**
 * @param {{maxTotal?: number, dir: string, sealer: {available(): Promise<boolean>, seal(b: Buffer): Promise<Buffer>, unseal(b: Buffer): Promise<{key: Buffer, reseal?: boolean}>}|null, now?: () => number, log?: (m: string) => void}} o
 */
function createStore({ dir, sealer, now = Date.now, log = () => undefined, maxTotal = MAX_TOTAL_BYTES }) {
  const files = { key: path.join(dir, "key.bin"), index: path.join(dir, "index.bin"), entries: path.join(dir, "e") };
  let generation = 0; // moves with every wipe: a write that started before one never lands after it
  let dataKey = null;
  let door = null;
  let lastSeen = null; // the door value of the newest API answer seen (not kept): a put must be of it
  let index = new Map(); // name -> { k, at, used, bytes, release, door }
  let total = 0;
  let durable = false;
  let opened = false;
  let writeTimer = null;
  let keyMaking = null;

  const nameOf = (key) => crypto.createHmac("sha256", dataKey).update(key).digest("hex");
  const slot = (name) => path.join(files.entries, `${name}.bin`);

  function mkdirs() {
    fs.mkdirSync(files.entries, { recursive: true, mode: 0o700 });
    try {
      fs.chmodSync(dir, 0o700);
    } catch {
      /* a platform without modes */
    }
  }

  function writeFileAtomic(file, buf) {
    const tmp = `${file}.${process.pid}.tmp`;
    fs.writeFileSync(tmp, buf, { mode: 0o600 });
    fs.renameSync(tmp, file);
  }

  function flushIndex() {
    if (writeTimer) clearTimeout(writeTimer);
    writeTimer = null;
    if (!durable || !dataKey) return;
    try {
      const body = { door, entries: Object.fromEntries(index) };
      mkdirs();
      writeFileAtomic(files.index, encrypt(dataKey, "index", Buffer.from(JSON.stringify(body))));
    } catch (e) {
      log(`store: index not written: ${e && e.code ? e.code : "error"}`);
    }
  }

  function scheduleIndex() {
    if (writeTimer || !durable) return;
    writeTimer = setTimeout(flushIndex, INDEX_DELAY_MS);
    if (writeTimer.unref) writeTimer.unref();
  }

  /** Forget everything, at once: the data key first (nothing left can be opened), then the files. */
  function wipe(reason) {
    generation += 1;
    if (writeTimer) clearTimeout(writeTimer);
    writeTimer = null;
    const had = index.size > 0 || dataKey !== null || door !== null;
    dataKey = null;
    door = null;
    lastSeen = null;
    index = new Map();
    total = 0;
    keyMaking = null;
    for (const f of [files.key, files.index]) {
      try {
        fs.rmSync(f, { force: true });
      } catch (e) {
        log(`store: ${path.basename(f)} not removed: ${e && e.code ? e.code : "error"}`);
      }
    }
    try {
      fs.rmSync(files.entries, { recursive: true, force: true });
    } catch (e) {
      log(`store: entries not removed: ${e && e.code ? e.code : "error"}`);
    }
    if (had) log(`store: wiped (${reason})`);
    return had;
  }

  async function ensureKey() {
    if (dataKey) return true;
    if (!durable) return false;
    if (keyMaking) return keyMaking;
    const gen = generation;
    keyMaking = (async () => {
      const fresh = crypto.randomBytes(32);
      const sealed = await sealer.seal(fresh);
      if (gen !== generation) return false;
      mkdirs();
      writeFileAtomic(files.key, sealed);
      dataKey = fresh;
      return true;
    })().catch((e) => {
      log(`store: key not made: ${e && e.message ? String(e.message).slice(0, 80) : "error"}`);
      return false;
    });
    try {
      return await keyMaking;
    } finally {
      keyMaking = null;
    }
  }

  function dropName(name) {
    const hit = index.get(name);
    if (!hit) return false;
    index.delete(name);
    total -= hit.bytes;
    try {
      fs.rmSync(slot(name), { force: true });
    } catch {
      /* the index no longer names it; open() removes strays */
    }
    return true;
  }

  function evict() {
    if (total <= maxTotal) return;
    const oldest = [...index.entries()].sort((a, b) => a[1].used - b[1].used);
    for (const [name] of oldest) {
      if (total <= maxTotal * 0.9) break;
      dropName(name);
    }
  }

  function read(name, meta) {
    const blob = fs.readFileSync(slot(name));
    const body = decrypt(dataKey, name, blob).toString("utf8");
    return { key: meta.k, body, fetchedAt: meta.at, release: meta.release, door: meta.door };
  }

  /** The keys by recent use, and the newest few entries (the first screen's). */
  function firstScreen() {
    if (!dataKey) return { durable: true, door, keys: [], entries: [] };
    const recent = [...index.entries()].sort((a, b) => b[1].used - a[1].used);
    const entries = [];
    let bytes = 0;
    for (const [name, m] of recent) {
      if (entries.length >= FIRST_SCREEN_ENTRIES || bytes + m.bytes > FIRST_SCREEN_BYTES) break;
      try {
        entries.push(read(name, m));
        bytes += m.bytes;
      } catch {
        dropName(name);
      }
    }
    const keys = recent.map(([, m]) => m.k);
    const t = now();
    const usedKeys = recent.filter(([, m]) => t - m.used <= 3600_000).map(([, m]) => m.k);
    return { durable: true, door, keys, usedKeys, entries };
  }

  return {
    /** Whether the durable tier is on. */
    get durable() {
      return durable;
    },
    /** The door value the store is under (null: none yet). */
    get door() {
      return door;
    },
    get size() {
      return index.size;
    },

    /** Lightweight read-only keys used in the last hour, most-recent-first, without decrypting entries. */
    usedKeys() {
      if (!durable || !dataKey) return [];
      const t = now();
      return [...index.entries()]
        .filter(([, m]) => t - m.used <= 3600_000)
        .sort((a, b) => b[1].used - a[1].used)
        .map(([, m]) => m.k);
    },

    /** Start: unseal the data key, read the index, drop what is older than TTL_MS or lost.
     * Returns what the first screen needs: the keys by recent use, and the newest few entries. */
    async open() {
      const off = { durable: false, door: null, keys: [], entries: [] };
      if (opened) return durable ? firstScreen() : off; // the page loaded again: what is held now, not the disk
      opened = true;
      let ok = false;
      try {
        ok = !!sealer && (await sealer.available());
      } catch {
        ok = false;
      }
      if (!ok) {
        durable = false;
        return off;
      }
      durable = true;
      const gen = generation;
      try {
        if (!fs.existsSync(files.key) || !fs.existsSync(files.index)) {
          if (fs.existsSync(files.key) || fs.existsSync(files.index) || fs.existsSync(files.entries)) wipe("incomplete store");
          return { durable: true, door: null, keys: [], entries: [] };
        }
        const un = await sealer.unseal(fs.readFileSync(files.key));
        if (gen !== generation) return off;
        dataKey = un.key;
        if (un.reseal) writeFileAtomic(files.key, await sealer.seal(dataKey));
        const parsed = JSON.parse(decrypt(dataKey, "index", fs.readFileSync(files.index)).toString("utf8"));
        door = typeof parsed.door === "string" ? parsed.door : null;
        const t = now();
        index = new Map();
        total = 0;
        for (const [name, m] of Object.entries(parsed.entries || {})) {
          if (!m || typeof m.k !== "string" || !validKey(m.k) || name !== nameOf(m.k) || m.door !== door || !door || t - m.at > TTL_MS || !fs.existsSync(slot(name))) {
            try {
              fs.rmSync(slot(name), { force: true });
            } catch {
              /* stray */
            }
            continue;
          }
          index.set(name, { k: m.k, at: m.at, used: m.used || m.at, bytes: m.bytes | 0, release: m.release || "", door: m.door });
          total += m.bytes | 0;
        }
        // files the index does not name (a crash between write and index): removed
        for (const f of fs.existsSync(files.entries) ? fs.readdirSync(files.entries) : []) {
          const name = f.replace(/\.bin$/, "");
          if (!index.has(name)) fs.rmSync(path.join(files.entries, f), { force: true });
        }
        scheduleIndex();
      } catch (e) {
        // an unreadable store is a wiped store: never a half-open one
        log(`store: open failed (${e && e.code ? e.code : "unreadable"}); wiped`);
        wipe("unreadable at open");
        return { durable: true, door: null, keys: [], entries: [] };
      }
      return firstScreen();
    },

    /** One entry by key, or null (also when it is under another door, or unreadable). */
    async get(key) {
      if (!durable || !dataKey || !validKey(key)) return null;
      const name = nameOf(key);
      const m = index.get(name);
      if (!m || m.door !== door || !door) return null;
      const gen = generation;
      try {
        const got = read(name, m);
        if (gen !== generation) return null;
        m.used = now();
        scheduleIndex();
        return got;
      } catch {
        dropName(name);
        scheduleIndex();
        return null;
      }
    },

    /** Keep an answer: only under the store's current door, as it came (an opaque string). */
    async put({ key, body, door: under, release, fetchedAt }) {
      if (!durable || !validKey(key) || typeof body !== "string" || typeof under !== "string" || !under) return false;
      if (Buffer.byteLength(body) > MAX_ENTRY_BYTES) return false;
      // only an answer of the newest door the listener saw, and never under another than the store's:
      // a late put of a person who has gone (their sign-out wiped the store) lands nowhere
      if (under !== lastSeen || (door !== null && under !== door)) return false;
      const gen = generation;
      if (!(await ensureKey())) return false;
      if (gen !== generation) return false;
      if (door === null) door = under;
      const name = nameOf(key);
      const blob = encrypt(dataKey, name, Buffer.from(body, "utf8"));
      mkdirs();
      writeFileAtomic(slot(name), blob);
      const had = index.get(name);
      if (had) total -= had.bytes;
      const t = now();
      index.set(name, { k: key, at: Number.isFinite(fetchedAt) ? fetchedAt : t, used: t, bytes: blob.length, release: String(release || ""), door });
      total += blob.length;
      evict();
      scheduleIndex();
      return true;
    },

    /** Forget one answer (a 404, or the page's own drop). */
    drop(key) {
      if (!durable || !dataKey || !validKey(key)) return false;
      const gone = dropName(nameOf(key));
      if (gone) scheduleIndex();
      return gone;
    },

    wipe,

    /** What one API answer says about the store, called by the one webRequest listener BEFORE
     * the answer's body is released. `refused`: X-PLX-Session: refused. `door`: X-PLX-Door.
     * Returns "wiped" when it emptied the store, else "kept". */
    observe({ refused, door: seen }) {
      if (refused) {
        wipe("session refused");
        return "wiped"; // also when the store was empty: the page forgets its own copy too
      }
      if (typeof seen !== "string" || !seen) return "kept"; // no door value: it says nothing, and is never stored
      let out = "kept";
      if (door !== null && door !== seen) {
        wipe("door changed");
        out = "wiped";
      }
      lastSeen = seen; // an empty store takes the door of its first put
      return out;
    },

    /** Write the index now (before quitting). */
    flush: flushIndex,
    get opened() {
      return opened;
    },
  };
}

module.exports = { createStore, validKey, TTL_MS, MAX_ENTRY_BYTES, MAX_TOTAL_BYTES };
