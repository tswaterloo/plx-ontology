"""Build, run and grade the generative evaluation (``evals.generative``).

    python3 -m scripts.eval_generative build --root DIR [--state DIR] [--census FILE]
        [--items 3300] [--dev-items 300] [--seed 7]
    python3 -m scripts.eval_generative run --root DIR --set dev|held --name NAME
        --api-args "--dev --dev-ticket-login --state-dir DIR --ask ... (operator_api serve flags)"
        [--concurrency 3] [--limit N] [--ids A,B]
        [--mint-cmd "CMD"] [--access-link FILE] (--reader-email ADDRESS | --reader-env VAR)
        [--reader-markings finance,purchasing_prices,quality_controlled,pii]
        [--min-available-gb 12]
    python3 -m scripts.eval_generative grade --root DIR --set dev|held --name NAME
        [--reader-markings ...]
    python3 -m scripts.eval_generative recipes --root DIR --census FILE [--state DIR]
        [--items 3300 --dev-items 300] [--workers 16] [--ids dev-G-0001,... --built-items F]
    python3 -m scripts.eval_generative truth --root DIR --out DIR2 --set dev|held|all
        [--as-of 2026-11-12T14:00:00Z] [--dsn DSN] [--workers 16] [--ids ...]

**build** reads the published projection generation read-only (sqlite ``mode=ro``,
``query_only``: the same store, census and people guard as ``scripts.eval_graph_walks``)
and writes ``ROOT/held/items.jsonl`` and ``ROOT/dev/items.jsonl`` (directories 0700, files
0600; ``ROOT`` outside any git work tree, refused otherwise). With no usable people
exclusion list the build refuses (fail closed). Stdout: counts only (categories, styles,
difficulty, coverage, drop reasons), never a question.

**run** asks the app's own Ask in process: it builds the app from ``--api-args`` (the
``operator_api serve --dev`` flags; the same start-up the served app runs, bound to no port)
and calls ``backend.ask`` as the ``/api/ask`` route does, ``--concurrency`` questions at a
time, each started only while ``MemAvailable`` is at least ``--min-available-gb``. No HTTP:
this runner opens no connection of its own (the egress gate's rule), so it measures the
code of the tree it runs from, not a deployed server, and holds a whole app in memory (give
it its own ``--app-data-dir``). The reader signs in through the app's authenticator with
the dev-login ticket that ``--mint-cmd`` writes to ``--access-link`` (re-minted when the
session ends), or the identity ``--dev-employee-oid`` configures. The reader's address must
be one the people exclusion decides and does not exclude, and the identity the app signed
in must have that address (fail closed). Each final answer is kept 0600 under
``ROOT/runs/NAME/answers``; the log holds ids, seconds and outcomes only. A run resumes by
name: answered ids are skipped. An ask writes nothing to a business system.

**recipes** and **truth** give a built set the served store's truth, keeping its
questions, ids, seed and dev/held split. ``recipes`` composes the set again over the
SQLite generation it was built from (the build replayed, each item checked against the
kept one) and writes down how each item was composed: its category's turn, the records
it picked and its draws (``evals.generative.Pinned``, ``PinnedRandom``; digests, no
question text). ``truth`` composes each item again by its recipe over Postgres as it
stood at ``--as-of`` (``scripts.eval_pg_store``: read-only), so the same question about
the same records gets the records' truth then; a question that names the set's own day
("As of <day>, ...") gets that day's. Items whose question no longer arises are left
out, with why (a newer version, link or record since the base store; counts only).

**grade** grades a run (``evals.generative.grade``) into ``verdicts.jsonl`` (ids, outcomes,
codes) and prints ``evals.generative.summary``: aggregates only, which is what a fix round
may see of the held-out set.
"""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import random
import shlex
import sqlite3
import subprocess
import sys
import threading
import time
from typing import Any, Iterable, Mapping, Sequence

from evals import generative as gen
from evals import graph_walks as gw
from scripts import eval_graph_walks as egw

OWNER_MARKINGS = "finance,purchasing_prices,quality_controlled,pii"
SPLITS = {"dev": "dev", "held": "held"}


class Refused(ValueError):
    """The command cannot run safely (a code, never a value)."""


# --------------------------------------------------------------------------- the store

class GenStore(egw.Store):
    """``evals.generative.Source`` over the projection store (read-only)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._counts: dict[tuple[str, str], int] = {}
        self._by_prop: dict[tuple[str, str], dict[str, list[str]] | None] = {}

    @staticmethod
    def _range(object_type: str, binding: str) -> tuple[str, str]:
        head = f"{object_type}:{binding}:" if binding else f"{object_type}:"
        return head, head[:-1] + ";"

    def count(self, object_type: str, binding: str = "") -> int:
        key = (object_type, binding)
        if key not in self._counts:
            lo, hi = self._range(object_type, binding)
            self._counts[key] = self.conn.execute(
                "SELECT count(*) FROM objects WHERE object_id >= ? AND object_id < ?",
                (lo, hi)).fetchone()[0]
        return self._counts[key]

    def objects(self, object_type: str, k: int, rng: random.Random, binding: str = ""
                ) -> list[str]:
        n = self.count(object_type, binding)
        if not n:
            return []
        lo, hi = self._range(object_type, binding)
        out: list[str] = []
        for offset in gen.draw_offsets(n, k, rng):
            row = self.conn.execute(
                "SELECT object_id FROM objects WHERE object_id >= ? AND object_id < ? "
                "ORDER BY object_id LIMIT 1 OFFSET ?", (lo, hi, offset)).fetchone()
            if row and row[0] not in out:
                out.append(row[0])
        return out

    def all_of(self, object_type: str, binding: str) -> list[str]:
        lo, hi = self._range(object_type, binding)
        return [r[0] for r in self.conn.execute(
            "SELECT object_id FROM objects WHERE object_id >= ? AND object_id < ? "
            "ORDER BY object_id", (lo, hi))]

    def lookup_prop(self, object_type: str, prop: str, value: str) -> list[str] | None:
        """Every record of the type whose ``prop`` is ``value`` (case folded); None for a
        type too large to index."""
        key = (object_type, prop)
        if key not in self._by_prop:
            if self.count(object_type) > self.MAX_NAMED:
                self._by_prop[key] = None
            else:
                index: dict[str, list[str]] = {}
                lo, hi = self._range(object_type, "")
                for oid, got in self.conn.execute(
                        "SELECT object_id, value FROM object_values WHERE object_id >= ? AND "
                        "object_id < ? AND property = ? AND superseded_at IS NULL",
                        (lo, hi, prop)):
                    text = " ".join(str(got or "").split()).casefold()
                    if text:
                        index.setdefault(text, []).append(oid)
                self._by_prop[key] = index
                while len(self._by_prop) > self.MAX_INDEXES:
                    self._by_prop.pop(next(iter(self._by_prop)))
        index = self._by_prop[key]
        if index is None:
            return None
        return sorted(set(index.get(" ".join(value.split()).casefold(), [])))

    def address_ok(self, address: str) -> bool:
        return self._address_ok(address)

    def sensitive(self, texts: Sequence[str], title: str | None) -> bool:
        from operating.chunking.sensitive import folders, personal_financial
        labels = [f for t in texts if "/" in t for f in folders(t)]
        return personal_financial("\n".join(texts), title=title, labels=labels) is not None


def type_counts(store: GenStore) -> dict[str, dict[str, int]]:
    """object type -> binding -> records, for every type of the served schema."""
    from ontology.schema import load_schema
    out: dict[str, dict[str, int]] = {}
    for object_type in sorted(load_schema().object_types):
        lo, hi = store._range(object_type, "")
        per: Counter = Counter()
        for (oid,) in store.conn.execute(
                "SELECT object_id FROM objects WHERE object_id >= ? AND object_id < ? "
                "LIMIT 2000", (lo, hi)):
            per[gw.binding_of(oid)] += 1
        for binding in sorted(per):
            n = store.count(object_type, binding)
            if n:
                out.setdefault(object_type, {})[binding] = n
    return out


# --------------------------------------------------------------------------- files

def _private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


def _write_lines(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    _private_dir(path.parent)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(gen.dumps(row) + "\n")
    tmp.replace(path)


def _write_json(path: Path, value: Any) -> None:
    _private_dir(path.parent)
    fd = os.open(path.with_suffix(".tmp"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True, indent=1)
    path.with_suffix(".tmp").replace(path)


def load_items(root: Path, split: str) -> list[dict[str, Any]]:
    path = root / SPLITS[split] / "items.jsonl"
    if not path.is_file():
        raise Refused("items_missing")
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line]


def _outside_git(path: Path) -> None:
    from scripts import eval_live_ops as lo
    if lo._inside_git(path):
        raise Refused("root_inside_a_git_work_tree")


# --------------------------------------------------------------------------- build

def build(args: argparse.Namespace, environ: Mapping[str, str]) -> dict[str, Any]:
    from scripts import eval_live_ops as lo
    _outside_git(args.root)
    named = args.state or environ.get(lo.real_state_env())
    if not named or not Path(named).is_dir():
        raise Refused("state_missing")
    store_path, generation, day = egw.current_store(Path(named))
    guard, own, boxes = egw.people_guard(Path(named), environ)   # refuses fail-closed
    census = json.loads(Path(args.census).read_text("utf-8"))
    if census.get("format") != egw.CENSUS_FORMAT or census.get("generation") != generation:
        raise Refused("census_of_another_generation")
    links = egw.schema_links(census["counts"])
    store = GenStore(store_path, census["samples"], guard, own, boxes)
    try:
        types = type_counts(store)
        generator = gen.Generator(links, store, date.fromisoformat(day), types)
        items, stats = gen.build(generator, items=args.items, seed=args.seed,
                                 dev_share=args.dev_items / max(args.items, 1),
                                 generation=generation)
    finally:
        store.close()
    _private_dir(args.root)
    for split, name in (("dev", "dev"), ("held_out", "held")):
        _write_lines(args.root / name / "items.jsonl", [i for i in items if i["split"] == split])
    stats.update(generation=generation, as_of=day, seed=args.seed,
                 built_at=datetime.now(timezone.utc).isoformat()[:19] + "Z")
    _write_json(args.root / "manifest.json", stats)
    return stats


# --------------------------------------------------------------------------- run

def _available_gb() -> float:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) / 1024 / 1024
    return 0.0


def reader_cleared(email: str, environ: Mapping[str, str], state: Path | None) -> None:
    """The reader is a person the exclusion decides and does not exclude (fail closed)."""
    from integrations.people_exclusion import PeopleExclusion, with_directory
    if not email or "@" not in email:
        raise Refused("reader_email_missing")
    guard = PeopleExclusion.from_env(environ)
    if state is not None:
        guard = with_directory(guard, state)
    if guard.unavailable:
        raise Refused("people_exclusion_unavailable")
    try:
        if not guard.decides(email) or guard.is_excluded(email):
            raise Refused("reader_excluded_or_undecided")
    except Refused:
        raise
    except Exception:   # the guard cannot decide: refuse, never assume
        raise Refused("reader_excluded_or_undecided") from None


class Session:
    """The reader's signed-in session with the app's own Ask, in this process: signed in
    through the app's authenticator (``auth.login``, the dev-login route's own call) and
    re-authenticated per question (``auth.authenticate``, the route's ``reader``
    dependency), so an expired or revoked ticket ends the session as it does over HTTP."""

    def __init__(self, auth: Any, mint: Sequence[str], link: Path) -> None:
        self.auth, self.mint, self.link = auth, mint, link
        self.lock = threading.Lock()
        self.epoch = 0
        self.token = ""

    def _ticket(self) -> str | None:
        if not getattr(self.auth, "needs_ticket", False):
            return None   # a configured dev identity: no ticket to present
        if not self.mint and not self.link.is_file():
            raise Refused("session_expired_and_no_login")
        if self.mint:
            subprocess.run(list(self.mint), check=True, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=120)
        link = self.link.read_text("utf-8").strip()
        if "#ticket=" not in link:
            raise Refused("access_link_without_ticket")
        return link.split("#ticket=", 1)[1]

    def login(self, seen_epoch: int) -> None:
        from operator_api.auth import AuthRequired
        with self.lock:
            if self.epoch != seen_epoch:
                return   # another worker signed in already
            try:
                self.token, _ = self.auth.login(self._ticket())
            except AuthRequired as refused:
                raise Refused("login_failed") from refused
            self.epoch += 1

    def reader(self) -> Any:
        from operator_api.auth import SESSION_COOKIE, AuthRequired
        for _ in range(2):
            epoch = self.epoch
            try:
                who = self.auth.authenticate({}, {SESSION_COOKIE: self.token})
            except AuthRequired:
                self.login(epoch)
                continue
            if getattr(who.session, "warm_ahead", False):   # read-ahead serves nobody
                raise Refused("reader_is_read_ahead")
            return who
        raise Refused("login_failed")


def ask(backend: Any, session: Session, question: str) -> tuple[dict[str, Any] | None, str]:
    """(the final answer, a status code): ``backend.ask`` as the ``/api/ask`` route runs it,
    its errors mapped as the route maps them to its ``error`` event."""
    from operator_api import models as m
    from operator_api.errors import UserFacing
    for attempt in range(4):
        final = None
        try:
            request = m.AskRequest(question=question)
            for event, payload in backend.ask(session.reader(), request):
                if event == "answer" and event in m.ASK_EVENTS:
                    final = payload.model_dump(mode="json") \
                        if hasattr(payload, "model_dump") else payload
            return final, "ok" if final else "no_final_event"
        except Refused:
            raise
        except UserFacing as error:
            if error.code == "busy":
                time.sleep(30 * (attempt + 1))
                continue
            return None, f"error_{error.code}"
        except TimeoutError:
            time.sleep(30 * (attempt + 1))
            continue
        except (KeyError, LookupError):
            return None, "error_not_found"
        except ValueError:
            return None, "error_invalid_request"
        except Exception:  # noqa: BLE001 - one item's failure, never the run's
            return None, "error_internal"
    return None, "retries_exhausted"


def own_app_data_dir(api_argv: Sequence[str], chosen: Any) -> None:
    """The eval's app keeps its own app data (sessions, audit, caches): ``--app-data-dir``
    must be given and must not be the served app's default (``serving.DEFAULT_DATA_DIR``),
    which the live server writes (fail closed)."""
    from operator_api.serving import DEFAULT_DATA_DIR
    given = any(a == "--app-data-dir" or a.startswith("--app-data-dir=") for a in api_argv)
    if not given or chosen is None:
        raise Refused("api_args_need_own_app_data_dir")
    if Path(chosen).expanduser().resolve() == Path(DEFAULT_DATA_DIR).expanduser().resolve():
        raise Refused("api_args_app_data_dir_is_the_live_servers")


def in_process(api_argv: Sequence[str], work: Any, *, fixtures: Any = None,
               planner: Any = None) -> Any:
    """``work(backend, auth)``'s result, run against the app's own stack built in this
    process from ``operator_api serve`` flags (``--dev`` only: the ticket sign-in or a
    configured identity), the same start-up the served app runs, but bound to no port:
    the questions reach ``backend.ask`` directly, never over a network. This thread serves
    the backend's reads (``backend.loop``) while ``work`` runs on another, as the server
    does with uvicorn. ``fixtures``/``planner``: the console's fixture world (tests)."""
    from operator_api import backend as be
    from operator_api.server import build_parser
    parser = build_parser()
    args = parser.parse_args(list(api_argv))
    if not args.dev:
        raise Refused("api_args_need_dev")   # mock answers nothing real; Entra needs a browser
    own_app_data_dir(api_argv, args.app_data_dir)
    out: dict[str, Any] = {}

    def start(backend: Any, auth: Any, _config: Mapping[str, Any]) -> int:
        def body() -> None:
            try:
                out["result"] = work(backend, auth)
            except BaseException as error:  # noqa: BLE001 - re-raised on the caller's thread
                out["error"] = error
        thread = threading.Thread(target=body, name="eval-generative", daemon=True)
        thread.start()
        owner_loop = getattr(backend, "loop", None)
        if owner_loop is None:
            thread.join()
        else:
            owner_loop(lambda: not thread.is_alive())
            thread.join()
        return 0

    be.run(parser, args, "dev", start, fixtures=fixtures, planner=planner)
    if "error" in out:
        raise out["error"]
    if "result" not in out:
        raise Refused("app_did_not_start")
    return out["result"]


def run(args: argparse.Namespace, environ: Mapping[str, str], *, fixtures: Any = None,
        planner: Any = None) -> dict[str, Any]:
    from scripts import eval_live_ops as lo
    _outside_git(args.root)
    state = args.state or environ.get(lo.real_state_env())
    email = args.reader_email
    if args.reader_env:
        try:
            email = str(json.loads(environ.get(args.reader_env) or "{}").get("email") or "")
        except (ValueError, AttributeError):
            email = ""
    reader_cleared(email, environ, Path(state) if state else None)
    items = load_items(args.root, args.set)
    if args.ids:
        wanted = set(args.ids.split(","))
        items = [i for i in items if i["id"] in wanted]
    if args.limit:
        items = items[:args.limit]
    out = _private_dir(args.root / "runs" / args.name)
    answers = _private_dir(out / "answers")
    done = {p.stem for p in answers.glob("*.json")}
    todo = [i for i in items if i["id"] not in done]

    def work(backend: Any, auth: Any) -> tuple[Counter, float]:
        session = Session(auth, shlex.split(args.mint_cmd) if args.mint_cmd else [],
                          Path(args.access_link).expanduser())
        session.login(0)
        signed_in = str(getattr(session.reader(), "email", "") or "")
        if signed_in.strip().lower() != email.strip().lower():
            # the identity the app signed in must be the reader the exclusion cleared
            raise Refused("signed_in_reader_is_not_the_cleared_reader")
        log_lock = threading.Lock()
        log_fd = os.open(out / "log.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)

        def one(item: Mapping[str, Any]) -> str:
            while _available_gb() < args.min_available_gb:
                time.sleep(20)
            start = time.time()
            answer, status = ask(backend, session, item["question"])
            seconds = round(time.time() - start, 1)
            if answer is not None:
                _write_json(answers / f"{item['id']}.json", answer)
            line = json.dumps({"id": item["id"], "status": status, "seconds": seconds,
                               "outcome": (answer or {}).get("outcome"),
                               "available_gb": round(_available_gb(), 1)}) + "\n"
            with log_lock:
                os.write(log_fd, line.encode())
            return status

        began = time.time()
        try:
            with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
                statuses = Counter(pool.map(one, todo))
        finally:
            os.close(log_fd)
        return statuses, time.time() - began

    if todo:
        statuses, wall = in_process(shlex.split(args.api_args), work, fixtures=fixtures,
                                    planner=planner)
    else:
        statuses, wall = Counter(), 0.0
    _LOGGED.pop(out, None)   # this run's log, read afresh
    report = grade_run(args.root, args.set, args.name, args.reader_markings)
    report["run"] = {"asked": len(todo), "skipped_done": len(items) - len(todo),
                     "statuses": dict(statuses), "wall_seconds": round(wall),
                     "concurrency": args.concurrency}
    return report


def grade_run(root: Path, split: str, name: str, markings: str) -> dict[str, Any]:
    items = load_items(root, split)
    out = root / "runs" / name
    held = [m for m in markings.split(",") if m]
    verdicts = []
    for item in items:
        path = out / "answers" / f"{item['id']}.json"
        if not path.is_file():
            if not _logged(out, item["id"]):
                continue   # not asked in this run
            answer = None
        else:
            answer = json.loads(path.read_text("utf-8"))
        verdicts.append(gen.grade(item, answer, held))
    _write_lines(out / "verdicts.jsonl", verdicts)
    report = gen.summary(verdicts)
    _write_json(out / "summary.json", report)
    return report


_LOGGED: dict[Path, set[str]] = {}


def _logged(out: Path, item_id: str) -> bool:
    if out not in _LOGGED:
        path = out / "log.jsonl"
        _LOGGED[out] = {json.loads(l)["id"] for l in path.read_text().splitlines() if l} \
            if path.is_file() else set()
    return item_id in _LOGGED[out]


# --------------------------------------------------------------------------- truth again

# The environment a worker process needs to rebuild the people guard (no credentials).
_GUARD_ENV = ("PLX_EXCLUDED_PEOPLE", "PLX_WORKBENCH_STATE_DIR")
RECIPES = "recipes.jsonl"
HEADER = "recipes.json"


def _digest(value: Any) -> str:
    import hashlib
    return hashlib.sha256(gen.dumps(value).encode() if not isinstance(value, str)
                          else value.encode()).hexdigest()[:24]


def _census(path: Path, generation: str | None = None) -> dict[str, Any]:
    census = json.loads(Path(path).read_text("utf-8"))
    if census.get("format") != egw.CENSUS_FORMAT or \
            (generation is not None and census.get("generation") != generation):
        raise Refused("census_of_another_generation")
    return census


def _guard_env(environ: Mapping[str, str]) -> dict[str, str]:
    return {k: v for k, v in environ.items() if k.startswith(_GUARD_ENV)}


def open_source(spec: Mapping[str, Any]) -> Any:
    """The store a spec names, people guard included: ``kind`` ``sqlite`` (a generation's
    ``store`` file) or ``pg`` (``dsn`` at ``as_of``, read-only)."""
    state = Path(spec["state"])
    guard, own, boxes = egw.people_guard(state, spec["env"])   # refuses fail-closed
    samples = _census(Path(spec["census"]))["samples"]
    if spec["kind"] == "sqlite":
        return GenStore(Path(spec["store"]), samples, guard, own, boxes)
    from scripts import eval_pg_store as pgs
    return pgs.PgGenStore(pgs.connect(spec["dsn"], spec.get("timeout_ms") or
                                      pgs.STATEMENT_TIMEOUT_MS),
                          samples, guard, own, boxes, as_of=spec.get("as_of"))


def _generator(spec: Mapping[str, Any], header: Mapping[str, Any]) -> gen.Generator:
    census = _census(Path(spec["census"]))
    return gen.Generator(egw.schema_links(census["counts"]), gen.Pinned(open_source(spec)),
                         date.fromisoformat(spec["day"]), header["types"])


_WORKER: dict[str, Any] = {}


def _worker_start(spec: Mapping[str, Any], header: Mapping[str, Any]) -> None:
    if spec["kind"] == "pg":
        _WORKER["replayer"] = Replayer(spec, header)
    else:
        _WORKER["generator"] = _generator(spec, header)
    _WORKER["seed"] = header["seed"]


def _worker_turn(task: tuple[str, int]) -> tuple[int, Any, Any]:
    category, turn = task
    got, pins = _WORKER["generator"].composed(category, _WORKER["seed"], turn)
    return turn, got, pins


def _worker_replay(recipe: Mapping[str, Any]) -> dict[str, Any]:
    return _WORKER["replayer"].one(recipe)


class Replayer:
    """Recipes composed again over Postgres at the truth time; a question that names the
    set's own day (a walk's "As of <day>, ...") is composed as of that day, its truth
    the records as they stood at its end (or at the truth time, if earlier)."""

    def __init__(self, spec: Mapping[str, Any], header: Mapping[str, Any]) -> None:
        self.spec, self.header = dict(spec), header
        self.generators: dict[str, gen.Generator] = {}
        day = str(header["day"])
        end = f"{day}T23:59:59.999999Z"
        self.anchor = None if day == spec["day"] else {
            **self.spec, "day": day, "as_of": min(end, spec["as_of"])}

    def _generator(self, which: str) -> gen.Generator:
        if which not in self.generators:
            self.generators[which] = _generator(
                self.spec if which == "now" else self.anchor, self.header)
        return self.generators[which]

    def one(self, recipe: Mapping[str, Any]) -> dict[str, Any]:
        got = replay_one(self._generator("now"), self.header["seed"], recipe)
        if got["status"] == "question_changed" and self.anchor is not None:
            again = replay_one(self._generator("anchor"), self.header["seed"], recipe)
            if again["status"] != "question_changed":
                return {**again, "truth_as_of": self.anchor["as_of"]}
        return got

    def close(self) -> None:
        for generator in self.generators.values():
            generator.source.close()


def _pool(spec: Mapping[str, Any], header: Mapping[str, Any], workers: int) -> Any:
    from concurrent.futures import ProcessPoolExecutor
    import multiprocessing
    return ProcessPoolExecutor(max_workers=workers, initializer=_worker_start,
                               initargs=(dict(spec), dict(header)),
                               mp_context=multiprocessing.get_context("spawn"))


def pool_turns(pool: Any, window: int) -> Any:
    """``gen.build``'s ``turns`` composed by ``pool`` up to ``window`` turns ahead (a turn
    is a pure function of the store, seed, category and turn); those past the last one
    the build takes are cancelled or dropped."""
    from collections import deque

    def turns(category: str, limit: int) -> Iterable[tuple[int, Any, Any]]:
        pending: Any = deque()
        nxt = 1
        try:
            while pending or nxt <= limit:
                while nxt <= limit and len(pending) < window:
                    pending.append(pool.submit(_worker_turn, (category, nxt)))
                    nxt += 1
                yield pending.popleft().result()
        finally:
            for f in pending:
                f.cancel()
    return turns


def _overlap(a: str, b: str) -> float:
    import re
    x, y = set(re.findall(r"\w+", a.casefold())), set(re.findall(r"\w+", b.casefold()))
    return len(x & y) / max(1, len(x | y))


def _match_key(category: str, start_type: str, rule: str, hops: Sequence[str],
               facts: Any) -> str:
    return _digest([category, start_type, rule, list(hops), facts])


def recipes(args: argparse.Namespace, environ: Mapping[str, str]) -> dict[str, Any]:
    """Write down how each item of the set was composed (its category's turn and the
    records it picked: ``gen.Pinned``) by composing it again over the store it was built
    from (the SQLite generation the manifest names, published or not; refused when it is
    gone), so
    ``truth`` can compose the same question over the served store later. Whole set: the
    build replayed (seed from the manifest, ``--items``/``--dev-items`` as the build had
    them), each item checked against the kept one by id (``note_set``). ``--ids`` (dev items only): each found by its truth among its category's
    turns. Writes ``ROOT/<split>/recipes.jsonl`` (0600) and ``ROOT/recipes.json``; counts
    only on stdout."""
    from scripts import eval_live_ops as lo
    if any(not i.startswith("dev-") for i in args.ids.split(",") if i):
        raise Refused("ids_mode_reads_dev_items_only")   # held-out items stay unseen
    _outside_git(args.root)
    named = args.state or environ.get(lo.real_state_env())
    if not named or not Path(named).is_dir():
        raise Refused("state_missing")
    manifest = json.loads((args.root / "manifest.json").read_text("utf-8"))
    # the generation the set was built from, published or not (a later one may be)
    generation, day = str(manifest.get("generation") or ""), str(manifest.get("as_of") or "")
    store_path = Path(named) / "projection" / "generations" / generation / "store.sqlite"
    if not generation or not day or not store_path.is_file():
        raise Refused("store_of_the_sets_generation_missing")
    census = _census(args.census, generation)
    want = {c: max(1, round(args.items * share)) for c, share in gen.QUOTA.items()}
    if manifest.get("wanted") != want or manifest.get("seed") is None:
        raise Refused("items_are_not_the_sets_build_size")   # --items as the build had it
    current = json.loads((Path(named) / "projection" / "CURRENT").read_text("utf-8"))
    spec = {"kind": "sqlite", "state": str(named), "store": str(store_path),
            "census": str(args.census), "day": day, "env": _guard_env(environ)}
    old_header = args.root / HEADER
    prior = json.loads(old_header.read_text("utf-8")) if old_header.is_file() else {}
    probe = open_source(spec)
    try:
        types = type_counts(probe)
        seen_until = prior.get("seen_until") if prior.get("generation") == generation \
            else None
        if seen_until is None:
            seen_until = {t: _seen_until(probe, t) for t in sorted(types)}
    finally:
        probe.close()
    header = {"format": "plx-eval-gen-recipes/v1", "generation": generation,
              "base_time": str(current["committed_at"])
              if str(current.get("generation")) == generation else
              prior.get("base_time") or max(v for v in seen_until.values() if v),
              "day": day,
              "seed": manifest["seed"], "types": types, "census": str(args.census),
              "census_digest": _digest(Path(args.census).read_text("utf-8")),
              "links": len(census["counts"]),
              "seen_until": {t: v for t, v in seen_until.items() if v}}
    if prior:
        if {k: prior.get(k) for k in ("generation", "seed", "census_digest", "types")} != \
                {k: header[k] for k in ("generation", "seed", "census_digest", "types")}:
            raise Refused("recipes_of_another_base")
    found: dict[str, dict[str, Any]] = {}
    counts: Counter = Counter()
    if args.ids:
        wanted = {i for i in args.ids.split(",") if i}
        built = load_items(args.root, "dev") if args.built_items is None else [
            json.loads(line) for line in Path(args.built_items).read_text("utf-8").splitlines()
            if line]
        items = {i["id"]: i for i in built if i["id"] in wanted and i["split"] == "dev"}
        by_category: dict[str, dict[str, list[str]]] = {}
        for item in items.values():
            by_category.setdefault(item["category"], {}).setdefault(_match_key(
                item["category"], item["start_type"], item["rule"], item["link_types"],
                item["facts"]), []).append(item["id"])
        generator = _generator(spec, header)
        try:
            for category, keys in sorted(by_category.items()):
                for turn in range(1, want[category] * 25 + 1):
                    if not keys or (args.max_turns and turn > args.max_turns):
                        break
                    q, pins = generator.composed(category, header["seed"], turn)
                    if isinstance(q, gen.Dropped) or q.category != category:
                        continue
                    gen.finish(q, generator.source)
                    key = _match_key(q.category, gw.object_type(q.start), q.rule, q.hops,
                                     q.facts)
                    if key in keys:
                        # items of one truth (a filter that kept every record): the one
                        # whose words the question shares most (a style keeps most)
                        item_id = max(keys[key], key=lambda i: _overlap(
                            items[i]["question"], q.text))
                        keys[key].remove(item_id)
                        if not keys[key]:
                            del keys[key]
                        found[item_id] = _recipe(item_id, q, pins, category, turn)
                counts["not_found"] += sum(map(len, keys.values()))
        finally:
            generator.source.close()
        counts["matched_by_truth"] = len(found)
    else:
        kept = [i for split in ("dev", "held") for i in load_items(args.root, split)]
        generator = _generator(spec, header)
        try:
            if args.workers > 1:
                with _pool(spec, header, args.workers) as pool:
                    found, counts = note_set(generator, kept, items=args.items,
                                             seed=header["seed"], dev_items=args.dev_items,
                                             generation=generation,
                                             turns=pool_turns(pool, args.workers * 4))
            else:
                found, counts = note_set(generator, kept, items=args.items,
                                         seed=header["seed"], dev_items=args.dev_items,
                                         generation=generation)
        finally:
            generator.source.close()
        counts["kept_items"] = len(kept)
    _write_json(old_header, header)
    for split, prefix in (("dev", "dev-"), ("held", "ho-")):
        path = args.root / split / RECIPES
        prior = {}
        if path.is_file():
            prior = {r["id"]: r for r in map(json.loads, path.read_text("utf-8").splitlines())}
        prior.update({k: v for k, v in found.items() if k.startswith(prefix)})
        _write_lines(path, [prior[k] for k in sorted(prior)])
        counts[f"recipes_{split}"] = len(prior)
    return {"recipes": dict(sorted(counts.items())), "generation": generation,
            "base_time": header["base_time"]}


def _seen_until(store: GenStore, object_type: str) -> str | None:
    """The latest time a current version of the type was recorded in the base store (what
    it had seen of that type's source)."""
    lo, hi = store._range(object_type, "")
    row = store.conn.execute(
        "SELECT max(recorded_at) FROM object_versions WHERE object_id >= ? AND "
        "object_id < ? AND superseded_at IS NULL", (lo, hi)
    ).fetchone()
    return row[0] if row else None


def note_set(generator: gen.Generator, kept: Sequence[Mapping[str, Any]], *, items: int,
             seed: Any, dev_items: int, generation: str, turns: Any = None
             ) -> tuple[dict[str, dict[str, Any]], Counter]:
    """The set's build replayed over ``generator`` (a ``gen.Pinned`` source): each kept
    item's recipe, by id where the build gives the same item (``reproduced``, or
    ``reproduced_truth_edited_since`` when its facts were fixed by hand), else by its
    truth among the build's questions (``matched_by_truth``); the rest ``not_found``."""
    by_id = {i["id"]: i for i in kept}
    by_key: dict[str, list[str]] = {}
    for item in kept:
        by_key.setdefault(_match_key(item["category"], item["start_type"], item["rule"],
                                     item["link_types"], item["facts"]), []).append(item["id"])
    found: dict[str, dict[str, Any]] = {}
    counts: Counter = Counter()
    later: list[tuple[str, gen.Question, Any]] = []

    def trace(item: Mapping[str, Any], q: gen.Question, pins: Any) -> None:
        old = by_id.get(item["id"])
        if old is not None and (old["question"], old["category"]) == \
                (item["question"], item["category"]):
            counts["reproduced" if old["facts"] == item["facts"] else
                   "reproduced_truth_edited_since"] += 1
            found[item["id"]] = _recipe(item["id"], q, pins, *q.origin)
        else:
            later.append((_match_key(item["category"], item["start_type"], item["rule"],
                                     item["link_types"], item["facts"]), q, pins))

    gen.build(generator, items=items, seed=seed, dev_share=dev_items / max(items, 1),
              generation=generation, trace=trace, turns=turns)
    for key, q, pins in later:
        free = [i for i in by_key.get(key, ()) if i not in found]
        if free:
            item_id = max(free, key=lambda i: _overlap(by_id[i]["question"], q.text))
            found[item_id] = _recipe(item_id, q, pins, *q.origin)
            counts["matched_by_truth"] += 1
    counts["not_found"] += len(by_id) - len(found)
    return found, counts


def _recipe(item_id: str, q: gen.Question, pins: Any, category: str, turn: int
            ) -> dict[str, Any]:
    """How an item was composed (no question text: digests of its text and truth)."""
    return {"id": item_id, "category": category, "turn": turn,
            "pins": pins or {"picks": [], "draws": []},
            "text": _digest(q.text), "truth": _digest(q.facts)}


def replay_one(generator: gen.Generator, seed: Any, recipe: Mapping[str, Any]
               ) -> dict[str, Any]:
    """The recipe's question composed again over the generator's store: ``status``
    ``dropped:<why>`` (it no longer arises), ``question_changed`` (other records picked
    or named), else its truth (``gen.truth_of``) with ``same`` or ``changed`` against the
    truth written down with the recipe."""
    try:
        q, _pins = generator.composed(recipe["category"], seed, recipe["turn"],
                                      recipe["pins"])
        if not isinstance(q, gen.Dropped):
            gen.finish(q, generator.source)
    except Exception as error:  # noqa: BLE001 - one item's store failure, never the pass's
        return {"id": recipe["id"], "status": f"error:{type(error).__name__}"}
    if isinstance(q, gen.Dropped):
        return {"id": recipe["id"], "status": f"dropped:{q}"}
    if _digest(q.text) != recipe["text"]:
        return {"id": recipe["id"], "status": "question_changed"}
    return {"id": recipe["id"], "truth": gen.truth_of(q),
            "status": "same" if _digest(q.facts) == recipe["truth"] else "changed"}


def truth(args: argparse.Namespace, environ: Mapping[str, str]) -> dict[str, Any]:
    """Each item's truth composed again from the served Postgres store as it stood at
    ``--as-of`` (default: now), by its recipe: the same question over the same picked
    records, its facts read from the records as they are then. Writes ``OUT`` (outside
    git) with the same split and ids: ``OUT/<split>/items.jsonl`` (items whose question
    still arises, truth replaced, ``as_of`` the stated time) and ``OUT/<split>/truth.jsonl``
    (id, status, why a changed truth changed: a cited record with a version, value or
    firm link recorded or superseded after the base generation, else ``unexplained``),
    and ``OUT/manifest.json``; a dropped or changed question is explained alike. Stdout:
    counts only."""
    from scripts import eval_live_ops as lo
    from scripts import eval_pg_store as pgs
    _outside_git(args.root)
    _outside_git(args.out)
    if args.out.resolve() == args.root.resolve():
        raise Refused("out_is_the_root")
    named = args.state or environ.get(lo.real_state_env())
    if not named or not Path(named).is_dir():
        raise Refused("state_missing")
    header = json.loads((args.root / HEADER).read_text("utf-8"))
    census_path = Path(args.census or header["census"])
    if _digest(census_path.read_text("utf-8")) != header["census_digest"]:
        raise Refused("census_is_not_the_recipes")
    as_of = args.as_of or datetime.now(timezone.utc).isoformat(timespec="microseconds") \
        .replace("+00:00", "Z")
    stamp = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    if stamp.tzinfo is None or stamp.utcoffset().total_seconds() != 0 or \
            not as_of.endswith("Z"):
        raise Refused("as_of_must_be_utc_z")
    spec = {"kind": "pg", "state": str(named), "census": str(census_path),
            "day": as_of[:10], "dsn": args.dsn or _pg_dsn(environ), "as_of": as_of,
            "timeout_ms": args.statement_timeout_ms, "env": _guard_env(environ)}
    splits = ["dev", "held"] if args.set == "all" else [args.set]
    wanted = {i for i in args.ids.split(",") if i} if args.ids else None
    counts: Counter = Counter()
    report: dict[str, Any] = {}
    _private_dir(args.out)
    began = time.time()
    for split in splits:
        items = load_items(args.root, split)
        path = args.root / split / RECIPES
        recipes_by_id = {r["id"]: r for r in map(json.loads, path.read_text(
            "utf-8").splitlines())} if path.is_file() else {}
        if wanted is not None:
            items = [i for i in items if i["id"] in wanted]
        todo = [recipes_by_id[i["id"]] for i in items if i["id"] in recipes_by_id]
        if args.workers > 1 and len(todo) > 1:
            with _pool(spec, header, args.workers) as pool:
                results = list(pool.map(_worker_replay, todo, chunksize=4))
        else:
            replayer = Replayer(spec, header)
            try:
                results = [replayer.one(r) for r in todo]
            finally:
                replayer.close()
        by_id = {r["id"]: r for r in results}
        changed = [r for r in results if r["status"] == "changed" or
                   r["status"].startswith(("dropped:", "question_changed"))]
        explain = pgs.PgGenStore(pgs.connect(spec["dsn"], args.statement_timeout_ms),
                                 {}, None, frozenset(), (), as_of=as_of)
        try:
            kept_by_id = {i["id"]: i for i in items}
            for result in changed:
                old = kept_by_id[result["id"]]
                handles = sorted(set(old["citations"]) |
                                 set(result.get("truth", {}).get("citations", ())))
                why = explain.changed_after(header.get("seen_until") or header["base_time"],
                                            handles, default=header["base_time"])
                result["why"] = sorted(set(why.values())) or ["unexplained"]
        finally:
            explain.close()
        out_items, log = [], []
        for item in items:
            result = by_id.get(item["id"])
            if result is None:
                log.append({"id": item["id"], "status": "no_recipe"})
                counts[f"{split}:no_recipe"] += 1
                continue
            entry = {"id": item["id"], "status": result["status"]}
            if "why" in result:
                entry["why"] = result["why"]
                for reason in result["why"]:
                    counts[f"{split}:changed_by:{reason}"] += 1
            log.append(entry)
            counts[f"{split}:{result['status']}"] += 1
            if "truth" in result:
                when = result.get("truth_as_of", as_of)
                if when != as_of:
                    entry["truth_as_of"] = when
                    counts[f"{split}:as_of_its_own_day"] += 1
                out_items.append({**item, **result["truth"], "as_of": when[:10],
                                  "truth_as_of": when, "generation": "postgres"})
        _write_lines(args.out / split / "items.jsonl", out_items)
        _write_lines(args.out / split / "truth.jsonl", log)
        report[split] = {"items": len(items), "kept": len(out_items)}
    manifest = {"format": "plx-eval-gen-truth/v1", "truth_as_of": as_of,
                "base_generation": header["generation"], "base_time": header["base_time"],
                "seed": header["seed"], "counts": dict(sorted(counts.items())),
                "splits": report, "seconds": round(time.time() - began),
                "workers": args.workers}
    _write_json(args.out / "manifest.json", manifest)
    return manifest


def _pg_dsn(environ: Mapping[str, str]) -> str:
    from operating.pg import config
    return config.dsn(environ)


# --------------------------------------------------------------------------- main

def main(argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build")
    b.add_argument("--root", type=Path, required=True)
    b.add_argument("--state", type=Path)
    b.add_argument("--census", type=Path, required=True)
    b.add_argument("--items", type=int, default=3300)
    b.add_argument("--dev-items", type=int, default=300)
    b.add_argument("--seed", type=int, default=7)
    c = sub.add_parser("recipes")
    c.add_argument("--root", type=Path, required=True)
    c.add_argument("--state", type=Path)
    c.add_argument("--census", type=Path, required=True)
    c.add_argument("--ids", default="", help="dev item ids (found by their truth)")
    c.add_argument("--built-items", type=Path,
                   help="the dev items as built (their truth before any hand fix)")
    c.add_argument("--max-turns", type=int, default=0)
    c.add_argument("--items", type=int, default=3300, help="the build's --items")
    c.add_argument("--dev-items", type=int, default=300, help="the build's --dev-items")
    c.add_argument("--workers", type=int, default=1)
    t = sub.add_parser("truth")
    t.add_argument("--root", type=Path, required=True)
    t.add_argument("--out", type=Path, required=True)
    t.add_argument("--set", choices=("dev", "held", "all"), required=True)
    t.add_argument("--ids", default="")
    t.add_argument("--state", type=Path)
    t.add_argument("--census", type=Path)
    t.add_argument("--dsn", default="")
    t.add_argument("--as-of", default="", help="an ISO UTC time ending in Z (default now)")
    t.add_argument("--workers", type=int, default=1)
    t.add_argument("--statement-timeout-ms", type=int, default=120_000)
    for name in ("run", "grade"):
        r = sub.add_parser(name)
        r.add_argument("--root", type=Path, required=True)
        r.add_argument("--set", choices=sorted(SPLITS), required=True)
        r.add_argument("--name", required=True)
        r.add_argument("--reader-markings", default=OWNER_MARKINGS)
        if name == "run":
            r.add_argument("--state", type=Path)
            r.add_argument("--api-args", required=True,
                           help="the app's own `operator_api serve --dev ...` flags")
            r.add_argument("--concurrency", type=int, default=3)
            r.add_argument("--limit", type=int, default=0)
            r.add_argument("--ids", default="")
            r.add_argument("--mint-cmd", default="")
            r.add_argument("--access-link", default="~/.config/plx/workbench-access-link")
            r.add_argument("--reader-email", default="")
            r.add_argument("--reader-env", default="",
                           help="a variable holding the reader's claims JSON (its email)")
            r.add_argument("--min-available-gb", type=float, default=12)
    args = parser.parse_args(argv)
    environ = os.environ if environ is None else environ
    try:
        if args.command == "build":
            result = build(args, environ)
        elif args.command == "recipes":
            result = recipes(args, environ)
        elif args.command == "truth":
            result = truth(args, environ)
        elif args.command == "run":
            result = run(args, environ)
        else:
            result = grade_run(args.root, args.set, args.name, args.reader_markings)
        print(json.dumps(result, indent=1, sort_keys=True))
        return 0
    except (Refused, egw.Refused, OSError, ValueError, sqlite3.Error,
            subprocess.SubprocessError) as exc:
        print(json.dumps({"error": type(exc).__name__, "code": str(exc)[:200]}),
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
