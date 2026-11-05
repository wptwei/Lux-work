import tempfile
import unittest
from pathlib import Path

from core.word_pdf import WordPdfError, _safe_stem, validate_word_file


class WordPdfTests(unittest.TestCase):
    def test_safe_stem_removes_windows_reserved_characters(self):
        self.assertEqual(_safe_stem('周报<第1期>:A/B'), "周报_第1期__A_B")

    def test_validate_accepts_docx(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "demo.docx"
            path.write_bytes(b"fake-docx")
            self.assertEqual(validate_word_file(path), path.resolve())

    def test_validate_rejects_non_word_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "demo.txt"
            path.write_text("x", encoding="utf-8")
            with self.assertRaises(WordPdfError):
                validate_word_file(path)


if __name__ == "__main__":
    unittest.main()
