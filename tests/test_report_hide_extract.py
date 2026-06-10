import unittest
from web_app import _extract_ai_summary


SAMPLE_REPORT = """# 测试报告

## 一、基础信息
...

### （一）AI 核心解读

这位选手展现出扎实的基础算法功底，在动态规划和字符串处理方面表现尤为突出。建议在保持现有优势的同时，重点加强图论和数据结构相关题目的练习，特别是在复杂场景的应用方面。

## 二、详细分析
...
"""


class TestExtractAiSummary(unittest.TestCase):
    def test_empty_report(self):
        self.assertEqual(_extract_ai_summary(""), "")

    def test_extracts_core_interpretation(self):
        out = _extract_ai_summary(SAMPLE_REPORT)
        self.assertIn("基础算法", out)
        self.assertIn("动态规划", out)

    def test_truncates_to_200_chars(self):
        long = "### （一）AI 核心解读\n\n" + "测试" * 500 + "\n\n## 二、"
        out = _extract_ai_summary(long)
        self.assertLessEqual(len(out), 200)
        self.assertGreater(len(out), 0)

    def test_returns_empty_when_section_missing(self):
        out = _extract_ai_summary("""# 报告

## 一、基础信息
选手是小学生。
""")
        self.assertEqual(out, "")
