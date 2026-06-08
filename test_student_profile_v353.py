"""v3.5.3 学员画像 + 奖项自录入 + 学段 GESP 视角 测试"""
import sys
import os
import time
import unittest
import tempfile
import sqlite3

sys.path.insert(0, '.')
sys.path.insert(0, 'docs')

# 关键：用临时 DB 隔离测试，不污染开发 DB
TEMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["LUOGU_DB_PATH"] = TEMP_DB

import task_store
from task_store import init_db, _get_conn
init_db()  # 在临时 DB 上建表

import admin_students


class TestCspAwardsCRUD(unittest.TestCase):
    """csp_awards CRUD + 校验"""

    @classmethod
    def setUpClass(cls):
        cls.sid = admin_students.create_student(
            f"test_csp_{int(time.time())}", real_name="测试学员", grade="JUNIOR_2",
            city="杭州", gender="M", is_minor=False, registered_via="smoke"
        )

    @classmethod
    def tearDownClass(cls):
        admin_students.delete_student(cls.sid)
        try:
            os.unlink(TEMP_DB)
        except OSError:
            pass

    def test_add_csp_award_basic(self):
        aid = admin_students.add_csp_award(
            student_id=self.sid, competition_type="csp_j_final",
            award_level="first", award_year=2024, actual_score=235,
            province="浙江", recorded_by="self",
        )
        self.assertIsInstance(aid, int)
        awards = admin_students.list_csp_awards(self.sid)
        self.assertEqual(len(awards), 1)
        a = awards[0]
        self.assertEqual(a["competition_type"], "csp_j_final")
        self.assertEqual(a["award_level"], "first")
        self.assertEqual(a["award_year"], 2024)
        self.assertEqual(a["actual_score"], 235)
        self.assertEqual(a["recorded_by"], "self")
        print(f"  [OK] add+list csp_award ({a['competition_type']}/{a['award_level']})")

    def test_add_csp_award_noip(self):
        aid = admin_students.add_csp_award(
            student_id=self.sid, competition_type="noip_1",
            award_level="first", award_year=2023,
        )
        self.assertIsInstance(aid, int)
        print(f"  [OK] add noip_1 award")

    def test_add_csp_award_invalid_type(self):
        with self.assertRaises(ValueError):
            admin_students.add_csp_award(
                student_id=self.sid, competition_type="INVALID_TYPE",
                award_level="first", award_year=2024,
            )
        print(f"  [OK] invalid type rejected")

    def test_add_csp_award_invalid_level(self):
        with self.assertRaises(ValueError):
            admin_students.add_csp_award(
                student_id=self.sid, competition_type="csp_j_final",
                award_level="INVALID_LEVEL", award_year=2024,
            )
        print(f"  [OK] invalid level rejected")

    def test_add_csp_award_invalid_year(self):
        with self.assertRaises(ValueError):
            admin_students.add_csp_award(
                student_id=self.sid, competition_type="csp_j_final",
                award_level="first", award_year=1900,
            )
        print(f"  [OK] invalid year rejected")

    def test_award_summary_best(self):
        summary = admin_students.get_student_award_summary(self.sid)
        # best_overall 应是最高赛事代码
        self.assertIn(summary["best_overall"], {"csp_j_final", "noip_1"})
        self.assertGreaterEqual(summary["total_awards"], 2)
        # best_label 是中文
        self.assertIsInstance(summary.get("best_label"), str)
        print(f"  [OK] best_overall={summary['best_overall']} label={summary['best_label']}")

    def test_delete_csp_award(self):
        # 加一个临时奖项用于删
        aid = admin_students.add_csp_award(
            student_id=self.sid, competition_type="noi_bronze",
            award_level="bronze", award_year=2022,
        )
        awards = admin_students.list_csp_awards(self.sid)
        self.assertEqual(len(awards), 3)
        # 删
        ok = admin_students.delete_csp_award(aid, self.sid)
        self.assertTrue(ok)
        awards2 = admin_students.list_csp_awards(self.sid)
        self.assertEqual(len(awards2), 2)
        print(f"  [OK] delete csp_award (3→2)")

    def test_delete_csp_award_wrong_owner(self):
        # 用错误 student_id 删应失败
        ok = admin_students.delete_csp_award(99999, self.sid)
        self.assertFalse(ok)
        print(f"  [OK] delete wrong-owner rejected")


class TestStageRecommendation(unittest.TestCase):
    """学段 GESP 视角判定（小学/初中/高中/大学）"""

    def test_primary_perspective(self):
        rec = admin_students._stage_recommendation("primary")
        self.assertEqual(rec["csp_visible"], False)
        self.assertIn("GESP", rec["perspective"])
        # primary 视角有 guidance（小学阶段建议）
        self.assertIsInstance(rec.get("guidance"), str)
        self.assertGreater(len(rec["guidance"]), 0)
        print(f"  [OK] primary: {rec['perspective']} · guidance len={len(rec['guidance'])}")

    def test_junior_perspective(self):
        rec = admin_students._stage_recommendation("junior")
        self.assertEqual(rec["csp_visible"], True)
        self.assertIn("CSP-J", rec["perspective"])
        self.assertIn("next_step", rec)
        print(f"  [OK] junior: {rec['perspective']} · next={rec['next_step']}")

    def test_senior_perspective(self):
        rec = admin_students._stage_recommendation("senior")
        self.assertEqual(rec["csp_visible"], True)
        self.assertIn("CSP-S", rec["perspective"])
        print(f"  [OK] senior: {rec['perspective']}")

    def test_univ_grade_falls_back_to_senior(self):
        """v3.5.4: NOI 不面向大学生，UNIV_* 兜底为 senior（不再有独立 university 分支）"""
        stage = admin_students._grade_to_stage("UNIV_3")
        self.assertEqual(stage, "senior")
        rec = admin_students._stage_recommendation(stage)
        self.assertEqual(rec["stage_label"], "高中")
        print(f"  [OK] UNIV_3 → senior (NOI 不面向大学生)")


class TestStudentProfile(unittest.TestCase):
    """compute_student_profile 集成"""

    @classmethod
    def setUpClass(cls):
        cls.sid = admin_students.create_student(
            f"test_prof_{int(time.time())}", real_name="画像测试", grade="JUNIOR_2",
            city="北京", gender="F", is_minor=True, registered_via="smoke"
        )

    @classmethod
    def tearDownClass(cls):
        admin_students.delete_student(cls.sid)
        try:
            os.unlink(TEMP_DB)
        except OSError:
            pass

    def test_profile_basic(self):
        profile = admin_students.compute_student_profile(self.sid)
        self.assertEqual(profile["stage"], "junior")
        # province 必须非空（_city_to_province("北京") 返回"直辖市"）
        self.assertIsNotNone(profile["province"])
        self.assertGreater(len(profile["province"]), 0)
        self.assertIn("stage_recommendation", profile)
        self.assertIn("award_summary", profile)
        print(f"  [OK] profile stage={profile['stage']} province={profile['province']}")

    def test_profile_with_birth(self):
        # 录入 birth_date 让 age 计算生效
        import datetime
        bd = (datetime.date.today() - datetime.timedelta(days=365 * 13)).isoformat()
        conn = _get_conn()
        conn.execute("UPDATE students SET birth_date = ? WHERE id = ?", (bd, self.sid))
        conn.commit()
        conn.close()
        profile = admin_students.compute_student_profile(self.sid)
        # 13 年前 → int(4745/365.25) = 12（闰年修正）；用 range [12, 13] 容错
        self.assertIn(profile["age"], (12, 13))
        # 重新测试 csp eligibility
        from docs.gesp_estimator import is_csp_age_eligible
        eligible_this_year = is_csp_age_eligible(bd, datetime.date.today().year)
        self.assertTrue(eligible_this_year)
        print(f"  [OK] profile age={profile['age']} csp_eligible={profile['is_csp_age_eligible']}")

    def test_profile_with_csp_award(self):
        # 录入 1 个 csp 奖项
        admin_students.add_csp_award(
            student_id=self.sid, competition_type="csp_s_final",
            award_level="second", award_year=2024,
        )
        profile = admin_students.compute_student_profile(self.sid)
        self.assertGreaterEqual(profile["award_summary"]["total_awards"], 1)
        self.assertEqual(profile["award_summary"]["best_overall"], "csp_s_final")
        print(f"  [OK] profile with csp_award: best={profile['award_summary']['best_overall']}")


class TestAddGespExamAwardYear(unittest.TestCase):
    """add_gesp_exam.award_year 持久化"""

    @classmethod
    def setUpClass(cls):
        cls.sid = admin_students.create_student(
            f"test_gesp_{int(time.time())}", real_name="GESP 测试", grade="PRIMARY_5",
            city="上海", gender="M", is_minor=True, registered_via="smoke"
        )
        # 临时 DB 没有 competitions 种子 → 注入一个 GESP 赛事供 add_gesp_exam 关联
        from task_store import _get_conn
        conn = _get_conn()
        cur = conn.execute(
            "INSERT INTO competitions (code, name, type, data_year, exam_date) "
            "VALUES (?, ?, ?, ?, ?)",
            ("GESP-2024-L7-8-09", "GESP 7-8 级 2024-09", "gesp", 2024, "2024-09-21"),
        )
        cls._gesp_exam_id = int(cur.lastrowid)
        conn.commit()
        conn.close()

    @classmethod
    def tearDownClass(cls):
        admin_students.delete_student(cls.sid)
        try:
            os.unlink(TEMP_DB)
        except OSError:
            pass

    def test_award_year_persisted(self):
        eid = admin_students.add_gesp_exam(
            student_id=self.sid, exam_id=self._gesp_exam_id, registered_level=4,
            actual_score=85, recorded_by="self", award_year=2024,
        )
        exams = admin_students.list_gesp_exams(self.sid)
        self.assertGreaterEqual(len(exams), 1)
        latest = exams[0]
        self.assertEqual(latest["award_year"], 2024)
        self.assertEqual(latest["actual_score"], 85)
        print(f"  [OK] gesp award_year=2024 persisted + listed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
