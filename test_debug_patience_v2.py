"""
v3.5.2 调试耐心 v2 测试
覆盖 3 种典型场景：
  1. 短重交 + 全是 CE (简单错误) → 5/5 效率高
  2. 短重交 + 全是 WA 提高级 → 1/5 真不耐心
  3. 长重交 + WA 入门 → 3/5 正常

并验证 v1+v2 合并函数。
"""
import sys
import os
import unittest
from collections import defaultdict

sys.path.insert(0, os.path.dirname(__file__))

from behavior_analyzer import (
    calc_debug_patience_v2,
    merge_debug_patience_v1_v2,
    _classify_error,
    _classify_difficulty,
    SIMPLE_ERROR_STATUSES,
    COMPLEX_ERROR_STATUSES,
)


def make_record(pid: str, status: int, t: int, difficulty: int = 0) -> dict:
    return {
        "status": status,
        "submitTime": t,
        "problem": {"pid": pid, "difficulty": difficulty},
    }


class TestClassifyHelpers(unittest.TestCase):
    """辅助分类函数测试"""

    def test_classify_error(self):
        self.assertEqual(_classify_error(12), "AC")
        self.assertEqual(_classify_error(3), "simple")   # CE
        self.assertEqual(_classify_error(4), "simple")   # CE
        self.assertEqual(_classify_error(5), "simple")   # RE
        self.assertEqual(_classify_error(10), "simple")  # PE
        self.assertEqual(_classify_error(8), "complex")  # TLE
        self.assertEqual(_classify_error(9), "complex")  # WA
        self.assertEqual(_classify_error(14), "complex") # OLE
        self.assertEqual(_classify_error(0), "other")    # unknown

    def test_classify_difficulty(self):
        self.assertEqual(_classify_difficulty(0), "entry")
        self.assertEqual(_classify_difficulty(2), "entry")
        self.assertEqual(_classify_difficulty(3), "popularize")
        self.assertEqual(_classify_difficulty(4), "popularize")
        self.assertEqual(_classify_difficulty(5), "improve")
        self.assertEqual(_classify_difficulty(6), "improve")
        self.assertEqual(_classify_difficulty(7), "advanced")
        self.assertEqual(_classify_difficulty(8), "advanced")

    def test_status_sets(self):
        # 确保简单/复杂错误集合不重叠
        self.assertEqual(SIMPLE_ERROR_STATUSES & COMPLEX_ERROR_STATUSES, set())
        # 12 (AC) 不在两个集合中
        self.assertNotIn(12, SIMPLE_ERROR_STATUSES)
        self.assertNotIn(12, COMPLEX_ERROR_STATUSES)


class TestDebugPatienceV2Scenarios(unittest.TestCase):
    """3 种核心场景测试"""

    def test_scenario_1_short_resubmit_with_ce_should_be_efficient(self):
        """场景 1：短重交 + 全是 CE → 5/5 效率高（不扣分）"""
        records = []
        # 模拟 5 道题，每道题 CE → 30 秒后改对 → AC
        for i in range(5):
            pid = f"P100{0}"
            records.append(make_record(pid, 3, 1000 + i*600))  # CE
            records.append(make_record(pid, 3, 1030 + i*600))  # 30 秒后 CE 又一次
            records.append(make_record(pid, 3, 1060 + i*600))  # 60 秒后 CE 再来
            records.append(make_record(pid, 12, 1120 + i*600)) # 60 秒后 AC
        # 难度 = 入门
        problems_meta = {"P1000": 0}
        result = calc_debug_patience_v2(records, problems_meta)
        print(f"\n[场景 1] 短重交+CE: score={result['score']}, score_1to5={result['score_1to5']}")
        print(f"  insight: {result['insight']}")
        # 期望：score >= 65 (高效调试信号触发 +15)
        self.assertGreaterEqual(result["score"], 65, f"期望 >= 65 实际 {result['score']}")
        # 期望：score_1to5 >= 4
        self.assertGreaterEqual(result["score_1to5"], 4, f"期望 >= 4 实际 {result['score_1to5']}")
        # 期望：simple 错误占绝大多数
        self.assertGreater(result["error_breakdown"]["simple"], 10)

    def test_scenario_2_short_resubmit_with_wa_advanced_should_be_impatient(self):
        """场景 2：短重交 + WA 提高级 → 1/5 真不耐心"""
        records = []
        # 模拟 5 道题，每道题 WA（提高级）→ 20 秒后重交
        for i in range(5):
            pid = f"P200{0}"
            records.append(make_record(pid, 9, 2000 + i*600))   # WA
            records.append(make_record(pid, 9, 2020 + i*600))   # 20 秒后 WA
            records.append(make_record(pid, 9, 2050 + i*600))   # 30 秒后 WA
            records.append(make_record(pid, 9, 2080 + i*600))   # 30 秒后 WA
        # 难度 = 提高（5-6）
        problems_meta = {"P2000": 6}
        result = calc_debug_patience_v2(records, problems_meta)
        print(f"\n[场景 2] 短重交+WA 提高级: score={result['score']}, score_1to5={result['score_1to5']}")
        print(f"  insight: {result['insight']}")
        # 期望：score <= 35 (复杂错误短重交 + 高难度 = -25)
        self.assertLessEqual(result["score"], 35, f"期望 <= 35 实际 {result['score']}")
        # 期望：score_1to5 <= 2
        self.assertLessEqual(result["score_1to5"], 2, f"期望 <= 2 实际 {result['score_1to5']}")
        # 期望：complex 错误占绝大多数
        self.assertGreater(result["error_breakdown"]["complex"], 10)
        # 期望：complex_quick_resubmit_rate > 0.4
        self.assertGreater(result["complex_quick_resubmit_rate"], 0.4)

    def test_scenario_3_long_resubmit_with_wa_entry_should_be_normal(self):
        """场景 3：长重交 + WA 入门 → 3/5 正常（思考）"""
        records = []
        # 模拟 3 道题，每道题 WA 入门 → 10 分钟后改对
        for i in range(3):
            pid = f"P300{0}"
            records.append(make_record(pid, 9, 3000 + i*1200))    # WA
            records.append(make_record(pid, 9, 3600 + i*1200))    # 10 分钟后 WA
            records.append(make_record(pid, 9, 4200 + i*1200))    # 10 分钟后 WA
            records.append(make_record(pid, 12, 4800 + i*1200))   # 10 分钟后 AC
        # 难度 = 入门
        problems_meta = {"P3000": 1}
        result = calc_debug_patience_v2(records, problems_meta)
        print(f"\n[场景 3] 长重交+WA 入门: score={result['score']}, score_1to5={result['score_1to5']}")
        print(f"  insight: {result['insight']}")
        # 期望：score 接近 50-70 (复杂错误慢重交 = +10)
        self.assertGreaterEqual(result["score"], 50, f"期望 >= 50 实际 {result['score']}")
        # 期望：complex_quick_resubmit_rate = 0
        self.assertEqual(result["complex_quick_resubmit_rate"], 0.0)
        # 期望：complex 错误有
        self.assertGreater(result["error_breakdown"]["complex"], 5)


class TestDebugPatienceV2EdgeCases(unittest.TestCase):
    """边界 case 测试"""

    def test_empty_records(self):
        result = calc_debug_patience_v2([], {})
        self.assertEqual(result["score"], 50)
        self.assertEqual(result["score_1to5"], 3)
        self.assertEqual(result["insight"], "无提交记录")

    def test_all_ac_records(self):
        """全 AC（无错误样本）→ 默认 50 + insight 提示"""
        records = [
            make_record("P100", 12, 1000),
            make_record("P200", 12, 2000),
        ]
        result = calc_debug_patience_v2(records, {})
        # 全 AC 没有 error_breakdown 样本
        self.assertEqual(result["error_breakdown"]["simple"], 0)
        self.assertEqual(result["error_breakdown"]["complex"], 0)
        # 提示全 AC
        self.assertIn("无复杂错误", result["insight"])

    def test_mixed_simple_and_complex(self):
        """混合错误类型 → 综合判定"""
        records = []
        # 题 1: CE → 30 秒后 AC（简单 + 短重交 = 高效）
        records.append(make_record("P1", 3, 1000))
        records.append(make_record("P1", 12, 1030))
        # 题 2: WA 提高级 → 10 分钟后再交（复杂 + 长重交 = 思考）
        records.append(make_record("P2", 9, 2000))
        records.append(make_record("P2", 9, 2600))
        records.append(make_record("P2", 12, 3200))
        problems_meta = {"P1": 1, "P2": 6}
        result = calc_debug_patience_v2(records, problems_meta)
        print(f"\n[混合] simple={result['error_breakdown']['simple']}, complex={result['error_breakdown']['complex']}")
        print(f"  insight: {result['insight']}")
        # 期望：混合场景
        self.assertEqual(result["error_breakdown"]["simple"], 1)
        self.assertEqual(result["error_breakdown"]["complex"], 2)
        # simple_ratio 约 33%
        self.assertAlmostEqual(result["simple_ratio"], 1/3, places=2)

    def test_resubmit_interval_ignored_when_too_long(self):
        """超过 1 小时的"重交"不计入（避免误判）"""
        records = []
        records.append(make_record("P1", 3, 1000))
        records.append(make_record("P1", 9, 1000 + 7200))  # 2 小时后
        result = calc_debug_patience_v2(records, {"P1": 1})
        # 2 小时间隔被忽略，simple_intervals 应空
        self.assertEqual(result["simple_quick_resubmit_rate"], 0.0)
        self.assertEqual(result["complex_quick_resubmit_rate"], 0.0)

    def test_no_problems_meta_defaults_to_entry(self):
        """problems_meta 为空时，按入门难度处理"""
        records = [
            make_record("P1", 9, 1000),
            make_record("P1", 9, 1020),  # 20 秒短重交
        ]
        result = calc_debug_patience_v2(records, None)
        # 默认 entry 难度 → 短重交 = -5（轻微扣分）
        self.assertGreater(result["complex_quick_resubmit_rate"], 0.4)


class TestMergeDebugPatience(unittest.TestCase):
    """v1 + v2 合并函数测试"""

    def test_v2_has_samples_use_v2(self):
        """v2 有样本时优先使用 v2"""
        v1 = {"median_resubmit_interval_seconds": 82, "quick_resubmit_under_60s_rate": 0.4}
        v2 = {
            "score": 80, "score_1to5": 4,
            "error_breakdown": {"simple": 10, "complex": 3, "ac_after_error": 8, "other": 0},
            "simple_quick_resubmit_rate": 0.6,
            "complex_quick_resubmit_rate": 0.2,
            "insight": "test"
        }
        merged = merge_debug_patience_v1_v2(v1, v2)
        self.assertEqual(merged["primary_source"], "v2")
        self.assertEqual(merged["primary_score"], 80)
        self.assertEqual(merged["primary_score_1to5"], 4)
        # v1 数据保留作对比
        self.assertEqual(merged["v1_median_resubmit_seconds"], 82)

    def test_v2_no_samples_fallback_to_v1(self):
        """v2 无样本（全是 AC）→ 回退 v1 提示"""
        v1 = {"median_resubmit_interval_seconds": 100, "quick_resubmit_under_60s_rate": 0.1}
        v2 = {
            "score": 50, "score_1to5": 3,
            "error_breakdown": {"simple": 0, "complex": 0, "ac_after_error": 0, "other": 0},
            "simple_quick_resubmit_rate": 0.0,
            "complex_quick_resubmit_rate": 0.0,
            "insight": "全 AC"
        }
        merged = merge_debug_patience_v1_v2(v1, v2)
        self.assertEqual(merged["primary_source"], "v1")
        self.assertIsNone(merged["primary_score"])
        self.assertIn("保持 v1", merged["v2_insight"])

    def test_v2_none_input(self):
        """v2 为 None → 仍能工作（兼容空数据）"""
        v1 = {"median_resubmit_interval_seconds": 60, "quick_resubmit_under_60s_rate": 0.3}
        merged = merge_debug_patience_v1_v2(v1, None)
        self.assertEqual(merged["primary_source"], "v1")
        self.assertIsNone(merged["primary_score"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
