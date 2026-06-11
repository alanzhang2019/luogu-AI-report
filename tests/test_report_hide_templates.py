import unittest
from web_app import REPORT_PREVIEW_HTML


class TestReportPreviewTemplate(unittest.TestCase):
    def test_template_exists(self):
        self.assertIsInstance(REPORT_PREVIEW_HTML, str)
        self.assertGreater(len(REPORT_PREVIEW_HTML), 1000)

    def test_contains_10_zones(self):
        for k in ["sticky", "AI 评测分", "AI 一句话总结", "6 维能力",
                  "错题本", "训练建议", "看完整", "生成你的报告",
                  "已为", "noindex"]:
            self.assertIn(k, REPORT_PREVIEW_HTML, f"missing: {k}")

    def test_renders_with_empty_data(self):
        from jinja2 import Environment
        env = Environment()
        t = env.from_string(REPORT_PREVIEW_HTML)
        html = t.render(
            luogu_uid="e279a542",
            student_name="UID e279a542",
            achievements={"six_dim": {}, "ai_score_thousand": None,
                          "ai_score_label": "—", "mistakes": []},
            ai_summary="", suggestions=[], ref=None, has_report=False,
        )
        self.assertIn("e279a542", html)
        self.assertIn("暂未生成报告", html)

    def test_renders_with_full_data(self):
        from jinja2 import Environment
        env = Environment()
        t = env.from_string(REPORT_PREVIEW_HTML)
        html = t.render(
            luogu_uid="e279a542",
            student_name="UID e279a542",
            achievements={
                "six_dim": {"基础算法": 72, "数据结构": 33, "图论": 33,
                            "动态规划": 39, "字符串": 47, "数学": 40},
                "ai_score_thousand": 440,
                "ai_score_label": "🟡 基础",
                "mistakes": [{"idx": 1, "problem_id": "P11229", "title": "小木棍",
                              "source": "CSP-J 2024", "summary": "贪心+构造",
                              "bottleneck": "枚举超时"}],
            },
            ai_summary="这位选手在基础算法和动态规划方面表现突出。",
            suggestions=["补 DP 专项", "贪心可突破", "GESP 7 级"],
            ref="abc123", has_report=True,
        )
        for k in ["440", "基础算法", "P11229", "贪心+构造",
                  "补 DP 专项", "ref=abc123", "看完整", "生成你的报告"]:
            self.assertIn(k, html, f"missing: {k}")


class TestStudentMePdfGray(unittest.TestCase):
    def test_contains_pdf_disabled_hint(self):
        from web_app import STUDENT_ME_HTML
        for k in ["PDF 暂未开放", "请用海报分享", "🔒"]:
            self.assertIn(k, STUDENT_ME_HTML, f"missing: {k}")
