import unittest
from unittest.mock import patch

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


class TestShareCardQRUrl(unittest.TestCase):
    def test_qr_url_points_to_r_route(self):
        c = app.test_client()
        with patch("web_app._render_share_card_png", return_value=b"\x89PNG\r\n\x1a\n") as mock_render:
            # 用任意已注册 uid；mock 掉数据源 + 渲染，只关心传入的 qr_url
            with patch("web_app._build_share_card_data", return_value={"luogu_uid": "e279a542"}):
                r = c.get("/me/e279a542/share-card.png")
                self.assertEqual(r.status_code, 200)
                self.assertEqual(mock_render.call_count, 1)
                args, kwargs = mock_render.call_args
                qr_url = args[1] if len(args) > 1 else kwargs.get("qr_url", "")
                self.assertIn("/r/e279a542", qr_url)


class TestRefCookie(unittest.TestCase):
    def setUp(self):
        self.c = app.test_client()

    def test_ref_query_writes_cookie(self):
        r = self.c.get("/?ref=abc123", follow_redirects=False)
        self.assertIn("ref_uid", r.headers.get("Set-Cookie", ""))
        import re as _re
        m = _re.search(r"ref_uid=([^;]+)", r.headers.get("Set-Cookie", ""))
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "abc123")

    def test_ref_truncated_to_32(self):
        long_ref = "a" * 100
        r = self.c.get(f"/?ref={long_ref}", follow_redirects=False)
        import re as _re
        m = _re.search(r"ref_uid=([^;]+)", r.headers.get("Set-Cookie", ""))
        self.assertIsNotNone(m)
        self.assertEqual(len(m.group(1)), 32)

    def test_ref_sanitized(self):
        r = self.c.get("/?ref=evil<script>", follow_redirects=False)
        import re as _re
        m = _re.search(r"ref_uid=([^;]+)", r.headers.get("Set-Cookie", ""))
        self.assertIsNotNone(m)
        self.assertNotIn("<", m.group(1))
