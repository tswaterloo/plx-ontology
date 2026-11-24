"""The Spark document reader refuses no size: a file over the box extractor's 20 MB or 200
pages goes over the ssh pipe (in frames) and comes back read, page by page, whole."""
import io
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

from integrations import document_understanding as du
from integrations import spark_doc_reader as reader

ROOT = Path(__file__).resolve().parents[2]


def importable(name: str) -> bool:
    try:
        __import__(name)
        return True
    except ImportError:
        return False


def worker() -> reader.Worker:
    return reader.Worker([sys.executable, "-m", "integrations.spark_doc_worker"])


class SparkDocReaderSizes(unittest.TestCase):
    def setUp(self):
        self._cwd = os.getcwd()
        os.chdir(ROOT)
        self.addCleanup(os.chdir, self._cwd)
        env = mock.patch.dict(os.environ, {"PYTHONPATH": str(ROOT)})
        env.start()
        self.addCleanup(env.stop)

    def test_a_text_file_past_20_mb_is_read_whole_over_several_frames(self):
        line = "lot 4471 expires 2027-03-01 on the supplier's certificate\n"
        body = (line * (du.MAX_BYTES // len(line) + 1000)).encode()
        self.assertGreater(len(body), du.MAX_BYTES)
        with mock.patch.object(reader, "PART", 7_000_000):   # four frames
            w = worker()
            try:
                got = w.read({"name": "ledger.txt", "content_type": "text/plain"}, body)
            finally:
                w.close()
        self.assertEqual(got["status"], "extracted")
        self.assertEqual(len(got["text"]), len(body))
        self.assertTrue(got["text"].endswith(line))

    @unittest.skipUnless(importable("pypdf") and importable("reportlab"), "needs pypdf, reportlab")
    def test_a_pdf_past_the_page_bound_is_read_to_its_last_page(self):
        from reportlab.pdfgen import canvas
        pages = du.MAX_PAGES + 7
        buf = io.BytesIO()
        pdf = canvas.Canvas(buf)
        for n in range(1, pages + 1):
            pdf.drawString(72, 720, f"Page marker M{n:03d}")
            pdf.showPage()
        pdf.save()
        w = worker()
        try:
            got = w.read({"name": "long.pdf", "content_type": "application/pdf"}, buf.getvalue())
        finally:
            w.close()
        self.assertEqual(got["status"], "extracted")
        self.assertIn(f"M{pages:03d}", got["text"])
        self.assertIn("M001", got["text"])


if __name__ == "__main__":
    unittest.main()
