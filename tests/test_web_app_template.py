import unittest
import importlib
import sys
import types
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from jinja2 import Environment, FileSystemLoader


class TestWebAppTemplate(unittest.TestCase):
    def test_index_template_includes_c3vk_cookie_field_and_instructions(self):
        root = Path(__file__).resolve().parents[1]
        content = (root / "web_app.py").read_text(encoding="utf-8")

        self.assertIn("C3VK", content)
        self.assertIn("https://www.luogu.com.cn", content)
        self.assertIn("Application(应用)", content)
        self.assertIn("Storage", content)
        self.assertIn("Cookies", content)
        self.assertIn("Name/Value", content)
        self.assertIn('name="c3vk"', content)
        self.assertIn('name="c3vk" value="{{ form_values.c3vk }}" required', content)
        self.assertIn('name="api_key" value="{{ form_values.api_key }}"', content)
        self.assertNotIn("C3VK（如有）", content)
        self.assertIn('formaction="/validate-cookies"', content)
        self.assertIn("校验 Cookies", content)
        self.assertIn("AI 报告生成进度", content)
        self.assertIn("重建 HTML", content)
        self.assertIn("/admin/rebuild-html/", content)
        self.assertIn("重建中...", content)
        self.assertIn("/admin/login", content)
        self.assertIn("ADMIN_USERNAME", content)
        self.assertIn("ADMIN_PASSWORD", content)
        self.assertIn('/retry/<task_id>', content)

    def test_report_template_includes_detail_fetch_overview_cards(self):
        root = Path(__file__).resolve().parents[1]
        content = (root / "report_template.html").read_text(encoding="utf-8")

        self.assertIn("提交详情抓取概览", content)
        self.assertIn("detail_fetch_overview | default", content)
        self.assertIn("df.status_label", content)
        self.assertIn("df.source_code_success", content)
        self.assertIn("df.blocker_reason", content)
        self.assertIn("图 5：通过/未通过占比", content)
        self.assertIn("chart_paths.status", content)
        self.assertIn("图 6：高频算法标签 Top 8", content)
        self.assertIn("chart_paths.tags", content)

    def test_report_template_renders_without_detail_fetch_overview(self):
        root = Path(__file__).resolve().parents[1]
        env = Environment(loader=FileSystemLoader(str(root)))
        template = env.get_template("report_template.html")

        rendered = template.render(
            export_data={
                "student_info": {"name": "测试", "eval_time": "2026-06-03 12:00", "school": "学校", "grade": "年级"},
                "solved_count": 1,
                "failed_count": 0,
            },
            report_html="<h1>测试报告</h1>",
            chart_paths={},
            avg_difficulty="2.0",
            avg_difficulty_label="普及-",
            avg_difficulty_color="#52C41A",
            avg_difficulty_text_color="#FFFFFF",
            top_tag="贪心",
        )

        self.assertIn("提交详情抓取概览", rendered)
        self.assertIn("未抓取详情", rendered)
        self.assertIn("阻断原因", rendered)

    def test_web_app_formats_practice_auth_error_as_cookie_hint(self):
        root = Path(__file__).resolve().parents[1]
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))

        flask = types.ModuleType("flask")

        class DummyFlask:
            def __init__(self, *args, **kwargs):
                pass

            def route(self, *args, **kwargs):
                def deco(fn):
                    return fn
                return deco

        flask.Flask = DummyFlask
        flask.render_template_string = lambda *args, **kwargs: ""
        flask.request = types.SimpleNamespace(form={})
        flask.redirect = lambda value: value
        flask.url_for = lambda *args, **kwargs: ""
        flask.send_from_directory = lambda *args, **kwargs: ""
        flask.send_file = lambda *args, **kwargs: types.SimpleNamespace(headers={})

        original_flask = sys.modules.get("flask")
        original_web_app = sys.modules.pop("web_app", None)
        sys.modules["flask"] = flask
        try:
            web_app = importlib.import_module("web_app")
            from pyLuogu.errors import AuthenticationError

            message = web_app.describe_generation_error(AuthenticationError("Need Login"), "获取标签与练习数据")
            self.assertIn("Cookies 无效或已失效", message)
            self.assertIn("无法读取练习数据", message)
        finally:
            sys.modules.pop("web_app", None)
            if original_web_app is not None:
                sys.modules["web_app"] = original_web_app
            if original_flask is not None:
                sys.modules["flask"] = original_flask
            else:
                sys.modules.pop("flask", None)

    def test_web_app_formats_missing_openai_credentials_as_hint(self):
        root = Path(__file__).resolve().parents[1]
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))

        flask = types.ModuleType("flask")

        class DummyFlask:
            def __init__(self, *args, **kwargs):
                pass

            def route(self, *args, **kwargs):
                def deco(fn):
                    return fn
                return deco

        flask.Flask = DummyFlask
        flask.render_template_string = lambda *args, **kwargs: ""
        flask.request = types.SimpleNamespace(form={})
        flask.redirect = lambda value: value
        flask.url_for = lambda *args, **kwargs: ""
        flask.send_from_directory = lambda *args, **kwargs: ""
        flask.send_file = lambda *args, **kwargs: types.SimpleNamespace(headers={})

        original_flask = sys.modules.get("flask")
        original_web_app = sys.modules.pop("web_app", None)
        sys.modules["flask"] = flask
        try:
            web_app = importlib.import_module("web_app")
            msg = web_app.describe_generation_error(
                Exception("Missing credentials. Please pass an `api_key`."),
                "生成 AI 报告",
            )
            self.assertIn("未配置 OpenAI API Key", msg)
            self.assertIn("OPENAI_API_KEY", msg)
        finally:
            sys.modules.pop("web_app", None)
            if original_web_app is not None:
                sys.modules["web_app"] = original_web_app
            if original_flask is not None:
                sys.modules["flask"] = original_flask
            else:
                sys.modules.pop("flask", None)

    def test_web_app_rejects_missing_required_cookie_fields(self):
        root = Path(__file__).resolve().parents[1]
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))

        flask = types.ModuleType("flask")

        class DummyFlask:
            def __init__(self, *args, **kwargs):
                pass

            def route(self, *args, **kwargs):
                def deco(fn):
                    return fn
                return deco

        flask.Flask = DummyFlask
        flask.render_template_string = lambda *args, **kwargs: ""
        flask.request = types.SimpleNamespace(form={})
        flask.redirect = lambda value: value
        flask.url_for = lambda *args, **kwargs: ""
        flask.send_from_directory = lambda *args, **kwargs: ""
        flask.send_file = lambda *args, **kwargs: types.SimpleNamespace(headers={})

        original_flask = sys.modules.get("flask")
        original_web_app = sys.modules.pop("web_app", None)
        sys.modules["flask"] = flask
        try:
            web_app = importlib.import_module("web_app")

            with self.assertRaises(ValueError) as cm:
                web_app.build_cookie_dict({"client_id": "abc", "uid": "123", "c3vk": ""})

            self.assertIn("Cookies 参数为必填项", str(cm.exception))
            self.assertIn("C3VK", str(cm.exception))
        finally:
            sys.modules.pop("web_app", None)
            if original_web_app is not None:
                sys.modules["web_app"] = original_web_app
            if original_flask is not None:
                sys.modules["flask"] = original_flask
            else:
                sys.modules.pop("flask", None)

    def test_validate_cookies_reports_record_list_failure_clearly(self):
        root = Path(__file__).resolve().parents[1]
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))

        flask = types.ModuleType("flask")

        class DummyFlask:
            def __init__(self, *args, **kwargs):
                pass

            def route(self, *args, **kwargs):
                def deco(fn):
                    return fn
                return deco

        flask.Flask = DummyFlask
        flask.render_template_string = lambda *args, **kwargs: ""
        flask.request = types.SimpleNamespace(form={})
        flask.redirect = lambda value: value
        flask.url_for = lambda *args, **kwargs: ""
        flask.send_from_directory = lambda *args, **kwargs: ""
        flask.send_file = lambda *args, **kwargs: types.SimpleNamespace(headers={})

        original_flask = sys.modules.get("flask")
        original_web_app = sys.modules.pop("web_app", None)
        sys.modules["flask"] = flask
        try:
            web_app = importlib.import_module("web_app")
            from pyLuogu.errors import AuthenticationError

            class FakeMe:
                uid = 123

            class FakeAPI:
                def __init__(self, cookies=None):
                    self.cookies = cookies

                def me(self):
                    return FakeMe()

                def get_user_practice(self, uid):
                    return {"passedProblems": [], "submittedProblems": []}

                def get_record_list(self, **kwargs):
                    raise AuthenticationError("Need Login")

                def close(self):
                    return None

            web_app.pyLuogu.luoguAPI = FakeAPI
            result = web_app.validate_cookies({"client_id": "abc", "uid": "123", "c3vk": "xyz"})

            self.assertFalse(result["ok"])
            self.assertIn("无法读取提交记录", result["message"])
        finally:
            sys.modules.pop("web_app", None)
            if original_web_app is not None:
                sys.modules["web_app"] = original_web_app
            if original_flask is not None:
                sys.modules["flask"] = original_flask
            else:
                sys.modules.pop("flask", None)

    def test_validate_cookies_reports_success_summary(self):
        root = Path(__file__).resolve().parents[1]
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))

        flask = types.ModuleType("flask")

        class DummyFlask:
            def __init__(self, *args, **kwargs):
                pass

            def route(self, *args, **kwargs):
                def deco(fn):
                    return fn
                return deco

        flask.Flask = DummyFlask
        flask.render_template_string = lambda *args, **kwargs: ""
        flask.request = types.SimpleNamespace(form={})
        flask.redirect = lambda value: value
        flask.url_for = lambda *args, **kwargs: ""
        flask.send_from_directory = lambda *args, **kwargs: ""
        flask.send_file = lambda *args, **kwargs: types.SimpleNamespace(headers={})

        original_flask = sys.modules.get("flask")
        original_web_app = sys.modules.pop("web_app", None)
        sys.modules["flask"] = flask
        try:
            web_app = importlib.import_module("web_app")

            class FakeProblem:
                def __init__(self, pid, difficulty=1):
                    self.pid = pid
                    self.difficulty = difficulty

                def to_json(self):
                    return {"pid": self.pid, "difficulty": self.difficulty}

            class FakePractice:
                def __init__(self):
                    self.passedProblems = [FakeProblem("P1000")]
                    self.submittedProblems = [FakeProblem("P1001")]

            class FakeRecordList:
                records = [object(), object()]

            class FakeMe:
                uid = 123

            class FakeAPI:
                def __init__(self, cookies=None):
                    self.cookies = cookies

                def me(self):
                    return FakeMe()

                def get_user_practice(self, uid):
                    return FakePractice()

                def get_record_list(self, **kwargs):
                    return FakeRecordList()

                def close(self):
                    return None

            web_app.pyLuogu.luoguAPI = FakeAPI
            result = web_app.validate_cookies({"client_id": "abc", "uid": "123", "c3vk": "xyz"})

            self.assertTrue(result["ok"])
            self.assertIn("Cookies 校验通过", result["title"])
            self.assertIn("record/list", result["message"])
        finally:
            sys.modules.pop("web_app", None)
            if original_web_app is not None:
                sys.modules["web_app"] = original_web_app
            if original_flask is not None:
                sys.modules["flask"] = original_flask
            else:
                sys.modules.pop("flask", None)

    def test_rebuild_existing_report_html_skips_pdf_export(self):
        root = Path(__file__).resolve().parents[1]
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))

        flask = types.ModuleType("flask")

        class DummyFlask:
            def __init__(self, *args, **kwargs):
                pass

            def route(self, *args, **kwargs):
                def deco(fn):
                    return fn
                return deco

        flask.Flask = DummyFlask
        flask.render_template_string = lambda *args, **kwargs: ""
        flask.request = types.SimpleNamespace(form={}, args={})
        flask.redirect = lambda value: value
        flask.url_for = lambda *args, **kwargs: ""
        flask.send_from_directory = lambda *args, **kwargs: ""
        flask.send_file = lambda *args, **kwargs: types.SimpleNamespace(headers={})

        original_flask = sys.modules.get("flask")
        original_web_app = sys.modules.pop("web_app", None)
        sys.modules["flask"] = flask
        try:
            web_app = importlib.import_module("web_app")
            with tempfile.TemporaryDirectory(dir=root) as tmpdir:
                report_dir = Path(tmpdir) / "reports" / "unit_test_report"
                assets_dir = report_dir / "assets"
                assets_dir.mkdir(parents=True, exist_ok=True)
                (report_dir / "export_data.json").write_text(
                    json.dumps(
                        {
                            "student_info": {
                                "name": "测试",
                                "school": "学校",
                                "grade": "年级",
                                "eval_time": "2026-06-04 14:00",
                            },
                            "solved_count": 1,
                            "failed_count": 0,
                            "summary": {"difficulty_histogram": {"2": 1}},
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                (report_dir / "report.md").write_text("# 测试报告\n", encoding="utf-8")

                relative_dir = report_dir.relative_to(root).as_posix()
                task = {
                    "html": f"/{relative_dir}/report.html",
                    "md": f"/{relative_dir}/report.md",
                    "pdf": f"/{relative_dir}/report.pdf",
                }
                captured = {}
                updates = {}

                web_app.get_task = lambda task_id: task
                web_app.generate_chart_images = lambda export_data, assets_path: {
                    "difficulty": str(Path(assets_path) / "difficulty_histogram.png")
                }

                def fake_build_html_and_pdf(report_md, export_data, html_path, pdf_path, chart_paths, export_pdf=True):
                    captured["html_path"] = html_path
                    captured["pdf_path"] = pdf_path
                    captured["chart_paths"] = chart_paths
                    captured["export_pdf"] = export_pdf
                    Path(html_path).write_text("<html></html>", encoding="utf-8")

                web_app.build_html_and_pdf = fake_build_html_and_pdf
                web_app.update_task = lambda task_id, **kwargs: updates.update(kwargs)

                import os
                cwd_before = Path.cwd()
                try:
                    os.chdir(root)
                    result = web_app.rebuild_existing_report_html("task-1")
                finally:
                    os.chdir(cwd_before)

                self.assertEqual(result, task["html"])
                self.assertEqual(captured["export_pdf"], False)
                self.assertTrue(str(captured["html_path"]).endswith("report.html"))
                self.assertEqual(updates["message"], "已重建 HTML 报告")
        finally:
            sys.modules.pop("web_app", None)
            if original_web_app is not None:
                sys.modules["web_app"] = original_web_app
            if original_flask is not None:
                sys.modules["flask"] = original_flask
            else:
                sys.modules.pop("flask", None)

    def test_check_admin_credentials_uses_env_values(self):
        root = Path(__file__).resolve().parents[1]
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))

        flask = types.ModuleType("flask")

        class DummyFlask:
            def __init__(self, *args, **kwargs):
                pass

            def route(self, *args, **kwargs):
                def deco(fn):
                    return fn
                return deco

        flask.Flask = DummyFlask
        flask.render_template_string = lambda *args, **kwargs: ""
        flask.request = types.SimpleNamespace(form={}, args={}, path="/admin", method="GET")
        flask.redirect = lambda value: value
        flask.url_for = lambda endpoint, **kwargs: f"/{endpoint}"
        flask.send_from_directory = lambda *args, **kwargs: ""
        flask.send_file = lambda *args, **kwargs: types.SimpleNamespace(headers={})
        flask.session = {}

        original_flask = sys.modules.get("flask")
        original_web_app = sys.modules.pop("web_app", None)
        old_user = os.environ.get("ADMIN_USERNAME")
        old_pass = os.environ.get("ADMIN_PASSWORD")
        os.environ["ADMIN_USERNAME"] = "root-admin"
        os.environ["ADMIN_PASSWORD"] = "secret-pass"
        sys.modules["flask"] = flask
        try:
            web_app = importlib.import_module("web_app")
            self.assertTrue(web_app.check_admin_credentials("root-admin", "secret-pass"))
            self.assertFalse(web_app.check_admin_credentials("root-admin", "bad-pass"))
            self.assertEqual(web_app.sanitize_admin_next("/foo"), "/admin")
            self.assertEqual(web_app.sanitize_admin_next("/admin"), "/admin")
        finally:
            sys.modules.pop("web_app", None)
            if original_web_app is not None:
                sys.modules["web_app"] = original_web_app
            if original_flask is not None:
                sys.modules["flask"] = original_flask
            else:
                sys.modules.pop("flask", None)
            if old_user is None:
                os.environ.pop("ADMIN_USERNAME", None)
            else:
                os.environ["ADMIN_USERNAME"] = old_user
            if old_pass is None:
                os.environ.pop("ADMIN_PASSWORD", None)
            else:
                os.environ["ADMIN_PASSWORD"] = old_pass

    def test_admin_rebuild_route_reports_running_state(self):
        root = Path(__file__).resolve().parents[1]
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))

        flask = types.ModuleType("flask")

        class DummyFlask:
            def __init__(self, *args, **kwargs):
                pass

            def route(self, *args, **kwargs):
                def deco(fn):
                    return fn
                return deco

        flask.Flask = DummyFlask
        flask.render_template_string = lambda *args, **kwargs: ""
        flask.request = types.SimpleNamespace(form={}, args={}, path="/admin/rebuild-html/task-1", method="POST")
        flask.redirect = lambda value: value
        flask.url_for = lambda endpoint, **kwargs: f"/{endpoint}?notice={kwargs.get('notice','')}&notice_type={kwargs.get('notice_type','')}"
        flask.send_from_directory = lambda *args, **kwargs: ""
        flask.send_file = lambda *args, **kwargs: types.SimpleNamespace(headers={})
        flask.session = {"admin_authed": True, "admin_user": "admin"}

        original_flask = sys.modules.get("flask")
        original_web_app = sys.modules.pop("web_app", None)
        sys.modules["flask"] = flask
        try:
            web_app = importlib.import_module("web_app")
            web_app.REBUILD_TASKS["task-1"] = {"status": "running", "message": "正在重建 HTML..."}
            result = web_app.admin_rebuild_html("task-1")
            self.assertIn("正在重建中", result)
            web_app.REBUILD_TASKS.clear()
        finally:
            sys.modules.pop("web_app", None)
            if original_web_app is not None:
                sys.modules["web_app"] = original_web_app
            if original_flask is not None:
                sys.modules["flask"] = original_flask
            else:
                sys.modules.pop("flask", None)

    def test_run_rebuild_existing_report_html_exports_pdf(self):
        root = Path(__file__).resolve().parents[1]
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))

        flask = types.ModuleType("flask")

        class DummyFlask:
            def __init__(self, *args, **kwargs):
                pass

            def route(self, *args, **kwargs):
                def deco(fn):
                    return fn
                return deco

        flask.Flask = DummyFlask
        flask.render_template_string = lambda *args, **kwargs: ""
        flask.request = types.SimpleNamespace(form={}, args={}, path="/admin", method="GET")
        flask.redirect = lambda value: value
        flask.url_for = lambda endpoint, **kwargs: f"/{endpoint}"
        flask.send_from_directory = lambda *args, **kwargs: ""
        flask.send_file = lambda *args, **kwargs: types.SimpleNamespace(headers={})
        flask.session = {"admin_authed": True, "admin_user": "admin"}

        original_flask = sys.modules.get("flask")
        original_web_app = sys.modules.pop("web_app", None)
        sys.modules["flask"] = flask
        try:
            web_app = importlib.import_module("web_app")
            captured = {}
            states = []

            web_app.rebuild_existing_report_html = lambda task_id, export_pdf=False: captured.update(
                {"task_id": task_id, "export_pdf": export_pdf}
            )
            web_app.set_rebuild_state = lambda task_id, status, message="": states.append((task_id, status, message))

            web_app._run_rebuild_existing_report_html("task-2")

            self.assertEqual(captured["task_id"], "task-2")
            self.assertTrue(captured["export_pdf"])
            self.assertEqual(states[0][1], "running")
            self.assertEqual(states[-1][1], "done")
        finally:
            sys.modules.pop("web_app", None)
            if original_web_app is not None:
                sys.modules["web_app"] = original_web_app
            if original_flask is not None:
                sys.modules["flask"] = original_flask
            else:
                sys.modules.pop("flask", None)

    def test_report_url_appends_version_query(self):
        root = Path(__file__).resolve().parents[1]
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))

        flask = types.ModuleType("flask")

        class DummyFlask:
            def __init__(self, *args, **kwargs):
                pass

            def route(self, *args, **kwargs):
                def deco(fn):
                    return fn
                return deco

        flask.Flask = DummyFlask
        flask.render_template_string = lambda *args, **kwargs: ""
        flask.request = types.SimpleNamespace(form={}, args={}, path="/", method="GET")
        flask.redirect = lambda value: value
        flask.url_for = lambda endpoint, **kwargs: f"/{endpoint}"
        flask.send_from_directory = lambda *args, **kwargs: types.SimpleNamespace(headers={})
        flask.send_file = lambda *args, **kwargs: types.SimpleNamespace(headers={})
        flask.session = {}

        original_flask = sys.modules.get("flask")
        original_web_app = sys.modules.pop("web_app", None)
        sys.modules["flask"] = flask
        try:
            web_app = importlib.import_module("web_app")
            tmp = root / "tmp_report_url_test.txt"
            tmp.write_text("ok", encoding="utf-8")
            try:
                url = web_app._report_url(tmp)
            finally:
                tmp.unlink(missing_ok=True)
            self.assertIn("tmp_report_url_test.txt?v=", url)
        finally:
            sys.modules.pop("web_app", None)
            if original_web_app is not None:
                sys.modules["web_app"] = original_web_app
            if original_flask is not None:
                sys.modules["flask"] = original_flask
            else:
                sys.modules.pop("flask", None)

    def test_admin_page_uses_active_generation_count_and_reconcile_notice(self):
        root = Path(__file__).resolve().parents[1]
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))

        flask = types.ModuleType("flask")

        class DummyFlask:
            def __init__(self, *args, **kwargs):
                pass

            def route(self, *args, **kwargs):
                def deco(fn):
                    return fn
                return deco

        flask.Flask = DummyFlask
        flask.render_template_string = lambda *args, **kwargs: kwargs
        flask.request = types.SimpleNamespace(form={}, args={}, path="/admin", method="GET")
        flask.redirect = lambda value: value
        flask.url_for = lambda endpoint, **kwargs: f"/{endpoint}"
        flask.send_from_directory = lambda *args, **kwargs: types.SimpleNamespace(headers={})
        flask.send_file = lambda *args, **kwargs: types.SimpleNamespace(headers={})
        flask.session = {"admin_authed": True, "admin_user": "admin"}

        original_flask = sys.modules.get("flask")
        original_web_app = sys.modules.pop("web_app", None)
        sys.modules["flask"] = flask
        try:
            web_app = importlib.import_module("web_app")
            web_app.require_admin_auth = lambda: None
            web_app.reconcile_stale_generation_tasks = lambda: 2
            today = datetime.now().strftime("%Y-%m-%d")
            web_app.list_tasks = lambda: [
                {
                    "task_id": "task-1",
                    "student_name": "张三",
                    "school": "测试学校",
                    "grade": "六年级",
                    "solved_count": 3,
                    "failed_count": 1,
                    "status": "done",
                    "eval_time": f"{today} 10:00",
                    "created_at": f"{today} 09:00:00",
                    "html": "/reports/task-1/report.html",
                    "pdf": "/reports/task-1/report.pdf",
                    "md": "/reports/task-1/report.md",
                }
            ]
            web_app.discover_orphan_report_tasks = lambda: [
                {
                    "id": "orphan-1",
                    "name": "历史报告",
                    "school": "未知学校",
                    "grade": "未知年级",
                    "solved": 0,
                    "failed": 0,
                    "status": "done",
                    "time": f"{today} 08:00",
                    "html": "/reports/orphan-1/report.html",
                    "pdf": "/download-report/orphan-1/report.pdf",
                    "md": "/reports/orphan-1/report.md",
                    "rebuild_status": "",
                    "rebuild_message": "",
                    "can_rebuild": False,
                    "is_orphan": True,
                    "sort_time": datetime(2026, 6, 4, 8, 0),
                }
            ]
            web_app.get_active_generation_task_count = lambda: 0

            result = web_app.admin_page()

            self.assertEqual(result["running_tasks"], 0)
            self.assertEqual(result["total_tasks"], 2)
            self.assertEqual(result["today_tasks"], 2)
            self.assertEqual(len(result["tasks"]), 2)
            self.assertIn("已自动修正 2 条失真的进行中任务状态", result["notice"])
        finally:
            sys.modules.pop("web_app", None)
            if original_web_app is not None:
                sys.modules["web_app"] = original_web_app
            if original_flask is not None:
                sys.modules["flask"] = original_flask
            else:
                sys.modules.pop("flask", None)

    def test_download_report_url_rewrites_pdf_report_links(self):
        root = Path(__file__).resolve().parents[1]
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))

        flask = types.ModuleType("flask")

        class DummyFlask:
            def __init__(self, *args, **kwargs):
                pass

            def route(self, *args, **kwargs):
                def deco(fn):
                    return fn
                return deco

        flask.Flask = DummyFlask
        flask.render_template_string = lambda *args, **kwargs: ""
        flask.request = types.SimpleNamespace(form={}, args={}, path="/", method="GET")
        flask.redirect = lambda value: value
        flask.url_for = lambda endpoint, **kwargs: f"/{endpoint}"
        flask.send_from_directory = lambda *args, **kwargs: types.SimpleNamespace(headers={})
        flask.send_file = lambda *args, **kwargs: types.SimpleNamespace(headers={})
        flask.session = {}

        original_flask = sys.modules.get("flask")
        original_web_app = sys.modules.pop("web_app", None)
        sys.modules["flask"] = flask
        try:
            web_app = importlib.import_module("web_app")
            url = web_app._download_report_url("/reports/991f72d2_%E9%BB%84%E9%BC%8E/report.pdf?v=123")
            self.assertEqual(url, "/download-report/991f72d2_%E9%BB%84%E9%BC%8E/report.pdf?v=123")
        finally:
            sys.modules.pop("web_app", None)
            if original_web_app is not None:
                sys.modules["web_app"] = original_web_app
            if original_flask is not None:
                sys.modules["flask"] = original_flask
            else:
                sys.modules.pop("flask", None)

    def test_source_cache_roundtrip_for_source_code_record(self):
        root = Path(__file__).resolve().parents[1]
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))

        flask = types.ModuleType("flask")

        class DummyFlask:
            def __init__(self, *args, **kwargs):
                pass

            def route(self, *args, **kwargs):
                def deco(fn):
                    return fn
                return deco

        flask.Flask = DummyFlask
        flask.render_template_string = lambda *args, **kwargs: ""
        flask.request = types.SimpleNamespace(form={}, args={}, path="/", method="GET")
        flask.redirect = lambda value: value
        flask.url_for = lambda endpoint, **kwargs: f"/{endpoint}"
        flask.send_from_directory = lambda *args, **kwargs: types.SimpleNamespace(headers={})
        flask.send_file = lambda *args, **kwargs: types.SimpleNamespace(headers={})
        flask.session = {}

        original_flask = sys.modules.get("flask")
        original_web_app = sys.modules.pop("web_app", None)
        sys.modules["flask"] = flask
        try:
            web_app = importlib.import_module("web_app")
            temp_root = Path(tempfile.mkdtemp())
            original_root = web_app._ROOT
            web_app._ROOT = temp_root
            try:
                record = {"id": 1, "sourceCode": "print(1)", "score": 100}
                web_app.save_cached_source_record(123, "P1001", record)
                loaded = web_app.load_cached_source_record(123, "P1001")
            finally:
                web_app._ROOT = original_root
                import shutil
                shutil.rmtree(temp_root, ignore_errors=True)

            self.assertIsInstance(loaded, dict)
            self.assertEqual(loaded["sourceCode"], "print(1)")
            self.assertEqual(loaded["score"], 100)
        finally:
            sys.modules.pop("web_app", None)
            if original_web_app is not None:
                sys.modules["web_app"] = original_web_app
            if original_flask is not None:
                sys.modules["flask"] = original_flask
            else:
                sys.modules.pop("flask", None)

    def test_retry_task_loads_saved_form_snapshot(self):
        root = Path(__file__).resolve().parents[1]
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))

        flask = types.ModuleType("flask")

        class DummyFlask:
            def __init__(self, *args, **kwargs):
                pass

            def route(self, *args, **kwargs):
                def deco(fn):
                    return fn
                return deco

        flask.Flask = DummyFlask
        flask.render_template_string = lambda *args, **kwargs: kwargs
        flask.request = types.SimpleNamespace(form={}, args={}, path="/retry/task-1", method="GET")
        flask.redirect = lambda value: value
        flask.url_for = lambda endpoint, **kwargs: f"/{endpoint}/{kwargs.get('task_id', '')}".rstrip("/")
        flask.send_from_directory = lambda *args, **kwargs: types.SimpleNamespace(headers={})
        flask.send_file = lambda *args, **kwargs: types.SimpleNamespace(headers={})
        flask.session = {}

        original_flask = sys.modules.get("flask")
        original_web_app = sys.modules.pop("web_app", None)
        sys.modules["flask"] = flask
        try:
            web_app = importlib.import_module("web_app")
            saved = {
                "client_id": "cid",
                "uid": "123",
                "c3vk": "vk",
                "api_key": "sk-test",
                "student_name": "张三",
                "school": "实验学校",
                "grade": "六年级",
                "max_passed": "5000",
                "max_failed": "1000",
            }
            web_app.get_task = lambda task_id: {"retry_form_json": json.dumps(saved, ensure_ascii=False)}
            web_app.render_index = lambda form=None, validation_result=None: {"form": form, "validation_result": validation_result}

            result = web_app.retry_task("task-1")

            self.assertEqual(result["form"]["client_id"], "cid")
            self.assertEqual(result["form"]["c3vk"], "vk")
            self.assertEqual(result["form"]["api_key"], "sk-test")
            self.assertEqual(result["form"]["student_name"], "张三")
        finally:
            sys.modules.pop("web_app", None)
            if original_web_app is not None:
                sys.modules["web_app"] = original_web_app
            if original_flask is not None:
                sys.modules["flask"] = original_flask
            else:
                sys.modules.pop("flask", None)
