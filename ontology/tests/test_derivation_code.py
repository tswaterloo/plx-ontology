"""What the derivation code digest covers (``ontology.project.derivation_code``): every
module and data file deriving documents' fields and container members runs, and nothing
an ordinary product change touches."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

from ontology import project

ROOT = Path(__file__).resolve().parents[2]

# Derive both relations of a mail capture in a fresh interpreter and report the repository
# modules it holds and the repository files it opened (an audit hook), beyond source.
_PROBE = r"""
import json, os, sys
root = os.path.realpath(sys.argv[1])
opened = set()

def hook(event, args):
    if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
        path = os.path.realpath(os.fsdecode(args[0]))
        if path.startswith(root + os.sep) and "__pycache__" not in path \
                and not path.endswith(".py"):
            opened.add(os.path.relpath(path, root))
sys.addaudithook(hook)
from ontology.project import MailCaptures
packages = {p for p in os.listdir(root) if os.path.isfile(os.path.join(root, p, "__init__.py"))}
ours = lambda: {m for m in sys.modules if m.split(".")[0] in packages}
with MailCaptures(sys.argv[2]) as mail:
    source = ours()   # opening the source: its records are fingerprinted, not derived
    records = mail.records(["documentFields", "containerMembers"])
    counts = {name: len(list(records[name])) for name in ("documentFields",
                                                          "containerMembers")}
print(json.dumps({"counts": counts, "opened": sorted(opened), "source": sorted(source),
                  "modules": sorted(ours() - source)}))
"""


class DerivationCodeTests(unittest.TestCase):
    def test_the_digest_covers_every_module_and_file_a_derivation_runs(self):
        """Documents typed from PDFs, a zip's members and a forwarded message's: every
        repository module loaded and every repository data file opened while deriving
        ``documentFields`` and ``containerMembers`` is one the digest covers."""
        os.environ.pop("PLX_WORKBENCH_API_KEY", None)
        from integrations import graph_read
        from integrations.capture_mail import MailCapture, MailCaptureStore
        from integrations.graph_read import GraphMailReader
        from integrations.live_source_guard import BoundedBackoff
        from integrations.tests.test_capture_mail import (BOX_A, CAPTURE, ENV, SCOPE, Clock,
                                                           FakeGraph, capture_audience,
                                                           file_attachment)
        from ontology.tests.test_document_fields import CONFIRMATION, QUOTE, SLIP, pdf

        from email.message import EmailMessage

        forwarded = EmailMessage()
        forwarded["From"], forwarded["To"] = "a@example.com", "b@example.com"
        forwarded["Subject"] = "Fwd PO100235"
        forwarded.set_content("Proforma for PO100235 attached.")
        forwarded.add_attachment(pdf(CONFIRMATION), maintype="application", subtype="pdf",
                                 filename="proforma.pdf")
        message = forwarded.as_bytes()
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as packed:
            packed.writestr("slip.pdf", pdf(SLIP))
            packed.writestr("notes.txt", "Quote FQ-2207 for PO100234")
        graph = FakeGraph()
        graph.put(BOX_A, "MSG-1", "CK-1", subject="Documents", body="For PO100234.",
                  attachments=[file_attachment("ATT-1", pdf(QUOTE), "quote.pdf"),
                               file_attachment("ATT-2", pdf(CONFIRMATION), "proforma.pdf"),
                               {**file_attachment("ATT-3", archive.getvalue(), "docs.zip"),
                                "contentType": "application/zip"},
                               {**file_attachment("ATT-4", message, "fwd.eml"),
                                "contentType": "message/rfc822"}])
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        capture = MailCaptureStore(root, clock=Clock())
        self.addCleanup(capture.close)
        api, _ = graph_read.build_graph(ENV, sender=graph, retry=BoundedBackoff(
            sleep=lambda s: None, jitter=lambda: 0.0))
        run = MailCapture(capture, GraphMailReader(api, capture_audience()), CAPTURE,
                          sleep=lambda s: None).run(SCOPE)
        self.assertTrue(run["complete"], run)

        env = {k: v for k, v in os.environ.items() if k != "PLX_WORKBENCH_API_KEY"}
        env["PYTHONPATH"] = str(ROOT)
        out = subprocess.run([sys.executable, "-c", _PROBE, str(ROOT), str(root)],
                             capture_output=True, text=True, timeout=300, env=env, cwd=ROOT)
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        found = json.loads(out.stdout.splitlines()[-1])
        self.assertGreaterEqual(found["counts"]["documentFields"], 3, found["counts"])
        self.assertGreaterEqual(found["counts"]["containerMembers"], 3, found["counts"])
        covered = project.derivation_modules()
        self.assertIn("operating.use_case_parts", found["modules"])   # the zip was walked
        self.assertEqual(sorted(set(found["modules"]) - set(covered)), [])
        # The source opened reads its capture file (through the mirror's scope views):
        # input records, fingerprinted as such.
        self.assertLessEqual(set(found["source"]) - set(covered),
                             {"integrations.source_mirror", "integrations.http",
                              "integrations.workbench_read_api"})
        self.assertTrue(found["opened"])
        self.assertEqual(sorted(set(found["opened"]) - set(project._DERIVATION_DATA)), [])

    def test_an_unrelated_change_keeps_the_digest_a_derivation_change_moves_it(self):
        """In a copy of the repository: an Ask, console or backend module or a domain
        schema file changed leaves the digest as it was; the container walk (imported in
        a function only), the field parser, a module it imports or its profiles move it."""
        copy = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, copy, True)
        for package in ("ontology", "integrations", "operating", "operator_api"):
            shutil.copytree(ROOT / package, copy / package,
                            ignore=shutil.ignore_patterns("tests", "__pycache__", "*.sqlite"))
        base = project._derivation_digest(copy)
        self.assertEqual(base, project.derivation_code())

        def changed(relative: str) -> str:
            path = copy / relative
            original = path.read_bytes()
            path.write_bytes(original + (b"\n# changed\n" if relative.endswith(".py")
                                         else b"\n"))
            try:
                return project._derivation_digest(copy)
            finally:
                path.write_bytes(original)
        for unrelated in ("operating/fast_answer.py", "operating/ontology_tools_traverse.py",
                          "operating/host.py", "operating/build_steps.py",
                          "operator_api/workspace/__init__.py", "ontology/project_incremental.py",
                          "integrations/capture_mail.py",
                          "ontology/schema/domains/purchasing.json"):
            self.assertEqual(changed(unrelated), base, unrelated)
        for derived in ("ontology/document_fields.py", "operating/use_case_parts.py",
                        "operating/doc_metadata_taxonomy.py", "ontology/units.py",
                        "integrations/document_understanding.py",
                        "ontology/schema/document_fields.json",
                        "ontology/schema/identifiers.json"):
            self.assertNotEqual(changed(derived), base, derived)
        # An import added to a derivation module is followed (fail closed).
        target = copy / "ontology" / "units.py"
        target.write_text(target.read_text() + "\ndef _later():\n"
                          "    from operating import fast_answer\n")
        self.assertIn("operating.fast_answer", project.derivation_modules(copy))


if __name__ == "__main__":
    unittest.main()
