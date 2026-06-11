import unittest
from web_app import app


class TestServeReportVisibility(unittest.TestCase):
    def setUp(self):
        self.c = app.test_client()

    def test_pdf_returns_403_when_hidden(self):
        from web_app import _init_report_hides_table, _record_hide_pdf
        _init_report_hides_table()
        _record_hide_pdf("test_task_hide_001")
        r = self.c.get("/reports/test_task_hide_001/report.pdf")
        self.assertEqual(r.status_code, 403)
        body = r.data.decode("utf-8", errors="replace")
        self.assertIn("PDF", body)

    def test_html_returns_200_when_pdf_hidden(self):
        from web_app import _init_report_hides_table, _record_hide_pdf
        _init_report_hides_table()
        _record_hide_pdf("test_task_hide_002")
        r = self.c.get("/reports/test_task_hide_002/report.html")
        self.assertNotEqual(r.status_code, 403)


class TestReportPreviewRoute(unittest.TestCase):
    def setUp(self):
        self.c = app.test_client()

    def test_existing_report_returns_200(self):
        r = self.c.get("/r/e279a542")
        self.assertEqual(r.status_code, 200)
        html = r.data.decode("utf-8", errors="replace")
        for k in ["AI 评测分", "440", "P11229", "生成你的报告", "看完整"]:
            self.assertIn(k, html, f"missing: {k}")

    def test_no_report_returns_200_empty(self):
        r = self.c.get("/r/9999999")
        self.assertEqual(r.status_code, 200)
        html = r.data.decode("utf-8", errors="replace")
        self.assertIn("暂未生成报告", html)

    def test_ref_param_sanitized(self):
        r = self.c.get("/r/e279a542?ref=evil<script>")
        self.assertEqual(r.status_code, 200)
        html = r.data.decode("utf-8", errors="replace")
        self.assertNotIn("<script>", html)
