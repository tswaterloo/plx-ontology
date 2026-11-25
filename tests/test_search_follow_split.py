"""Split Spark-compute / box-apply tests for ``operating.embed follow --mode layers``."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from integrations.people_exclusion import PeopleExclusion
from operating.chunking import make_chunk
from operating.chunking.permissions import Exclusion
from operating.pg import scratch_db

scratch_db.use_test_cluster()

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED = "0e0e0e0e-0000-4000-8000-00000000e0e1"
STAFF = "0f0f0f0f-0000-4000-8000-0000000000a1"
STAFF_BOX = "0f0f0f0f-0000-4000-8000-0000000000b2"


def _pg_available() -> bool:
    return shutil.which("createdb") is not None and shutil.which("psql") is not None


def _exclusion(*, excluded=(EXCLUDED,)) -> Exclusion:
    return Exclusion(
        PeopleExclusion(list(excluded)),
        directory={"staff@example-plx.com": STAFF, "gone@example-plx.com": EXCLUDED},
        internal_domains=("example-plx.com",),
        drive_owners={},
        chat_members={},
    )


FAKE_WORKER = textwrap.dedent("""
    import hashlib, json, sys
    import numpy as np
    sys.path.insert(0, sys.argv[1])
    from operating.embed import remote_worker as rw

    def encode(texts):
        out = []
        for t in texts:
            seed = int.from_bytes(hashlib.sha256(t.encode()).digest()[:8], "big")
            v = np.random.default_rng(seed).standard_normal(1024).astype(np.float32)
            out.append(v / np.linalg.norm(v))
        return np.stack(out) if out else np.zeros((0, 1024), dtype=np.float32)

    hello = {"model": "BAAI/bge-m3", "dim": 1024, "device": "fake", "precision": "fp32",
             "max_seq_length": rw.MAX_SEQ_LENGTH, "max_chars": rw.LOCAL_MAX_CHARS,
             "revision": rw.REVISIONS["BAAI/bge-m3"]}
    rw.serve(sys.stdin.buffer, sys.stdout.buffer, encode, hello)
""")


class _Fixture:
    def __init__(self, path: Path) -> None:
        self.conn = sqlite3.connect(path)
        self.conn.executescript("""
            CREATE TABLE objects (object_id TEXT PRIMARY KEY, object_type TEXT,
              binding_id TEXT, native_key TEXT, audience_key TEXT, first_recorded_at TEXT,
              missing_from_source_since TEXT);
            CREATE TABLE object_versions (object_id TEXT, l1_handle TEXT, digest TEXT,
              lineage TEXT, markings TEXT, recorded_at TEXT, superseded_at TEXT);
            CREATE TABLE text_fields (field_id INTEGER PRIMARY KEY, object_id TEXT,
              version_handle TEXT, citation TEXT, field_path TEXT, document_kind TEXT,
              sender_class TEXT, markings TEXT, text_digest TEXT);
            CREATE TABLE text_chunks (chunk_id INTEGER PRIMARY KEY, field_id INTEGER,
              start INTEGER, body BLOB);
            CREATE TABLE object_values (object_id TEXT, property TEXT, value TEXT,
              markings TEXT, superseded_at TEXT, l1_handle TEXT, field_path TEXT,
              value_type TEXT);
            CREATE TABLE link_rows (link_id TEXT, link_type TEXT, from_object TEXT,
              to_object TEXT, tier TEXT, binding_kind TEXT);
            CREATE TABLE identifiers (identifier_kind TEXT, normalized TEXT, raw TEXT,
              role TEXT, object_id TEXT, markings TEXT, superseded_at TEXT,
              property TEXT);
            CREATE TABLE delta_owned (object_id TEXT PRIMARY KEY);
        """)
        self.n = 0

    def obj(self, oid: str, otype: str, audience: str, *, handle: str | None = None,
            marks: str = "[]", values: dict | None = None, text: dict | None = None,
            kind: str | None = None, owned: bool = True) -> str:
        import zlib
        handle = handle or f"fx://{oid}@1"
        self.conn.execute("INSERT INTO objects VALUES (?,?,?,?,?,?,NULL)",
                          (oid, otype, "b", "{}", audience, "2026-10-01"))
        self.conn.execute("INSERT INTO object_versions VALUES (?,?,?,?,?,?,NULL)",
                          (oid, handle, "d", "mail", marks, "2026-10-01T00:00:00Z"))
        for prop, (value, vmarks) in (values or {}).items():
            self.conn.execute("INSERT INTO object_values VALUES (?,?,?,?,NULL,?,?,?)",
                              (oid, prop, value, vmarks, handle, f"row.{prop}", "text"))
        for path, (citation, body) in (text or {}).items():
            self.n += 1
            self.conn.execute("INSERT INTO text_fields VALUES (?,?,?,?,?,?,?,?,?)",
                              (self.n, oid, handle, citation, path, kind, "unknown", marks, "x"))
            self.conn.execute("INSERT INTO text_chunks (field_id, start, body) VALUES (?,?,?)",
                              (self.n, 0, zlib.compress(body.encode())))
        if owned:
            self.conn.execute("INSERT OR IGNORE INTO delta_owned VALUES (?)", (oid,))
        return handle

    def done(self) -> None:
        self.conn.commit()
        self.conn.close()


@unittest.skipUnless(_pg_available(), "no local Postgres")
class SearchFollowSplitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db = f"plx_test_sf_split_{os.getpid()}"
        made = subprocess.run(["createdb", cls.db], capture_output=True, text=True)
        if made.returncode != 0:
            raise unittest.SkipTest("createdb failed")
        env = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY"}
        env["PLX_PG_DSN"] = f"postgresql:///{cls.db}"
        done = subprocess.run([sys.executable, "-m", "operating.pg", "migrate"], cwd=ROOT,
                              env=env, capture_output=True, text=True)
        if done.returncode != 0:
            subprocess.run(["dropdb", "--if-exists", cls.db], capture_output=True)
            raise AssertionError(f"migrate failed: {done.stderr[-500:]}")

    @classmethod
    def tearDownClass(cls) -> None:
        subprocess.run(["dropdb", "--if-exists", cls.db], capture_output=True)

    def setUp(self) -> None:
        from operating.embed import store as st
        self.st = st
        self.dsn = f"postgresql:///{self.db}"
        self.conn = st.connect(self.dsn)
        self.addCleanup(self.conn.close)
        self._truncate()
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = self.tmp / "state"
        proj = self.state / "projection"
        (proj / "delta").mkdir(parents=True)
        (proj / "CURRENT").write_text(json.dumps(
            {"generation": "g1", "content_digest": "sha256:aaa"}))
        self.worker_script = self.tmp / "fake_worker.py"
        self.worker_script.write_text(FAKE_WORKER, encoding="utf-8")
        self.worker_cmd = f"{sys.executable} {self.worker_script} {ROOT}"

    def _truncate(self) -> None:
        self.conn.execute("TRUNCATE search.chunks, search.entities")
        if self.conn.execute("SELECT to_regclass('search.audience_units')").fetchone()[0]:
            self.conn.execute("TRUNCATE search.audience_units")
        if self.conn.execute("SELECT to_regclass('ontology.load_state')").fetchone()[0]:
            self.conn.execute(
                "TRUNCATE ontology.objects, ontology.object_versions, ontology.value_cells, "
                "ontology.value_sources, ontology.value_fields, ontology.value_kinds, "
                "ontology.text_fields, ontology.text_chunks, ontology.link_rows, "
                "ontology.identifiers, ontology.load_state")
        self.conn.commit()

    def _copy_layer_to_pg(self, layer_path: Path, *, mark_applied: bool = True) -> None:
        src = sqlite3.connect(layer_path)
        src.row_factory = sqlite3.Row
        try:
            with self.conn.cursor() as cur:
                for r in src.execute("SELECT * FROM objects"):
                    cur.execute(
                        "INSERT INTO ontology.objects (object_id, object_type, binding_id, "
                        "native_key, audience_key, first_recorded_at, missing_from_source_since) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                        (r["object_id"], r["object_type"], r["binding_id"], r["native_key"],
                         r["audience_key"], r["first_recorded_at"], r["missing_from_source_since"]))
                vers: dict[tuple[str, str], int] = {}
                for r in src.execute("SELECT * FROM object_versions"):
                    ver = cur.execute(
                        "INSERT INTO ontology.object_versions (ver, object_id, l1_handle, "
                        "digest, lineage, markings, recorded_at, superseded_at) VALUES "
                        "(nextval('ontology.object_versions_ver'),%s,%s,%s,%s,%s,%s,%s) "
                        "RETURNING ver",
                        (r["object_id"], r["l1_handle"], r["digest"], r["lineage"],
                         r["markings"], r["recorded_at"], r["superseded_at"])).fetchone()[0]
                    vers[(r["object_id"], r["l1_handle"])] = ver
                for r in src.execute("SELECT * FROM object_values"):
                    ver = vers[(r["object_id"], r["l1_handle"])]
                    field_id = cur.execute(
                        "INSERT INTO ontology.value_fields (field, property, field_path) "
                        "VALUES (nextval('ontology.value_fields_field'), %s, %s) "
                        "ON CONFLICT (property, field_path) DO UPDATE SET property = EXCLUDED.property "
                        "RETURNING field", (r["property"], r["field_path"])).fetchone()[0]
                    kind_id = cur.execute(
                        "INSERT INTO ontology.value_kinds (kind, value_type, parser, markings) "
                        "VALUES (nextval('ontology.value_kinds_kind'), %s, 'raw', %s) "
                        "ON CONFLICT (value_type, unit, quantity_role, tax_basis, parser, "
                        "authority_rank, markings) DO UPDATE SET parser = EXCLUDED.parser "
                        "RETURNING kind", (r["value_type"], r["markings"])).fetchone()[0]
                    src_id = cur.execute(
                        "INSERT INTO ontology.value_sources (src, ver, binding_id, recorded_at) "
                        "VALUES (nextval('ontology.value_sources_src'), %s, 'b', '2026-10-01T00:00:00Z') "
                        "RETURNING src", (ver,)).fetchone()[0]
                    cur.execute(
                        "INSERT INTO ontology.value_cells (src, field, kind, value) "
                        "VALUES (%s, %s, %s, %s)", (src_id, field_id, kind_id, r["value"]))
                field_map: dict[int, int] = {}
                for r in src.execute("SELECT * FROM text_fields"):
                    new_fid = cur.execute(
                        "SELECT coalesce(max(field_id), 0) + 1 FROM ontology.text_fields"
                    ).fetchone()[0]
                    field_map[r["field_id"]] = new_fid
                    cur.execute(
                        "INSERT INTO ontology.text_fields (field_id, object_id, version_handle, "
                        "citation, field_path, document_kind, sender_class, markings, text_digest) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (new_fid, r["object_id"], r["version_handle"], r["citation"],
                         r["field_path"], r["document_kind"], r["sender_class"], r["markings"],
                         r["text_digest"]))
                for r in src.execute("SELECT * FROM text_chunks"):
                    new_cid = cur.execute(
                        "SELECT coalesce(max(chunk_id), 0) + 1 FROM ontology.text_chunks"
                    ).fetchone()[0]
                    cur.execute(
                        "INSERT INTO ontology.text_chunks (chunk_id, field_id, start, body) "
                        "VALUES (%s,%s,%s,%s)",
                        (new_cid, field_map[r["field_id"]], r["start"], r["body"]))
            if mark_applied:
                self.conn.execute(
                    "INSERT INTO ontology.load_state (name, value) VALUES (%s, '{}') "
                    "ON CONFLICT (name) DO NOTHING", (f"delta:{layer_path.name}",))
            self.conn.commit()
        finally:
            src.close()

    def _seed_layer(self, name: str, *, include_excluded_sender: bool = False) -> Path:
        layer = self.state / "projection" / "delta" / name
        fx = _Fixture(layer)
        fx.obj("MCTask:T-1", "MCTask", "mc:live/tasks/PRJ-X", kind="mc_task",
               handle="mc://live/tasks/T-1@2",
               text={"row.description": ("mc://live/tasks/T-1@2",
                                         "Task 1 waits on the carton supplier for lot 44.")},
               values={"title": ("Carton task 1", "[]")})
        fx.obj("Email:E-1", "Email", f"mail:{STAFF_BOX}", kind="email",
               handle=f"graph-mail://{STAFF_BOX}/E-1@1",
               text={"row.body_text": (f"graph-mail://{STAFF_BOX}/E-1@1",
                                       "Lavender soap shipment is confirmed for Monday.")},
               values={"sender": ("staff@example-plx.com", "[]"),
                       "to_addresses": ("['buyer@customer.example']", "[]"),
                       "received_at": ("2026-10-02T10:00:00Z", "[]"),
                       "subject": ("Shipment confirmed", "[]")})
        fx.obj("Customer:C-1", "Customer", "api:public.app_customer",
               values={"name": ("Maple Suds Inc.", "[]")}, owned=True)
        if include_excluded_sender:
            fx.obj("Email:E-EX", "Email", f"mail:{STAFF_BOX}", kind="email",
                   handle=f"graph-mail://{STAFF_BOX}/E-EX@1",
                   text={"row.body_text": (f"graph-mail://{STAFF_BOX}/E-EX@1",
                                           "Excluded sender message never searchable.")},
                   values={"sender": ("gone@example-plx.com", "[]"),
                           "received_at": ("2026-10-02T11:00:00Z", "[]")})
        fx.done()
        return layer

    def _write_pointer(self, layers: list[Path]) -> None:
        (self.state / "projection" / "delta" / "CURRENT.json").write_text(json.dumps({
            "format": "plx-delta/v1", "base_digest": "sha256:aaa",
            "store": str(layers[-1]),
            "layers": [{"store": str(p), "owned": 1} for p in reversed(layers)],
        }))

    def _base_env(self, **extra: str) -> dict[str, str]:
        return {
            "PLX_PG_DSN": self.dsn,
            "PLX_EXCLUDED_PEOPLE_OIDS": EXCLUDED,
            "PLX_EMBED_MODEL": "BAAI/bge-m3",
            "PLX_SEARCH_FOLLOW_EMBED_CMD": self.worker_cmd,
            **extra,
        }

    def _run_cli(self, argv: list[str], env: dict[str, str]) -> tuple[int, dict]:
        from operating.embed import follow as fl
        out = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(fl.config, "settings", return_value=dict(env)), \
                mock.patch.object(fl, "_exclusion", return_value=_exclusion()), \
                mock.patch("operating.embed.follow_transport._exclusion_from_context",
                           return_value=_exclusion()), \
                mock.patch("operating.embed.follow_transport._exclusion",
                           return_value=_exclusion()), \
                contextlib.redirect_stdout(out):
            code = fl.main(argv)
        text = out.getvalue().strip()
        return code, json.loads(text) if text else {}

    def test_split_compute_apply_matches_fused_roundtrip_and_never_loads_model_on_box(self):
        from operating.embed import follow as fl, models as md
        layer = self._seed_layer("delta-20261124T120000000000.sqlite",
                                 include_excluded_sender=True)
        self._write_pointer([layer])
        # Seed an earlier version of MCTask:T-1 and an excluded sender row to be purged/replaced
        old = make_chunk(kind="mc_task", parts=("mc:live/tasks/PRJ-X", "old", "0"),
                         source_handle="mc://live/tasks/T-1@1", object_id="MCTask:T-1",
                         title="T-1", author_role=None, occurred_at=None,
                         header="Source: x", body="Old task text.", linked_ids=[],
                         audience_key="mc:live/tasks/PRJ-X", markings=())
        ex_old = make_chunk(kind="mail", parts=(f"mail:{STAFF_BOX}", "ex", "0"),
                            source_handle=f"graph-mail://{STAFF_BOX}/E-EX@0",
                            object_id="Email:E-EX", title="Ex", author_role=None,
                            occurred_at=None, header="Source: x", body="Old excluded.",
                            linked_ids=[], audience_key=f"mail:{STAFF_BOX}", markings=())

        def run_fused() -> tuple[list, list, dict]:
            self._truncate()
            (self.state / "search").mkdir(exist_ok=True)
            for fname in ("follow.json", "embed-write-budget.json", "embedded_gate.json"):
                (self.state / "search" / fname).unlink(missing_ok=True)
            self.st.ChunkWriter(self.conn).write([old, ex_old])
            env = self._base_env()
            def fake_embedder(settings, **_):
                from operating.embed import remote
                import shlex
                h = remote.Host("fake", shlex.split(self.worker_cmd))
                h.start()
                self.addCleanup(h.close)
                return md.Embedder("BAAI/bge-m3", 1024, h.encode, remote=True)
            with mock.patch.object(md, "load", fake_embedder):
                code, _rep = self._run_cli([
                    "--state", str(self.state), "--mode", "layers", "--embed-seconds", "30",
                    "--embed-batch", "16", "--embed-rows", "32", "--embed-here"], env)
            self.assertEqual(code, 0)
            chunks = self.conn.execute(
                "SELECT chunk_id, object_id, body, embedding IS NOT NULL FROM search.chunks "
                "ORDER BY chunk_id").fetchall()
            ents = self.conn.execute(
                "SELECT entity_id, object_id, name FROM search.entities ORDER BY entity_id"
            ).fetchall()
            cur = json.loads((self.state / "search" / "follow.json").read_text())
            self.conn.commit()
            return chunks, ents, cur

        fused_chunks, fused_ents, fused_cur = run_fused()

        # Now run the exact same scenario through split prepare -> compute -> apply
        self._truncate()
        for fname in ("follow.json", "embed-write-budget.json", "embedded_gate.json"):
            (self.state / "search" / fname).unlink(missing_ok=True)
        self.st.ChunkWriter(self.conn).write([old, ex_old])
        ship = self.tmp / "ship"
        back = self.tmp / "back"
        env = self._base_env()

        code, prep = self._run_cli([
            "--state", str(self.state), "--mode", "layers", "--embed-seconds", "30",
            "--embed-batch", "16", "--embed-rows", "32", "--prepare-dir", str(ship)], env)
        self.assertEqual(code, 0, prep)
        self.assertTrue((ship / "manifest.json").is_file())

        code, comp = self._run_cli([
            "--state", str(self.state), "--mode", "layers", "--embed-seconds", "30",
            "--embed-batch", "16", "--embed-rows", "32",
            "--context-dir", str(ship), "--export-dir", str(back)], env)
        self.assertEqual(code, 0, comp)
        self.assertTrue((back / "export.json").is_file())

        # Box apply must NEVER call models.load or import torch / sentence_transformers
        with mock.patch.object(md, "load", side_effect=AssertionError("model loaded on box!")):
            code, applied = self._run_cli([
                "--state", str(self.state), "--mode", "layers", "--embed-seconds", "30",
                "--embed-batch", "16", "--embed-rows", "32",
                "--embed-write-mb-per-hour", "256", "--apply-dir", str(back)], env)
        self.assertEqual(code, 0, applied)
        self.assertNotIn("torch", sys.modules)
        self.assertNotIn("sentence_transformers", sys.modules)
        self.assertFalse((back / "export.json").exists())

        split_chunks = self.conn.execute(
            "SELECT chunk_id, object_id, body, embedding IS NOT NULL FROM search.chunks "
            "ORDER BY chunk_id").fetchall()
        split_ents = self.conn.execute(
            "SELECT entity_id, object_id, name FROM search.entities ORDER BY entity_id"
        ).fetchall()
        split_cur = json.loads((self.state / "search" / "follow.json").read_text())
        self.conn.commit()

        self.assertEqual(split_chunks, fused_chunks)
        self.assertEqual(split_ents, fused_ents)
        self.assertEqual(split_cur["layers"], fused_cur["layers"])
        self.assertEqual(split_cur["pending_after"], 0)
        self.assertTrue(all(r[3] for r in split_chunks))
        self.assertNotIn("Email:E-EX", {r[1] for r in split_chunks})

    def test_pg_standby_mode_respects_load_state_and_scopes_to_owned_objects(self):
        l1 = self._seed_layer("delta-20261124T120000000000.sqlite")
        l2 = self.state / "projection" / "delta" / "delta-20261124T130000000000.sqlite"
        fx2 = _Fixture(l2)
        fx2.obj("MCTask:T-2", "MCTask", "mc:live/tasks/PRJ-X", kind="mc_task",
                handle="mc://live/tasks/T-2@1",
                text={"row.description": ("mc://live/tasks/T-2@1",
                                          "Task 2 waits on packaging delivery.")})
        fx2.done()
        # Only l1 is applied to Postgres ontology; l2 is not yet in ontology.load_state
        self._copy_layer_to_pg(l1, mark_applied=True)
        self._copy_layer_to_pg(l2, mark_applied=False)
        self._write_pointer([l1, l2])

        ship = self.tmp / "ship"
        back = self.tmp / "back"
        env = self._base_env(PLX_ONTOLOGY_PG="only")

        code, _ = self._run_cli(["--state", str(self.state), "--mode", "layers",
                                 "--embed-seconds", "30", "--prepare-dir", str(ship)], env)
        self.assertEqual(code, 0)
        code, comp = self._run_cli(["--state", str(self.state), "--mode", "layers",
                                    "--embed-seconds", "30", "--context-dir", str(ship),
                                    "--export-dir", str(back)], env)
        self.assertEqual(code, 0, comp)
        self.assertEqual(comp["layers"][l2.name], {"waiting": "waiting_for_postgres"})
        code, _ = self._run_cli(["--state", str(self.state), "--mode", "layers",
                                 "--embed-seconds", "30", "--apply-dir", str(back)], env)
        self.assertEqual(code, 0)
        cur = json.loads((self.state / "search" / "follow.json").read_text())
        self.assertEqual(cur["layers"], [str(l1)])

        # Once l2 is applied in ontology.load_state, the next run indexes l2
        self.conn.execute("INSERT INTO ontology.load_state (name, value) VALUES (%s, '{}')",
                          (f"delta:{l2.name}",))
        self.conn.commit()
        shutil.rmtree(ship); shutil.rmtree(back)
        self._run_cli(["--state", str(self.state), "--mode", "layers",
                       "--embed-seconds", "30", "--prepare-dir", str(ship)], env)
        self._run_cli(["--state", str(self.state), "--mode", "layers",
                       "--embed-seconds", "30", "--context-dir", str(ship),
                       "--export-dir", str(back)], env)
        self._run_cli(["--state", str(self.state), "--mode", "layers",
                       "--embed-seconds", "30", "--apply-dir", str(back)], env)
        cur = json.loads((self.state / "search" / "follow.json").read_text())
        self.assertEqual(cur["layers"], [str(l1), str(l2)])
        oids = {r[0] for r in self.conn.execute("SELECT DISTINCT object_id FROM search.chunks")}
        self.assertIn("MCTask:T-2", oids)

    def test_wal_write_budget_and_row_cap_and_disk_floor_enforced_during_apply(self):
        from operating.embed import follow as fl
        # Seed 4 distinct pending chunks in Postgres
        chunks = [make_chunk(kind="mail", parts=(f"mail:{STAFF_BOX}", f"h{i}", "0"),
                             source_handle=f"fx://h{i}", object_id=f"O{i}", title=f"T{i}",
                             author_role=None, occurred_at="2026-10-01T00:00:00Z",
                             header="Source: x", body=f"Unique body {i} about cartons.",
                             linked_ids=[], audience_key=f"mail:{STAFF_BOX}", markings=())
                  for i in range(4)]
        self.st.ChunkWriter(self.conn).write(chunks)
        ship = self.tmp / "ship"
        back = self.tmp / "back"
        env = self._base_env()
        self._run_cli(["--state", str(self.state), "--mode", "layers",
                       "--embed-seconds", "30", "--prepare-dir", str(ship)], env)
        self._run_cli(["--state", str(self.state), "--mode", "layers",
                       "--embed-seconds", "30", "--context-dir", str(ship),
                       "--export-dir", str(back)], env)

        # Tiny WAL budget (1 byte/hr): 1st batch of 1 hash writes > 1 byte WAL, then blocks!
        code, applied = self._run_cli([
            "--state", str(self.state), "--mode", "layers", "--embed-seconds", "30",
            "--embed-batch", "1", "--embed-rows", "1", "--embed-write-mb-per-hour", "0.000001",
            "--apply-dir", str(back)], env)
        self.assertEqual(code, 0, applied)
        self.assertEqual(applied["embed"]["texts_embedded"], 1)
        self.assertTrue(applied["embed"]["write_budget"]["blocked"])
        self.assertTrue((self.state / "search" / "embed-write-budget.json").is_file())

        # Disk floor below limit skips prepare cleanly (no manifest.json)
        ship2 = self.tmp / "ship2"
        free_gb = shutil.disk_usage(self.state).free / 1e9
        code, prep2 = self._run_cli([
            "--state", str(self.state), "--mode", "layers",
            "--min-free-gb", str(free_gb + 1000), "--prepare-dir", str(ship2)], env)
        self.assertEqual(code, 0)
        self.assertEqual(prep2["skipped"], "disk_below_min_free")
        self.assertFalse((ship2 / "manifest.json").exists())

    def test_partial_slice_resumes_across_compute_apply_cycles(self):
        from operating.embed import follow as fl
        layer = self.state / "projection" / "delta" / "delta-20261124T140000000000.sqlite"
        fx = _Fixture(layer)
        for n in range(3):
            fx.obj(f"MCTask:S-{n}", "MCTask", "mc:live/tasks/PRJ-X", kind="mc_task",
                   handle=f"mc://live/tasks/S-{n}@1",
                   text={"row.description": (f"mc://live/tasks/S-{n}@1",
                                             f"Slice task {n} waits on carton lot.")})
        fx.done()
        self._write_pointer([layer])
        ship, back = self.tmp / "ship", self.tmp / "back"
        env = self._base_env()
        self._run_cli(["--state", str(self.state), "--mode", "layers", "--embed-seconds", "0",
                       "--prepare-dir", str(ship)], env)
        ticks = iter([0.0, 0.0, 0.0, 0.5, 5.0])  # start, layer, slice0 start/end, slice1 check
        with mock.patch.object(fl, "SLICE", 2), \
                mock.patch.object(fl, "_clock", lambda: next(ticks)):
            code, comp = self._run_cli([
                "--state", str(self.state), "--mode", "layers", "--layer-seconds", "1.0",
                "--embed-seconds", "0", "--context-dir", str(ship),
                "--export-dir", str(back)], env)
        self.assertEqual(code, 0, comp)
        self.assertEqual(comp["layers"][layer.name]["partial"], {"slices_done": 1, "slices": 2})
        self._run_cli(["--state", str(self.state), "--mode", "layers", "--embed-seconds", "0",
                       "--apply-dir", str(back)], env)
        cur = json.loads((self.state / "search" / "follow.json").read_text())
        self.assertEqual(cur["partial"], {"layer": str(layer), "done": 1})
        self.assertEqual(self.conn.execute(
            "SELECT count(DISTINCT object_id) FROM search.chunks").fetchone()[0], 2)

        # Second cycle completes slice 1
        shutil.rmtree(ship); shutil.rmtree(back)
        self._run_cli(["--state", str(self.state), "--mode", "layers", "--embed-seconds", "0",
                       "--prepare-dir", str(ship)], env)
        with mock.patch.object(fl, "SLICE", 2):
            self._run_cli(["--state", str(self.state), "--mode", "layers", "--embed-seconds", "0",
                           "--context-dir", str(ship), "--export-dir", str(back)], env)
        self._run_cli(["--state", str(self.state), "--mode", "layers", "--embed-seconds", "0",
                       "--apply-dir", str(back)], env)
        cur2 = json.loads((self.state / "search" / "follow.json").read_text())
        self.assertNotIn("partial", cur2)
        self.assertEqual(cur2["layers"], [str(layer)])
        self.assertEqual(self.conn.execute(
            "SELECT count(DISTINCT object_id) FROM search.chunks").fetchone()[0], 3)

    def test_people_exclusion_fail_closed_and_stale_or_bad_exports_refuse(self):
        from operating.embed import follow as fl
        layer = self._seed_layer("delta-20261124T150000000000.sqlite")
        self._write_pointer([layer])
        ship, back = self.tmp / "ship", self.tmp / "back"
        env = self._base_env()
        self._run_cli(["--state", str(self.state), "--mode", "layers", "--embed-seconds", "30",
                       "--prepare-dir", str(ship)], env)
        self._run_cli(["--state", str(self.state), "--mode", "layers", "--embed-seconds", "30",
                       "--context-dir", str(ship), "--export-dir", str(back)], env)

        # 1. Unavailable exclusion refuses apply with exit 2 and leaves DB untouched
        down = Exclusion(PeopleExclusion([]))
        out = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(fl.config, "settings", return_value=dict(env)), \
                mock.patch("operating.embed.follow_transport._exclusion", return_value=down), \
                contextlib.redirect_stdout(out):
            code = fl.main(["--state", str(self.state), "--mode", "layers",
                            "--apply-dir", str(back)])
        self.assertEqual(code, 2)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM search.chunks").fetchone()[0], 0)
        self.assertTrue((back / "export.json").is_file())

        # 2. Stale export (served digest moved) refuses with exit 1 and writes nothing
        (self.state / "projection" / "CURRENT").write_text(json.dumps(
            {"generation": "g2", "content_digest": "sha256:bbb"}))
        code, stale = self._run_cli(["--state", str(self.state), "--mode", "layers",
                                     "--apply-dir", str(back)], env)
        self.assertEqual(code, 1)
        self.assertEqual(stale["stale"], 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM search.chunks").fetchone()[0], 0)
        self.assertTrue((back / "export.json").is_file())
        (self.state / "projection" / "CURRENT").write_text(json.dumps(
            {"generation": "g1", "content_digest": "sha256:aaa"}))

        # 3. Bad export (corrupt vectors.npy shape) refuses with exit 1 and writes nothing
        bad_dir = self.tmp / "bad"
        shutil.copytree(back, bad_dir)
        np.save(bad_dir / "vectors.npy", np.ones((1, 4), dtype=np.float32), allow_pickle=False)
        code, bad = self._run_cli(["--state", str(self.state), "--mode", "layers",
                                   "--apply-dir", str(bad_dir)], env)
        self.assertEqual(code, 1)
        self.assertEqual(bad["bad"], 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM search.chunks").fetchone()[0], 0)

        # 4. Unit newly excluded between compute and apply is dropped & never written
        only_staff_excl = _exclusion(excluded=(EXCLUDED, STAFF_BOX))
        out2 = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(fl.config, "settings", return_value=dict(env)), \
                mock.patch("operating.embed.follow_transport._exclusion",
                           return_value=only_staff_excl), \
                contextlib.redirect_stdout(out2):
            code = fl.main(["--state", str(self.state), "--mode", "layers",
                            "--embed-seconds", "0", "--apply-dir", str(back)])
        self.assertEqual(code, 0)
        audiences = {r[0] for r in self.conn.execute("SELECT audience_key FROM search.chunks")}
        self.assertNotIn(f"mail:{STAFF_BOX}", audiences)
        self.assertIn("mc:live/tasks/PRJ-X", audiences)

    def test_corrupt_chunks_or_entities_payload_aborts_before_any_db_mutation(self):
        import gzip
        layer = self._seed_layer("delta-20261124T160000000000.sqlite",
                                 include_excluded_sender=True)
        self._write_pointer([layer])
        ship, back = self.tmp / "ship", self.tmp / "back"
        env = self._base_env()
        self._run_cli(["--state", str(self.state), "--mode", "layers", "--embed-seconds", "30",
                       "--prepare-dir", str(ship)], env)
        self._run_cli(["--state", str(self.state), "--mode", "layers", "--embed-seconds", "30",
                       "--context-dir", str(ship), "--export-dir", str(back)], env)

        # Pre-seed chunks (including an excluded sender that apply would purge and an old
        # MCTask:T-1 version that apply would delete) plus an existing entity.
        old = make_chunk(kind="mc_task", parts=("mc:live/tasks/PRJ-X", "old", "0"),
                         source_handle="mc://live/tasks/T-1@1", object_id="MCTask:T-1",
                         title="T-1", author_role=None, occurred_at=None,
                         header="Source: x", body="Keep me on corrupt export.",
                         linked_ids=[], audience_key="mc:live/tasks/PRJ-X", markings=())
        ex_old = make_chunk(kind="mail", parts=(f"mail:{STAFF_BOX}", "ex", "0"),
                            source_handle=f"graph-mail://{STAFF_BOX}/E-EX@0",
                            object_id="Email:E-EX", title="Ex", author_role=None,
                            occurred_at=None, header="Source: x", body="Old excluded.",
                            linked_ids=[], audience_key=f"mail:{STAFF_BOX}", markings=())
        self.st.ChunkWriter(self.conn).write([old, ex_old])
        before_chunks = self.conn.execute(
            "SELECT chunk_id, object_id, body FROM search.chunks ORDER BY chunk_id").fetchall()
        before_ents = self.conn.execute(
            "SELECT entity_id, object_id, name FROM search.entities ORDER BY entity_id").fetchall()
        self.conn.commit()

        # Corrupt chunks.jsonl.gz (bad gzip bytes and invalid JSON/schema)
        for label, payload in (
            ("bad_gzip", b"not-valid-gzip-bytes"),
            ("bad_json", gzip.compress(b'{"not":"a list"}\n')),
            ("truncated_row", gzip.compress(b'["only-one-field"]\n')),
        ):
            bad_dir = self.tmp / f"bad_chunks_{label}"
            shutil.copytree(back, bad_dir)
            (bad_dir / "chunks.jsonl.gz").write_bytes(payload)
            code, rep = self._run_cli(["--state", str(self.state), "--mode", "layers",
                                       "--apply-dir", str(bad_dir)], env)
            self.assertEqual(code, 1, (label, rep))
            self.assertEqual(rep["bad"], 1, (label, rep))
            self.assertEqual(self.conn.execute(
                "SELECT chunk_id, object_id, body FROM search.chunks ORDER BY chunk_id"
            ).fetchall(), before_chunks, label)
            self.assertEqual(self.conn.execute(
                "SELECT entity_id, object_id, name FROM search.entities ORDER BY entity_id"
            ).fetchall(), before_ents, label)
            self.assertFalse((self.state / "search" / "follow.json").exists(), label)

        # Corrupt entities.jsonl.gz (bad gzip bytes and missing required keys)
        for label, payload in (
            ("bad_gzip", b"not-valid-gzip-bytes"),
            ("bad_json", gzip.compress(b"{broken-json\n")),
            ("missing_row_key", gzip.compress(b'{"new_only":false}\n')),
        ):
            bad_dir = self.tmp / f"bad_entities_{label}"
            shutil.copytree(back, bad_dir)
            (bad_dir / "entities.jsonl.gz").write_bytes(payload)
            code, rep = self._run_cli(["--state", str(self.state), "--mode", "layers",
                                       "--apply-dir", str(bad_dir)], env)
            self.assertEqual(code, 1, (label, rep))
            self.assertEqual(rep["bad"], 1, (label, rep))
            self.assertEqual(self.conn.execute(
                "SELECT chunk_id, object_id, body FROM search.chunks ORDER BY chunk_id"
            ).fetchall(), before_chunks, label)
            self.assertEqual(self.conn.execute(
                "SELECT entity_id, object_id, name FROM search.entities ORDER BY entity_id"
            ).fetchall(), before_ents, label)
            self.assertFalse((self.state / "search" / "follow.json").exists(), label)

    def test_new_chunks_clear_pending_after_when_apply_embeddings_skips_early(self):
        from operating.embed import follow as fl
        l1 = self._seed_layer("delta-20261124T170000000000.sqlite")
        self._write_pointer([l1])
        ship1, back1 = self.tmp / "ship1", self.tmp / "back1"
        env = self._base_env()
        self._run_cli(["--state", str(self.state), "--mode", "layers", "--embed-seconds", "30",
                       "--prepare-dir", str(ship1)], env)
        self._run_cli(["--state", str(self.state), "--mode", "layers", "--embed-seconds", "30",
                       "--context-dir", str(ship1), "--export-dir", str(back1)], env)
        self._run_cli(["--state", str(self.state), "--mode", "layers", "--embed-seconds", "30",
                       "--apply-dir", str(back1)], env)
        cur1 = json.loads((self.state / "search" / "follow.json").read_text())
        self.assertEqual(cur1["pending_after"], 0)

        # Layer 2 arrives; prepare & compute run while delta is idle, then delta starts before apply
        l2 = self.state / "projection" / "delta" / "delta-20261124T180000000000.sqlite"
        fx2 = _Fixture(l2)
        fx2.obj("MCTask:T-2", "MCTask", "mc:live/tasks/PRJ-X", kind="mc_task",
                handle="mc://live/tasks/T-2@1",
                text={"row.description": ("mc://live/tasks/T-2@1",
                                          "Task 2 waits on carton labels delivery.")})
        fx2.done()
        self._write_pointer([l1, l2])
        ship2, back2 = self.tmp / "ship2", self.tmp / "back2"
        self._run_cli(["--state", str(self.state), "--mode", "layers", "--embed-seconds", "30",
                       "--prepare-dir", str(ship2)], env)
        self._run_cli(["--state", str(self.state), "--mode", "layers", "--embed-seconds", "30",
                       "--context-dir", str(ship2), "--export-dir", str(back2)], env)
        with mock.patch.object(fl, "delta_running", return_value=True):
            code, applied2 = self._run_cli([
                "--state", str(self.state), "--mode", "layers", "--embed-seconds", "30",
                "--apply-dir", str(back2)], env)
        self.assertEqual(code, 0, applied2)
        self.assertGreaterEqual(applied2["write"]["inserted"], 1)
        self.assertEqual(applied2["embed"]["skipped"], "delta_running")
        cur2 = json.loads((self.state / "search" / "follow.json").read_text())
        self.assertIsNone(cur2.get("pending_after"))

        # Next run (no new layers) must NOT skip as idle; it prepares, computes, and embeds T-2
        ship3, back3 = self.tmp / "ship3", self.tmp / "back3"
        code, prep3 = self._run_cli(["--state", str(self.state), "--mode", "layers",
                                     "--embed-seconds", "30", "--prepare-dir", str(ship3)], env)
        self.assertEqual(code, 0, prep3)
        self.assertNotIn("skipped", prep3)
        self.assertTrue((ship3 / "manifest.json").is_file())
        self._run_cli(["--state", str(self.state), "--mode", "layers", "--embed-seconds", "30",
                       "--context-dir", str(ship3), "--export-dir", str(back3)], env)
        code, applied3 = self._run_cli(["--state", str(self.state), "--mode", "layers",
                                        "--embed-seconds", "30", "--apply-dir", str(back3)], env)
        self.assertEqual(code, 0, applied3)
        self.assertGreaterEqual(applied3["embed"]["texts_embedded"], 1)
        cur3 = json.loads((self.state / "search" / "follow.json").read_text())
        self.assertEqual(cur3["pending_after"], 0)

    def test_newly_excluded_person_or_contact_skipped_in_apply_even_inside_clear_unit(self):
        from operating.embed import follow as fl
        COLLEAGUE = "0c0c0c0c-0000-4000-8000-0000000000c1"
        base_excl = Exclusion(
            PeopleExclusion([EXCLUDED]),
            directory={"staff@example-plx.com": STAFF,
                       "colleague@example-plx.com": COLLEAGUE},
            internal_domains=("example-plx.com",),
            drive_owners={}, chat_members={},
        )
        later_excl = Exclusion(
            PeopleExclusion([EXCLUDED, COLLEAGUE]),
            directory={"staff@example-plx.com": STAFF,
                       "colleague@example-plx.com": COLLEAGUE},
            internal_domains=("example-plx.com",),
            drive_owners={}, chat_members={},
        )
        layer = self.state / "projection" / "delta" / "delta-20261124T190000000000.sqlite"
        fx = _Fixture(layer)
        # Clear unit bc:co/contacts holding a Contact (email only in object_values, not alias)
        fx.obj("Contact:COL", "Contact", "bc:co/contacts",
               values={"display_name": ("Colleague Person", '["pii"]'),
                       "email": ("colleague@example-plx.com", '["pii"]')})
        fx.obj("Contact:SAFE", "Contact", "bc:co/contacts",
               values={"display_name": ("Safe Buyer", '["pii"]'),
                       "email": ("buyer@customer.example", '["pii"]')})
        # Clear unit mail:STAFF_BOX with participant colleague@example-plx.com
        fx.obj("Email:E-COL", "Email", f"mail:{STAFF_BOX}", kind="email",
               handle=f"graph-mail://{STAFF_BOX}/E-COL@1",
               text={"row.body_text": (f"graph-mail://{STAFF_BOX}/E-COL@1",
                                       "Update for colleague on shipment.")},
               values={"sender": ("staff@example-plx.com", "[]"),
                       "to_addresses": ("['colleague@example-plx.com', 'buyer@customer.example']",
                                        "[]"),
                       "received_at": ("2026-10-03T10:00:00Z", "[]")})
        fx.done()
        self._write_pointer([layer])
        ship, back = self.tmp / "ship", self.tmp / "back"
        env = self._base_env()
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(fl.config, "settings", return_value=dict(env)), \
                mock.patch("operating.embed.follow_transport._exclusion", return_value=base_excl), \
                mock.patch("operating.embed.follow_transport._exclusion_from_context",
                           return_value=base_excl):
            self.assertEqual(fl.main(["--state", str(self.state), "--mode", "layers",
                                      "--embed-seconds", "0", "--prepare-dir", str(ship)]), 0)
            self.assertEqual(fl.main(["--state", str(self.state), "--mode", "layers",
                                      "--embed-seconds", "0", "--context-dir", str(ship),
                                      "--export-dir", str(back)]), 0)

        # Before apply, COLLEAGUE is excluded while bc:co/contacts and mail:STAFF_BOX stay clear
        out = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(fl.config, "settings", return_value=dict(env)), \
                mock.patch("operating.embed.follow_transport._exclusion", return_value=later_excl), \
                contextlib.redirect_stdout(out):
            code = fl.main(["--state", str(self.state), "--mode", "layers",
                            "--embed-seconds", "0", "--apply-dir", str(back)])
        self.assertEqual(code, 0, out.getvalue())
        ents = self.conn.execute(
            "SELECT object_id, name, alias FROM search.entities ORDER BY name").fetchall()
        names = {r[1] for r in ents}
        all_aliases = {a for r in ents for a in r[2]}
        self.assertIn("Safe Buyer", names)
        self.assertIn("Buyer", names)
        self.assertNotIn("Colleague Person", names)
        self.assertNotIn("colleague@example-plx.com", all_aliases)

        # Also verify fail-closed when a person/contact entity carries no decidable identity
        import gzip
        from operating.embed import follow_transport as ft
        self.assertFalse(ft._entity_clear(
            ft.Entity(entity_id="p:noid", object_id="Contact:NOID", type="person",
                      name="Mystery Person", alias=(), normalized="mystery person",
                      audience_key="bc:co/contacts", markings=("pii",)),
            later_excl,
        ))
        self.assertFalse(ft._entity_clear(
            ft.Entity(entity_id="p:ok", object_id="Contact:OK", type="contact",
                      name="Safe Buyer", alias=(), normalized="safe buyer",
                      audience_key="bc:co/contacts", markings=("pii",)),
            Exclusion(PeopleExclusion([])),
            identities=("buyer@customer.example",),
        ))


if __name__ == "__main__":
    unittest.main()
