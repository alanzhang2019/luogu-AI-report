import unittest
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

from pyLuogu.errors import AuthenticationError
from pyLuogu.types import ProblemSummary

from behavior_analyzer import compute_six_dimension_scores
from examples.export_for_ai import RECORD_LIST_PAGES_TO_TRY, _pick_record_for_problem, _summarize
from luogu_evaluator import (
    build_html_and_pdf,
    build_detail_fetch_overview,
    build_trusted_data_summary_md,
    fetch_behavior_analysis,
    repair_behavior_analysis_from_items,
    render_star_rating_html,
    split_practice_problems,
    summarize_detail_fetch_stats,
    summarize_average_difficulty,
)


class DummyPractice:
    def __init__(self, data):
        self.problems = []
        self.data = data


class TestEvaluatorPracticeFallback(unittest.TestCase):
    def test_summarize_separates_algorithm_tags_from_all_tags(self):
        problems = [
            ProblemSummary(
                {
                    "pid": "P1012",
                    "title": "拼数",
                    "difficulty": 3,
                    "type": "P",
                    "submitted": True,
                    "accepted": True,
                    "tags": [2, 83, 113],
                    "totalSubmit": 1,
                    "totalAccepted": 1,
                    "flag": 0,
                    "fullScore": 100,
                }
            )
        ]
        tag_by_id = {
            2: {"id": 2, "name": "字符串", "type": 2, "parent": None},
            83: {"id": 83, "name": "NOIP 提高组", "type": 3, "parent": None},
            113: {"id": 113, "name": "排序", "type": 2, "parent": 110},
        }

        summary = _summarize(problems, tag_by_id)

        self.assertEqual([item["name"] for item in summary["top_tags"]], ["字符串", "NOIP 提高组", "排序"])
        self.assertEqual([item["name"] for item in summary["top_algorithm_tags"]], ["字符串", "排序"])

    def test_six_dimension_scores_prefer_algorithm_tags(self):
        scores = compute_six_dimension_scores(
            {
                "solved_count": 0,
                "summary": {
                    "difficulty_histogram": {},
                    "top_tags": [{"name": "1998", "count": 50}],
                    "top_algorithm_tags": [{"name": "字符串", "count": 5}],
                },
            },
            {},
        )

        self.assertGreaterEqual(scores["字符串"], 45)

    def test_summarize_adds_level_experience_from_origin_and_difficulty(self):
        problems = [
            ProblemSummary(
                {
                    "pid": "P3195",
                    "title": "玩具装箱",
                    "difficulty": 6,
                    "type": "P",
                    "submitted": True,
                    "accepted": True,
                    "tags": [3, 48, 150, 254],
                    "totalSubmit": 1,
                    "totalAccepted": 1,
                    "flag": 0,
                    "fullScore": 100,
                }
            )
        ]
        tag_by_id = {
            3: {"id": 3, "name": "动态规划 DP", "type": 2, "parent": None},
            48: {"id": 48, "name": "各省省选", "type": 3, "parent": 426},
            150: {"id": 150, "name": "斜率优化", "type": 2, "parent": 146},
            254: {"id": 254, "name": "前缀和", "type": 2, "parent": 44},
        }

        summary = _summarize(problems, tag_by_id)
        level_exp = summary["level_experience"]

        self.assertEqual(level_exp["provincial"]["solved"], 1)
        self.assertEqual(level_exp["provincial"]["by_origin"], 1)
        self.assertEqual(level_exp["provincial"]["by_difficulty"], 1)
        self.assertEqual(level_exp["noi"]["solved"], 0)

    def test_trusted_summary_hides_level_experience_table_when_charts_exist(self):
        export_data = {
            "student_info": {"eval_time": "2026-06-03 12:00"},
            "summary": {
                "difficulty_histogram": {"6": 1},
                "level_experience": {
                    "csp_j": {"solved": 1, "by_origin": 0, "by_difficulty": 1},
                    "csp_s": {"solved": 1, "by_origin": 1, "by_difficulty": 1},
                    "provincial": {"solved": 1, "by_origin": 1, "by_difficulty": 1},
                    "noi": {"solved": 0, "by_origin": 0, "by_difficulty": 0},
                },
            },
            "behavior_analysis": {"error": "未获取到有效提交记录"},
            "syllabus_evaluation": {
                "csp_j": {"stats": {"total": 28, "空白": 0}, "coverage": 100},
                "csp_s": {"stats": {"total": 49, "空白": 49}, "coverage": 0},
                "provincial": {"stats": {"total": 10, "空白": 10}, "coverage": 0},
                "noi": {"stats": {"total": 43, "空白": 43}, "coverage": 0},
            },
        }

        markdown = build_trusted_data_summary_md(export_data)

        self.assertIn("知识点覆盖统计表（按算法标签）", markdown)
        self.assertNotIn("题目级别经历表", markdown)
        self.assertNotIn("来源标签命中", markdown)
        self.assertNotIn("难度命中", markdown)

    def test_average_difficulty_uses_luogu_label_and_color(self):
        info = summarize_average_difficulty({"5": 2, "6": 1})

        self.assertEqual(info["label"], "提高+/省选-")
        self.assertEqual(info["color"], "#3498DB")
        self.assertAlmostEqual(float(info["average_value"]), 5.3, places=1)

    def test_trusted_data_summary_hides_diagnosis_date_and_unknown_difficulty(self):
        markdown = build_trusted_data_summary_md(
            {
                "student_info": {"eval_time": "2026-06-03 23:06"},
                "summary": {"difficulty_histogram": {"0": 10, "1": 5, "2": 1}},
                "behavior_analysis": {"error": "nope"},
                "detail_fetch_stats": {},
                "syllabus_evaluation": {},
            }
        )
        self.assertIn("报告生成时间", markdown)
        self.assertNotIn("诊断日期", markdown)
        self.assertNotIn("暂无评定", markdown)
        self.assertNotIn("提交详情抓取统计", markdown)
        self.assertNotIn("题目级别经历表", markdown)
        self.assertNotIn("图表中文字体", markdown)
        self.assertNotIn("提交时间数据", markdown)
        self.assertNotIn("下方本节为程序直出的真实统计", markdown)

    def test_render_star_rating_html_uses_capsule_style(self):
        html = render_star_rating_html("⭐⭐⭐☆☆")

        self.assertIn("display:inline-flex", html)
        self.assertIn("background:#111827", html)
        self.assertIn("3/5", html)
        self.assertIn("color:#F5C542", html)
        self.assertIn("color:#94A3B8", html)

    def test_build_html_uses_relative_chart_paths_for_web_reports(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            assets_dir = root / "assets"
            assets_dir.mkdir(parents=True, exist_ok=True)
            chart_file = assets_dir / "difficulty_histogram.png"
            chart_file.write_bytes(b"fake-png")
            html_path = root / "report.html"
            pdf_path = root / "report.pdf"

            build_html_and_pdf(
                report_md="# 测试报告",
                export_data={
                    "student_info": {
                        "name": "测试",
                        "school": "学校",
                        "grade": "年级",
                        "eval_time": "2026-06-04 16:00",
                    },
                    "solved_count": 1,
                    "failed_count": 0,
                    "summary": {"difficulty_histogram": {"2": 1}},
                },
                html_path=str(html_path),
                pdf_path=str(pdf_path),
                chart_paths={"difficulty": str(chart_file)},
                export_pdf=False,
            )

            html = html_path.read_text(encoding="utf-8")
            self.assertIn('src="assets/difficulty_histogram.png"', html)
            self.assertNotIn("file:///", html)

    def test_generate_chart_images_applies_style_before_font_config(self):
        from luogu_evaluator import generate_chart_images

        calls = []
        with patch("luogu_evaluator.plt.style.use", side_effect=lambda style: calls.append("style")), \
             patch("luogu_evaluator.configure_matplotlib_font", side_effect=lambda: calls.append("font")), \
             patch("luogu_evaluator.repair_behavior_analysis_from_items", side_effect=lambda data: data):
            generate_chart_images(
                {
                    "summary": {"difficulty_histogram": {"2": 1}},
                    "solved_count": 1,
                    "failed_count": 0,
                    "behavior_analysis": {},
                },
                tempfile.mkdtemp(),
            )

        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(calls[:2], ["style", "font"])

    def test_pick_record_for_problem_keeps_summary_when_detail_decode_fails(self):
        class DummyRecord:
            def __init__(self):
                self.id = 123

            def to_json(self):
                return {
                    "id": 123,
                    "submitTime": 1234567890,
                    "status": 12,
                    "problem": {"pid": "P1000", "title": "A+B Problem"},
                }

        class DummyRecordList:
            records = [DummyRecord()]

        class DummyLuogu:
            def get_record_list(self, **kwargs):
                return DummyRecordList()

            def get_record(self, rid):
                raise ValueError("Failed to decode JSON response")

        record = _pick_record_for_problem(DummyLuogu(), uid=1, pid="P1000", max_records_to_try=2)

        self.assertEqual(record["id"], 123)
        self.assertEqual(record["submitTime"], 1234567890)
        self.assertIn("_detail_error", record)
        self.assertIsNone(record["sourceCode"])

    def test_pick_record_for_problem_skips_detail_when_source_not_required(self):
        class DummyRecord:
            def __init__(self):
                self.id = 456

            def to_json(self):
                return {
                    "id": 456,
                    "submitTime": 1234567891,
                    "status": 12,
                    "problem": {"pid": "P1001", "title": "B Problem"},
                }

        class DummyRecordList:
            records = [DummyRecord()]

        class DummyLuogu:
            def __init__(self):
                self.detail_calls = 0

            def get_record_list(self, **kwargs):
                return DummyRecordList()

            def get_record(self, rid):
                self.detail_calls += 1
                raise AssertionError("detail fetch should not be called")

        api = DummyLuogu()
        record = _pick_record_for_problem(api, uid=1, pid="P1001", max_records_to_try=2, require_source_code=False)

        self.assertEqual(record["id"], 456)
        self.assertEqual(api.detail_calls, 0)
        self.assertIsNone(record["sourceCode"])

    def test_pick_record_for_problem_stops_future_detail_fetch_after_blocking_error(self):
        class DummyRecord:
            def __init__(self, rid):
                self.id = rid

            def to_json(self):
                return {
                    "id": self.id,
                    "submitTime": 1234567000 + self.id,
                    "status": 12,
                    "problem": {"pid": f"P{self.id}", "title": "Sample"},
                }

        class DummyRecordList:
            def __init__(self, rid):
                self.records = [DummyRecord(rid)]

        class DummyLuogu:
            def __init__(self):
                self.detail_calls = 0

            def get_record_list(self, **kwargs):
                pid = kwargs["pid"]
                rid = 1 if pid == "P1" else 2
                return DummyRecordList(rid)

            def get_record(self, rid):
                self.detail_calls += 1
                raise AuthenticationError("Need Login")

        api = DummyLuogu()
        state = {}

        first = _pick_record_for_problem(api, uid=1, pid="P1", max_records_to_try=2, detail_fetch_state=state)
        second = _pick_record_for_problem(api, uid=1, pid="P2", max_records_to_try=2, detail_fetch_state=state)

        self.assertTrue(state.get("stop_detail_fetch"))
        self.assertEqual(api.detail_calls, 1)
        self.assertIn("_detail_error", first)
        self.assertIn("_detail_skipped", second)
        self.assertIsNone(second["sourceCode"])

    def test_pick_record_for_problem_keeps_list_level_skip_after_blocking_list_error(self):
        class DummyLuogu:
            def __init__(self):
                self.list_calls = 0

            def get_record_list(self, **kwargs):
                self.list_calls += 1
                raise AuthenticationError("Need Login")

        api = DummyLuogu()
        state = {}

        first = _pick_record_for_problem(api, uid=1, pid="P1", max_records_to_try=2, detail_fetch_state=state)
        second = _pick_record_for_problem(api, uid=1, pid="P2", max_records_to_try=2, detail_fetch_state=state)

        self.assertTrue(state.get("stop_detail_fetch"))
        self.assertEqual(api.list_calls, RECORD_LIST_PAGES_TO_TRY)
        self.assertTrue(first.get("_record_list_unavailable"))
        self.assertIn("_detail_skipped", first)
        self.assertTrue(second.get("_record_list_unavailable"))
        self.assertIn("_detail_skipped", second)
        self.assertIsNone(second["sourceCode"])

    def test_pick_record_for_problem_recovers_after_transient_network_error(self):
        class DummyDetail:
            def to_json(self):
                return {
                    "id": 101,
                    "submitTime": 1234567999,
                    "status": 12,
                    "problem": {"pid": "P1002", "title": "Recover"},
                }

            @property
            def sourceCode(self):
                return "print('ok')"

        class DummyRecord:
            id = 101

            def to_json(self):
                return {
                    "id": 101,
                    "submitTime": 1234567999,
                    "status": 12,
                    "problem": {"pid": "P1002", "title": "Recover"},
                }

        class DummyRecordList:
            records = [DummyRecord()]

        class DummyDetailResp:
            record = DummyDetail()

        class DummyLuogu:
            def __init__(self):
                self.list_calls = 0
                self.detail_calls = 0

            def get_record_list(self, **kwargs):
                self.list_calls += 1
                if self.list_calls <= 2:
                    raise RequestError("Request error")
                return DummyRecordList()

            def get_record(self, rid):
                self.detail_calls += 1
                if self.detail_calls <= 2:
                    raise RequestError("Failed to send request after 5 attempts")
                return DummyDetailResp()

        api = DummyLuogu()
        record = _pick_record_for_problem(api, uid=1, pid="P1002", max_records_to_try=2)

        self.assertEqual(record["sourceCode"], "print('ok')")
        self.assertGreaterEqual(api.list_calls, 3)
        self.assertGreaterEqual(api.detail_calls, 3)

    def test_repair_behavior_analysis_from_items_uses_valid_fallback_records(self):
        export_data = {
            "passed_items": [
                {
                    "problem": {"pid": "P1000", "title": "A+B Problem"},
                    "record": {
                        "id": 1,
                        "status": 12,
                        "submitTime": 1234567890,
                        "problem": {"pid": "P1000", "title": "A+B Problem"},
                    },
                }
            ],
            "failed_items": [],
            "behavior_analysis": {"error": "Failed to decode JSON response"},
        }

        repaired = repair_behavior_analysis_from_items(export_data)

        self.assertIn("personality_scores", repaired)
        self.assertEqual(repaired["_source"], "record_detail_fallback_repaired")
        self.assertIn("decode JSON", repaired["_warning"])

    def test_summarize_detail_fetch_stats_counts_requested_summary_and_blocking(self):
        passed_items = [
            {"problem": {"pid": "P1"}, "record": {"submitTime": 1, "sourceCode": "code", "_detail_requested": True}},
            {"problem": {"pid": "P2"}, "record": {"submitTime": 2, "sourceCode": None, "_detail_requested": True, "_detail_error": "Need Login"}},
        ]
        failed_items = [
            {"problem": {"pid": "P3"}, "record": {"submitTime": 3, "sourceCode": None, "_detail_requested": False}},
            {"problem": {"pid": "P4"}, "record": {"submitTime": 4, "sourceCode": None, "_detail_requested": True, "_detail_skipped": "Need Login"}},
            {"problem": {"pid": "P5"}, "record": {"error": "Failed to decode JSON response"}},
        ]

        stats = summarize_detail_fetch_stats(passed_items, failed_items, {"last_detail_error": "Need Login"})

        self.assertEqual(stats["total_items"], 5)
        self.assertEqual(stats["source_code_success"], 1)
        self.assertEqual(stats["summary_only"], 3)
        self.assertEqual(stats["detail_requested"], 3)
        self.assertEqual(stats["detail_skipped"], 1)
        self.assertEqual(stats["detail_errors"], 1)
        self.assertEqual(stats["pure_error_records"], 1)
        self.assertEqual(stats["blocker_reason"], "Need Login")

    def test_trusted_summary_hides_detail_fetch_stats_when_overview_card_exists(self):
        export_data = {
            "student_info": {"eval_time": "2026-06-03 12:00"},
            "summary": {
                "difficulty_histogram": {"1": 2},
                "level_experience": {},
            },
            "detail_fetch_stats": {
                "total_items": 8,
                "source_code_success": 3,
                "summary_only": 5,
                "detail_requested": 4,
                "detail_skipped": 1,
                "detail_errors": 1,
                "pure_error_records": 0,
                "blocker_reason": "Need Login",
            },
            "behavior_analysis": {"error": "未获取到有效提交记录"},
            "syllabus_evaluation": {},
        }

        markdown = build_trusted_data_summary_md(export_data)

        self.assertNotIn("提交详情抓取统计", markdown)
        self.assertNotIn("成功拿到源码详情", markdown)
        self.assertNotIn("摘要保底记录", markdown)
        self.assertNotIn("阻断原因：Need Login", markdown)

    def test_build_detail_fetch_overview_maps_status_and_counts(self):
        overview = build_detail_fetch_overview(
            {
                "total_items": 8,
                "source_code_success": 3,
                "summary_only": 5,
                "detail_skipped": 2,
                "pure_error_records": 0,
                "blocker_reason": "Need Login",
            }
        )

        self.assertEqual(overview["status_label"], "已触发止损")
        self.assertEqual(overview["source_code_success"], 3)
        self.assertEqual(overview["summary_only"], 5)
        self.assertEqual(overview["detail_skipped"], 2)
        self.assertEqual(overview["blocker_reason"], "Need Login")

    def test_split_practice_problems_uses_submitted_without_dup_passed(self):
        practice = DummyPractice(
            {
                "passed": [
                    {"pid": "P1000", "title": "Passed", "difficulty": 1, "type": "P", "tags": [1]},
                ],
                "submitted": [
                    {"pid": "P1000", "title": "Passed", "difficulty": 1, "type": "P", "tags": [1]},
                    {"pid": "P1001", "title": "Failed", "difficulty": 2, "type": "P", "tags": [2]},
                ],
            }
        )

        passed, failed = split_practice_problems(practice)

        self.assertEqual([problem.pid for problem in passed], ["P1000"])
        self.assertEqual([problem.pid for problem in failed], ["P1001"])
        self.assertTrue(failed[0].submitted)
        self.assertFalse(failed[0].accepted)

    def test_fetch_behavior_analysis_reports_auth_errors_clearly(self):
        class DummyLuogu:
            def get_record_list(self, **kwargs):
                raise AuthenticationError("Need Login")

        fake_behavior_module = SimpleNamespace(
            analyze_submission_behavior=lambda records: {"sample_count": len(records)}
        )

        with patch.dict("sys.modules", {"behavior_analyzer": fake_behavior_module}):
            result = fetch_behavior_analysis(DummyLuogu(), 1)

        self.assertIn("Cookies", result["error"])
        self.assertIn("提交记录列表", result["error"])

    def test_fetch_behavior_analysis_keeps_warning_when_fallback_is_used(self):
        class DummyLuogu:
            def get_record_list(self, **kwargs):
                raise AuthenticationError("Need Login")

        fake_behavior_module = SimpleNamespace(
            analyze_submission_behavior=lambda records: {"sample_count": len(records)}
        )
        fallback_items = [{"record": {"submitTime": 1234567890, "sourceCode": "print(1)"}}]

        with patch.dict("sys.modules", {"behavior_analyzer": fake_behavior_module}):
            result = fetch_behavior_analysis(DummyLuogu(), 1, fallback_items)

        self.assertEqual(result["sample_count"], 1)
        self.assertEqual(result["_source"], "record_detail_fallback")
        self.assertIn("Cookies", result["_warning"])


if __name__ == "__main__":
    unittest.main()
