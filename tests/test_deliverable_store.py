import tempfile
import unittest
from pathlib import Path

from core.deliverable_store import DeliverableStore


class DeliverableStoreTests(unittest.TestCase):
    def test_versions_increment_per_user_and_logical_name(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            file_path = root / "report.pdf"
            file_path.write_bytes(b"%PDF-test")
            store = DeliverableStore(root / "deliverables.db")

            first = store.register(file_path, kind="PDF", created_by="zhangsan", logical_name="安保周报")
            second = store.register(file_path, kind="PDF", created_by="zhangsan", logical_name="安保周报")
            other = store.register(file_path, kind="PDF", created_by="lisi", logical_name="安保周报")

            self.assertEqual(1, first["version"])
            self.assertEqual(2, second["version"])
            self.assertEqual(1, other["version"])
            self.assertEqual(2, len(store.list_for_user("zhangsan")))
            self.assertEqual(1, len(store.list_for_user("lisi")))


if __name__ == "__main__":
    unittest.main()
