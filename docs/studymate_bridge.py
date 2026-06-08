"""
studymate_bridge.py — v3.5 P0 stub

StudyMate 跳转桥接器（方案 A：URL 跳转）。
v3.5 P1 升级为 iframe 内嵌（方案 B）+ postMessage 通信。

本文件为占位实现，不依赖外部 HTTP 库。
完整集成见 docs/开发计划_v3.5.md § 3.2。
"""
from __future__ import annotations
import hashlib
import hmac
import time
from urllib.parse import urlencode, quote


STUDYMATE_BASE_URL = "https://studymate.example.com"  # 待上线后替换
STUDYMATE_SECRET = "REPLACE_WITH_HMAC_SECRET"  # 必须 env 注入
DEFAULT_LANG = "zh-CN"
SOURCE_TAG = "luogu"


def build_studymate_url(
    luogu_pid: str | int,
    *,
    student_id: int | None = None,
    gesp_level: int | None = None,
    exam_date: str | None = None,
    extra: dict | None = None,
) -> str:
    """
    构造 StudyMate 错题讲解页跳转 URL。

    Args:
        luogu_pid: 洛谷题号，如 "P1000" 或 1000
        student_id: 学员 ID（v3.5 P0 不传 PII 仅做 token 关联）
        gesp_level: 当前 GESP 等级（决定讲解深度）
        exam_date: 考试日期（token 有效期参考）
        extra: 额外参数（如 contest_id, source_url）

    Returns:
        完整 URL 字符串

    Examples:
        >>> build_studymate_url("P1000", gesp_level=5)
        'https://studymate.example.com/mistake?pid=P1000&lang=zh-CN&from=luogu&gesp_level=5&token=...'
    """
    params = {
        "pid": str(luogu_pid),
        "lang": DEFAULT_LANG,
        "from": SOURCE_TAG,
    }
    if gesp_level is not None:
        params["gesp_level"] = gesp_level

    # token = HMAC_SHA256(secret, student_id|exam_date|timestamp)
    ts = int(time.time())
    token_payload = f"{student_id or 'anon'}|{exam_date or 'unknown'}|{ts}"
    params["ts"] = ts
    if student_id is not None:
        params["student_id"] = student_id  # 不传真实姓名/手机号
    params["token"] = _sign(token_payload)

    if extra:
        params.update({k: v for k, v in extra.items() if k not in params})

    return f"{STUDYMATE_BASE_URL}/mistake?{urlencode(params, quote_via=quote)}"


def _sign(payload: str) -> str:
    """HMAC-SHA256 签名"""
    return hmac.new(
        STUDYMATE_SECRET.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verify_token(token: str, payload: str, max_age_seconds: int = 86400 * 30) -> bool:
    """
    验证 token（StudyMate 端调用，v3.5 P1 实装）。

    Args:
        token: URL 中的 token
        payload: 原始 payload（学生必须能复现）
        max_age_seconds: 默认 30 天有效

    Returns:
        是否合法且未过期
    """
    if not hmac.compare_digest(token, _sign(payload)):
        return False
    try:
        ts = int(payload.split("|")[-1])
    except (ValueError, IndexError):
        return False
    return (int(time.time()) - ts) <= max_age_seconds


# 错题本按钮 HTML 生成（v3.5 P0 用法）
def render_studymate_button(luogu_pid: str, *, gesp_level: int | None = None) -> str:
    """
    生成"AI 讲题"按钮 HTML 片段，嵌入报告 PDF/学员 Pro 页面。

    Returns:
        HTML 字符串
    """
    url = build_studymate_url(luogu_pid, gesp_level=gesp_level)
    return (
        f'<a href="{url}" target="_blank" rel="noopener noreferrer" '
        f'class="btn-studymate">🤖 AI 讲题（StudyMate）</a>'
    )


# -- 单测 / smoke test --
if __name__ == "__main__":
    url = build_studymate_url("P1000", student_id=42, gesp_level=5)
    assert "pid=P1000" in url
    assert "lang=zh-CN" in url
    assert "from=luogu" in url
    assert "gesp_level=5" in url
    assert "student_id=42" in url
    assert "token=" in url
    assert "ts=" in url
    print(f"[OK] studymate_bridge smoke test")
    print(f"     example url: {url[:80]}...")
