"""
verify_security.py — v3.5 Month 1 周末修复 smoke test

验证 3 项安全修复：
  1. task_store.DB_PATH 是绝对路径（不受 CWD 影响）
  2. web_app 在 admin 密码 / session secret 缺失时拒绝启动
  3. web_app 在 ALLOW_INSECURE_DEFAULT=1 时放行（带警告）

用法：
    python verify_security.py
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def test_db_path_is_absolute() -> None:
    """DB_PATH 默认值必须是绝对路径（CWD 不影响）"""
    import importlib
    if "task_store" in sys.modules:
        importlib.reload(sys.modules["task_store"])
    else:
        import task_store
    assert task_store.DB_PATH.is_absolute(), (
        f"DB_PATH 不是绝对路径：{task_store.DB_PATH}"
    )
    # 必须在项目根目录下（或者通过 TASK_DB_PATH 显式覆盖）
    expected_root = ROOT / "tasks.db"
    assert str(task_store.DB_PATH).lower() == str(expected_root).lower(), (
        f"DB_PATH 默认值不匹配：实际={task_store.DB_PATH} 期望={expected_root}"
    )
    print(f"  [OK] DB_PATH 是绝对路径：{task_store.DB_PATH}")


def _run_webapp_import(env_overrides: dict[str, str | None]) -> tuple[int, str, str]:
    """用受控 env 跑 `python -c "import web_app"`，返回 (returncode, stdout, stderr)

    注意：web_app 用 `load_dotenv(_ROOT / ".env")`，总是从项目根加载。
    所以要测"无 env" 场景，必须在 subprocess env 里**显式**覆盖 .env 加载的值。
    """
    env = os.environ.copy()
    # 清空所有与安全检查相关的 env
    for k in ("ADMIN_PASSWORD", "ADMIN_SESSION_SECRET", "FLASK_SECRET_KEY",
              "ALLOW_INSECURE_DEFAULT"):
        env.pop(k, None)
    for k, v in env_overrides.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v

    proc = subprocess.run(
        [sys.executable, "-c", "import web_app"],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_webapp_refuses_weak_overrides() -> None:
    """subprocess env 显式覆盖为弱值（覆盖 .env 加载），应被拒绝"""
    code, out, err = _run_webapp_import({
        "ADMIN_PASSWORD": "",                # 显式空，env_loader 不会用 .env 覆盖
        "ADMIN_SESSION_SECRET": "",          # 同上
    })
    assert code != 0, f"预期 web_app 拒绝启动，但 exit code = {code}"
    combined = out + err
    assert "安全基线检查失败" in combined, f"缺少 FATAL 提示：\n{combined}"
    assert "ADMIN_PASSWORD" in combined, f"缺少 ADMIN_PASSWORD 提示"
    assert "ADMIN_SESSION_SECRET" in combined, f"缺少 secret 提示"
    print(f"  [OK] 弱/空 env（覆盖 .env）拒绝启动（exit={code}）")


def test_webapp_refuses_known_weak_values() -> None:
    """显式设了已知弱默认，仍应拒绝"""
    code, out, err = _run_webapp_import({
        "ADMIN_PASSWORD": "change-me-now",
        "ADMIN_SESSION_SECRET": "luogu-ai-report-admin-secret-change-me",
    })
    assert code != 0, "弱默认应被拒绝"
    combined = out + err
    assert "已知弱默认" in combined or "安全基线检查失败" in combined
    print("  [OK] 弱默认密码/secret 仍被拒绝")


def test_webapp_refuses_admin_password() -> None:
    """只设了 secret，admin 密码仍应触发拒绝"""
    code, out, err = _run_webapp_import({
        "ADMIN_PASSWORD": "admin",  # 在弱默认名单中
        "ADMIN_SESSION_SECRET": "x" * 64,  # 强 secret
    })
    assert code != 0
    combined = out + err
    assert "ADMIN_PASSWORD" in combined
    print("  [OK] 弱 admin 密码被拒绝（即使 secret 强）")


def test_webapp_allows_escape_hatch() -> None:
    """ALLOW_INSECURE_DEFAULT=1 放行（带警告）"""
    code, out, err = _run_webapp_import({
        "ADMIN_PASSWORD": "",  # 强制弱，触发检查
        "ADMIN_SESSION_SECRET": "",
        "ALLOW_INSECURE_DEFAULT": "1",
    })
    combined = out + err
    assert "ALLOW_INSECURE_DEFAULT=1 已启用" in combined, (
        f"未看到逃逸提示：\n{combined[:500]}"
    )
    print("  [OK] ALLOW_INSECURE_DEFAULT=1 放行（带警告）")


def test_webapp_with_dot_env_works() -> None:
    """现有 .env 已设强凭据 → 正常启动（验证不破坏现有部署）"""
    code, out, err = _run_webapp_import({})
    # .env 有强凭据，应通过安全检查并正常 import
    assert code == 0, (
        f"现有 .env 应能正常启动，但 exit code = {code}\n"
        f"stdout: {out[:300]}\nstderr: {err[:300]}"
    )
    print("  [OK] 现有 .env（强凭据）正常启动")


def main() -> int:
    print("=" * 60)
    print("[SMOKE] v3.5 Month 1 周末安全修复验证")
    print("=" * 60)
    print()
    print("[1/5] DB_PATH 绝对路径")
    test_db_path_is_absolute()
    print()
    print("[2/5] 显式空/弱 env（覆盖 .env）拒绝启动")
    test_webapp_refuses_weak_overrides()
    print()
    print("[3/5] 弱默认密码/secret 拒绝启动")
    test_webapp_refuses_known_weak_values()
    print()
    print("[4/5] ALLOW_INSECURE_DEFAULT 逃逸通道")
    test_webapp_allows_escape_hatch()
    print()
    print("[5/5] 现有 .env 强凭据不破坏启动")
    test_webapp_with_dot_env_works()
    print()
    print("=" * 60)
    print("[OK] 全部 5 项安全检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
