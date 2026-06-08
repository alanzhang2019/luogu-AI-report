# -*- coding: utf-8 -*-
"""Smoke check: 封闭测试指引_真实教练30天.md 关键交叉引用与 v3.5 §7-§10 对位。"""
from pathlib import Path

DOC = Path(__file__).resolve().parent / "docs" / "封闭测试指引_真实教练30天.md"
V35 = Path(__file__).resolve().parent / "docs" / "开发计划_v3.5.md"

content = DOC.read_text(encoding="utf-8")
print(f"[OK] 文档存在: {DOC.relative_to(DOC.parents[1])}  ({len(content)} 字符, {content.count(chr(10))} 行)")

checks = {
    "v3.5 §7.1 引用": "§7.1" in content,
    "v3.5 §10 引用": "§10" in content,
    "v3.5 §11 反向 Scope": "§11" in content,
    "1 教练 + 5 学员 + 30 天": "1 真实教练" in content and "5 学员" in content and "30 天" in content,
    "NDA 流程": "NDA" in content,
    "NPS 问卷 (0-10)": "NPS" in content and "0-10" in content,
    "6 项验收门槛": content.count("≥") >= 6,
    "D-Day ~ D+30 时间表": "D-Day" in content and "D+30" in content,
    "退出条件": "退出条件" in content,
    "风险预案": "风险预案" in content,
    "数据采集 (自动 + 手动)": "数据采集" in content and "自动埋点" in content,
    "每日日志模板 (附录 A)": "Coach Daily Log" in content,
    "30 天报告模板 (附录 B)": "30 天封闭测试报告" in content,
    "反向 Scope 提醒": "反向 Scope 提醒" in content,
    "PIPL 合规红线": "PIPL" in content,
    "StudyMate 降级预案": "降级" in content,
    "政策日历过期水印": "政策数据可能与实际有偏差" in content,
    "4 SKU 激活码就绪 (3-3 §2.3)": "4 SKU 激活码" in content,
}
print("=== 关键章节检查 ===")
for k, v in checks.items():
    mark = "x" if v else " "
    print(f"  [{mark}] {k}")
all_ok = all(checks.values())
print(f"\n=== 结果: {'全部 OK' if all_ok else '有缺失'} ===")

# 交叉验证 v3.5 计划内容确实存在这些验收门槛
v35 = V35.read_text(encoding="utf-8")
v35_checks = {
    "v3.5 §7.1 真实教练 30 天": "1 真实教练 + 5 学员 + 30 天" in v35,
    "v3.5 §10 Month 3 末验收": "真实教练 30 天试用" in v35,
    "v3.5 §11 反向 Scope": "在线支付" in v35,
}
print("\n=== v3.5 计划交叉验证 ===")
for k, v in v35_checks.items():
    mark = "x" if v else " "
    print(f"  [{mark}] {k}")
