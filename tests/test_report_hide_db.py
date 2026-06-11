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


from web_app import _check_file_visibility


class TestCheckFileVisibility(unittest.TestCase):
    def test_pdf_hidden_returns_false(self):
        _init_report_hides_table()
        tid = "test_task_vis_001"
        _record_hide_pdf(tid)
        visible, reason = _check_file_visibility(f"reports/{tid}/report.pdf")
        self.assertFalse(visible)
        self.assertIn("PDF", reason)

    def test_html_visible_when_only_pdf_hidden(self):
        _init_report_hides_table()
        tid = "test_task_vis_002"
        _record_hide_pdf(tid)
        visible, reason = _check_file_visibility(f"reports/{tid}/report.html")
        self.assertTrue(visible)
        self.assertEqual(reason, "")

    def test_md_always_visible(self):
        visible, reason = _check_file_visibility("reports/any/report.md")
        self.assertTrue(visible)
        self.assertEqual(reason, "")

    def test_unknown_path_visible(self):
        visible, reason = _check_file_visibility("static/logo.png")
        self.assertTrue(visible)

    def test_db_failure_fail_open(self):
        # 删表后调用应返回 True (fail-open)
        conn = _get_conn()
        try:
            conn.execute("DROP TABLE IF EXISTS report_hides")
            conn.commit()
        finally:
            conn.close()
        visible, reason = _check_file_visibility("reports/any/report.pdf")
        self.assertTrue(visible)  # fail-open
