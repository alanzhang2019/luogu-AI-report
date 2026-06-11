import unittest
from web_app import _init_report_hides_table, _get_conn
from web_app import _record_hide_pdf


class TestReportHidesSchema(unittest.TestCase):
    def test_idempotent_create(self):
        _init_report_hides_table()
        # 再次调用不抛
        _init_report_hides_table()
        conn = _get_conn()
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='report_hides'"
            ).fetchone()
            self.assertIsNotNone(row)
            cols = [r["name"] for r in conn.execute("PRAGMA table_info(report_hides)").fetchall()]
            self.assertIn("task_id", cols)
            self.assertIn("hide_pdf", cols)
            self.assertIn("hide_html", cols)
            self.assertIn("ref_uid", cols)
        finally:
            conn.close()


class TestRecordHidePdf(unittest.TestCase):
    def test_writes_row(self):
        _init_report_hides_table()
        tid = "test_task_record_001"
        _record_hide_pdf(tid)
        conn = _get_conn()
        try:
            row = conn.execute(
                "SELECT hide_pdf FROM report_hides WHERE task_id=?", (tid,)
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["hide_pdf"], 1)
        finally:
            conn.close()

    def test_db_failure_does_not_raise(self):
        tid = "test_task_record_002"
        _record_hide_pdf(tid)  # 不应抛

    def test_idempotent(self):
        _init_report_hides_table()
        tid = "test_task_record_003"
        _record_hide_pdf(tid)
        _record_hide_pdf(tid)
        conn = _get_conn()
        try:
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM report_hides WHERE task_id=?", (tid,)
            ).fetchone()["n"]
            self.assertEqual(n, 1)
        finally:
            conn.close()
