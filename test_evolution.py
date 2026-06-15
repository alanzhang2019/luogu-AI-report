# -*- coding: utf-8 -*-
"""v3.9.39 提交代码考古本地测试

不调用真实 luogu API（mock），验证：
  1. analyze_submission_evolution 正确挑选 TOP N 题
  2. status_timeline / code_length_timeline 拼得对
  3. evolution_to_prompt_block 输出格式 OK
  4. 降级路径（luogu 报错时不影响主流程）
"""
import sys
import os
from unittest.mock import MagicMock

# 模拟 3 道题的提交历史（含多版源码 diff）
MOCK_RECORDS = [
    # ===== 题目 1：P1001 · A+B Problem（5 次提交，3 次 WA → 1 次 TLE → AC）=====
    # 考古价值：高（多次失败+最终AC+状态变迁丰富）
    {
        "id": 1001, "submitTime": 1740000000, "status": 9,  # WA
        "problem": {"pid": "P1001", "title": "A+B Problem", "difficulty": 1, "tags": ["入门", "模拟"]},
        "sourceCodeLength": 200, "language": 3, "time": 100, "memory": 1024, "score": 0,
    },
    {
        "id": 1002, "submitTime": 1740001000, "status": 9,  # WA
        "problem": {"pid": "P1001", "title": "A+B Problem", "difficulty": 1, "tags": ["入门", "模拟"]},
        "sourceCodeLength": 220, "language": 3, "time": 100, "memory": 1024, "score": 0,
    },
    {
        "id": 1003, "submitTime": 1740002000, "status": 8,  # TLE
        "problem": {"pid": "P1001", "title": "A+B Problem", "difficulty": 1, "tags": ["入门", "模拟"]},
        "sourceCodeLength": 230, "language": 3, "time": 2000, "memory": 1024, "score": 60,
    },
    {
        "id": 1004, "submitTime": 1740003000, "status": 9,  # WA
        "problem": {"pid": "P1001", "title": "A+B Problem", "difficulty": 1, "tags": ["入门", "模拟"]},
        "sourceCodeLength": 250, "language": 3, "time": 100, "memory": 1024, "score": 0,
    },
    {
        "id": 1005, "submitTime": 1740004000, "status": 12,  # AC
        "problem": {"pid": "P1001", "title": "A+B Problem", "difficulty": 1, "tags": ["入门", "模拟"]},
        "sourceCodeLength": 240, "language": 3, "time": 50, "memory": 1024, "score": 100,
    },
    # ===== 题目 2：P2002 · 排序（4 次提交，全是 RE → RE → RE → RE，未AC）=====
    # 考古价值：高（多次失败+未AC+状态不变）
    {
        "id": 2001, "submitTime": 1740010000, "status": 10,  # RE
        "problem": {"pid": "P2002", "title": "排序", "difficulty": 2, "tags": ["排序"]},
        "sourceCodeLength": 500, "language": 3, "time": 0, "memory": 0, "score": 0,
    },
    {
        "id": 2002, "submitTime": 1740011000, "status": 10,  # RE
        "problem": {"pid": "P2002", "title": "排序", "difficulty": 2, "tags": ["排序"]},
        "sourceCodeLength": 520, "language": 3, "time": 0, "memory": 0, "score": 0,
    },
    {
        "id": 2003, "submitTime": 1740012000, "status": 10,  # RE
        "problem": {"pid": "P2002", "title": "排序", "difficulty": 2, "tags": ["排序"]},
        "sourceCodeLength": 550, "language": 3, "time": 0, "memory": 0, "score": 0,
    },
    {
        "id": 2004, "submitTime": 1740013000, "status": 10,  # RE
        "problem": {"pid": "P2002", "title": "排序", "difficulty": 2, "tags": ["排序"]},
        "sourceCodeLength": 580, "language": 3, "time": 0, "memory": 0, "score": 0,
    },
    # ===== 题目 3：P3003 · 二分（2 次提交，1 次 WA → AC）=====
    # 考古价值：低（仅 2 次）
    {
        "id": 3001, "submitTime": 1740020000, "status": 9,  # WA
        "problem": {"pid": "P3003", "title": "二分查找", "difficulty": 3, "tags": ["二分"]},
        "sourceCodeLength": 800, "language": 3, "time": 100, "memory": 2048, "score": 0,
    },
    {
        "id": 3002, "submitTime": 1740021000, "status": 12,  # AC
        "problem": {"pid": "P3003", "title": "二分查找", "difficulty": 3, "tags": ["二分"]},
        "sourceCodeLength": 820, "language": 3, "time": 80, "memory": 2048, "score": 100,
    },
    # ===== 题目 4：P4004 · DP（仅 1 次提交，不应该被选）=====
    {
        "id": 4001, "submitTime": 1740030000, "status": 12,  # AC 1 次
        "problem": {"pid": "P4004", "title": "DP 入门", "difficulty": 4, "tags": ["DP"]},
        "sourceCodeLength": 1500, "language": 3, "time": 200, "memory": 4096, "score": 100,
    },
]

# 模拟 get_record 返回的源码（带 diff）
MOCK_SOURCE_BY_RID = {
    1001: "int main() {\n    int a, b;\n    scanf(\"%d %d\", &a, &b);\n    return a - b;  // bug: 写成减法\n}\n",
    1002: "int main() {\n    int a, b;\n    scanf(\"%d%d\", &a, &b);  // 删了空格\n    return a - b;  // 还是减法\n}\n",
    1003: "int main() {\n    ios::sync_with_stdio(false);  // 加了 I/O 优化\n    int a, b;\n    cin >> a >> b;\n    return a - b;  // 忘了改算法\n}\n",
    1004: "int main() {\n    ios::sync_with_stdio(false);\n    int a, b;\n    cin >> a >> b;\n    cout << a + b;  // 终于发现是 a - b\n    return 0;\n}\n",
    1005: "int main() {\n    ios::sync_with_stdio(false);\n    int a, b;\n    cin >> a >> b;\n    cout << a + b << endl;  // 加了换行\n    return 0;\n}\n",
    2001: "void sort(int a[], int n) {\n    for (int i = 0; i < n; i++)\n        for (int j = 0; j < n; j++)\n            if (a[i] < a[j]) swap(a[i], a[j]);  // RE：栈溢出\n}\n",
    2002: "void sort(int a[], int n) {\n    for (int i = 0; i < n; i++)\n        for (int j = 0; j < n-i; j++)\n            if (a[j] < a[j+1]) swap(a[j], a[j+1]);  // 还是 RE\n}\n",
    2003: "void sort(int a[], int n) {\n    for (int i = 0; i < n-1; i++)\n        for (int j = 0; j < n-1-i; j++)\n            if (a[j] > a[j+1]) swap(a[j], a[j+1]);  // 还是 RE\n}\n",
    2004: "// 还是冒泡\nvoid sort(int a[], int n) {\n    for (int i = 0; i < n-1; i++)\n        for (int j = 0; j < n-1-i; j++)\n            if (a[j] > a[j+1]) swap(a[j], a[j+1]);\n}\n",
    3001: "int bsearch(int a[], int n, int x) {\n    int l = 0, r = n;\n    while (l < r) {\n        int m = (l + r) / 2;\n        if (a[m] < x) l = m;\n        else r = m;\n    }\n    return l;  // bug: 漏了 +1\n}\n",
    3002: "int bsearch(int a[], int n, int x) {\n    int l = 0, r = n;\n    while (l < r) {\n        int m = (l + r) / 2;\n        if (a[m] < x) l = m + 1;  // 修正\n        else r = m;\n    }\n    return l;\n}\n",
}


def make_mock_luogu():
    """模拟 luogu 客户端"""
    mock = MagicMock()

    def get_record(rid: str):
        rid_int = int(rid)
        source = MOCK_SOURCE_BY_RID.get(rid_int, "// no source")
        # 模拟 record 对象（支持 to_json）
        rec = MagicMock()
        rec.to_json.return_value = {
            "id": rid_int,
            "sourceCode": source,
            "sourceCodeLength": len(source),
            "status": MOCK_RECORDS[[r["id"] for r in MOCK_RECORDS].index(rid_int)]["status"] if rid_int in [r["id"] for r in MOCK_RECORDS] else 0,
            "submitTime": 0,
        }
        return rec

    mock.get_record.side_effect = get_record
    return mock


print("=" * 70)
print("  v3.9.39 提交代码考古测试")
print("=" * 70)

# ===== 1. 选 TOP N 题 =====
from submission_evolution import analyze_submission_evolution, evolution_to_prompt_block, _evolution_score

luogu = make_mock_luogu()
result = analyze_submission_evolution(luogu, uid=999, records=MOCK_RECORDS, top_n=3, verbose=True)

print(f"\n[1] 选 TOP 3 题（按 evolution_score 排序）")
print(f"    summary: {result['summary']}")
selected = result["selected_problems"]
print(f"    selected count: {len(selected)}")
for p in selected:
    print(f"    · {p['pid']} ({p['title']}) · attempts={p['attempts']} · "
          f"is_ac={p['is_accepted']} · score={p['evolution_score']:.1f}")
    print(f"      status_timeline: {p['status_timeline']}")
    print(f"      code_length_timeline: {p['code_length_timeline']}")
    print(f"      versions: {len(p['versions'])}")

# 验证：
# - P1001 (5 次，最终 AC，状态变迁丰富) 应该是 #1
# - P2002 (4 次，未 AC，全 RE) 应该是 #2
# - P3003 (2 次，AC) 应该是 #3
# - P4004 (1 次) 不应该被选
assert len(selected) == 3, f"应选 3 道，实际 {len(selected)}"
assert selected[0]["pid"] == "P1001", f"第 1 名应是 P1001，实际 {selected[0]['pid']}"
assert selected[1]["pid"] == "P2002", f"第 2 名应是 P2002，实际 {selected[1]['pid']}"
assert selected[2]["pid"] == "P3003", f"第 3 名应是 P3003，实际 {selected[2]['pid']}"
assert "P4004" not in [p["pid"] for p in selected], "P4004 只有 1 次，不应入选"
print("    ✓ TOP 3 排序正确，P4004 正确排除")

# ===== 2. status_timeline 格式 =====
print(f"\n[2] status_timeline 格式")
expected_timeline = "v1:WA → v2:WA → v3:TLE → v4:WA → v5:AC"
assert selected[0]["status_timeline"] == expected_timeline, \
    f"P1001 时间线错：{selected[0]['status_timeline']} != {expected_timeline}"
print(f"    P1001: {selected[0]['status_timeline']}")
print(f"    ✓ 拼写正确（v1:WA → v2:WA → ... → v5:AC）")

# ===== 3. diff 数据 =====
print(f"\n[3] diff 数据")
p1001_diffs = selected[0]["diffs"]
assert len(p1001_diffs) == 4, f"P1001 应有 4 个 diff (5 版)，实际 {len(p1001_diffs)}"
for d in p1001_diffs:
    print(f"    v{d['v_from']}({d['from_status']}) → v{d['v_to']}({d['to_status']}): "
          f"行数 +{d['lines_added']}/-{d['lines_removed']} 字节 {d['byte_delta']:+d}")
print("    ✓ 4 个 diff 都生成了")

# ===== 4. prompt block 输出 =====
print(f"\n[4] evolution_to_prompt_block 输出")
prompt = evolution_to_prompt_block(result)
print(f"    prompt 长度: {len(prompt)} 字符")
print(f"    前 500 字符:\n{'-' * 40}\n{prompt[:500]}\n{'-' * 40}")
# 验证关键内容
assert "P1001" in prompt
assert "P2002" in prompt
assert "P3003" in prompt
assert "diff 时间线" in prompt or "逐版" in prompt
assert "状态变迁" in prompt
assert "AC" in prompt or "未AC" in prompt
print("    ✓ prompt 块格式正确（包含 P1001/P2002/P3003、状态变迁、关键代码片段）")

# ===== 5. 降级路径（luogu 抛错时）=====
print(f"\n[5] 降级路径（luogu 抛错时）")
bad_luogu = MagicMock()
bad_luogu.get_record.side_effect = Exception("网络超时")
result2 = analyze_submission_evolution(bad_luogu, uid=999, records=MOCK_RECORDS, top_n=3, verbose=False)
print(f"    summary: {result2['summary']}")
print(f"    selected: {len(result2['selected_problems'])}")
# 即使 luogu 失败，题目的元数据还能展示（只是 code_head/tail 缺失）
for p in result2["selected_problems"]:
    print(f"    · {p['pid']} · versions={len(p['versions'])} · "
          f"first v _error: {p['versions'][0].get('_error', 'N/A')}")
assert len(result2["selected_problems"]) == 3, "降级路径仍应选 TOP 3"
print("    ✓ 降级路径正常工作（luogu 失败不影响题目筛选，只是源码缺）")

# ===== 6. 空 records =====
print(f"\n[6] 空 records 降级")
result3 = analyze_submission_evolution(luogu, uid=999, records=[], top_n=5)
print(f"    selected: {len(result3['selected_problems'])}")
assert len(result3["selected_problems"]) == 0
print("    ✓ 空 records 返回空 selected")

print()
print("=" * 70)
print("  ✓ 6/6 测试通过")
print("=" * 70)
