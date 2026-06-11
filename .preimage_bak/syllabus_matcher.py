"""
NOI 大纲知识点对标模块
实现五级分类（精通/熟练/入门/初窥/空白）和覆盖率统计
"""

from pathlib import Path
import re
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent
SYLLABUS_2025_PDF = PROJECT_ROOT / "NOI大纲_Syllabus_Edition_2025.pdf"
SYLLABUS_2025_TEXT = PROJECT_ROOT / "NOI大纲_Syllabus_Edition_2025.pdf.txt"

# CSP-J 知识点定义 (入门级)
CSP_J_TOPICS = {
    "枚举法": ["枚举"],
    "模拟法": ["模拟"],
    "贪心法": ["贪心"],
    "递推法": ["递推"],
    "递归法": ["递归"],
    "二分法": ["二分"],
    "倍增法": ["倍增"],
    "前缀和": ["前缀和"],
    "差分": ["差分"],
    "排序": ["排序"],
    "DFS": ["dfs", "深度优先"],
    "BFS": ["bfs", "广度优先"],
    "Flood Fill": ["flood", "填充"],
    "DP基础": ["dp", "动态规划"],
    "背包DP": ["背包"],
    "区间DP": ["区间dp"],
    "图遍历": ["图遍历", "图的遍历"],
    "链表": ["链表"],
    "栈": ["栈"],
    "队列": ["队列"],
    "二叉树遍历": ["二叉树"],
    "哈夫曼树": ["哈夫曼"],
    "BST": ["二叉搜索树", "bst"],
    "GCD": ["gcd", "最大公约数", "辗转相除"],
    "素数筛法": ["筛法", "素数", "质数"],
    "排列组合": ["排列", "组合"],
    "杨辉三角": ["杨辉"],
    "进制转换": ["进制"],
}

# CSP-S 知识点定义 (提高级)
CSP_S_TOPICS = {
    "位集合bitset": ["bitset", "位集合"],
    "单调栈/队列": ["单调栈", "单调队列"],
    "双端队列": ["双端队列"],
    "ST表": ["st表", "稀疏表"],
    "并查集": ["并查集"],
    "树状数组": ["树状数组", "bit"],
    "线段树": ["线段树"],
    "优先队列/堆": ["堆", "优先队列"],
    "字典树Trie": ["trie", "字典树"],
    "笛卡尔树": ["笛卡尔树"],
    "平衡树": ["平衡树", "treap", "splay"],
    "二分图": ["二分图"],
    "欧拉图": ["欧拉"],
    "强连通图": ["强连通"],
    "分治": ["分治"],
    "离散化": ["离散化"],
    "扫描线": ["扫描线"],
    "归并排序": ["归并排序"],
    "快速排序": ["快速排序"],
    "KMP": ["kmp"],
    "Manacher": ["manacher"],
    "剪枝优化": ["剪枝"],
    "记忆化搜索": ["记忆化"],
    "启发式搜索": ["启发式", "a*"],
    "双向BFS": ["双向bfs"],
    "迭代加深": ["迭代加深"],
    "树形DP": ["树形dp"],
    "状压DP": ["状压"],
    "多维DP": ["多维dp"],
    "DP优化": ["dp优化", "单调队列优化", "斜率优化"],
    "最小生成树": ["最小生成树", "mst"],
    "最短路": ["最短路", "dijkstra", "spfa", "bellman"],
    "Floyd": ["floyd"],
    "拓扑排序": ["拓扑"],
    "单源次短路": ["次短路"],
    "欧拉道路": ["欧拉道路"],
    "同余式": ["同余"],
    "欧拉定理/函数": ["欧拉函数", "欧拉定理"],
    "费马小定理": ["费马"],
    "逆元": ["逆元"],
    "扩展欧几里得": ["扩展欧几里得", "exgcd"],
    "中国剩余定理": ["crt", "中国剩余定理"],
    "快速幂": ["快速幂"],
    "容斥原理": ["容斥"],
    "卡特兰数": ["卡特兰"],
    "矩阵运算": ["矩阵"],
    "高斯消元": ["高斯消元"],
    "鸽巢原理": ["鸽巢"],
    "二项式定理": ["二项式"],
    "错排列/圆排列": ["错排", "圆排列"],
}

# 省选级知识点
PROVINCIAL_TOPICS = {
    "匈牙利算法": ["匈牙利", "二分图匹配"],
    "网络流": ["网络流", "最大流", "费用流", "dinic", "ek"],
    "分块": ["分块", "莫队"],
    "Nim博弈/SG": ["nim", "sg函数", "博弈"],
    "离线处理": ["离线"],
    "可持久化线段树": ["可持久化", "主席树"],
    "块状链表": ["块状链表"],
    "随机化": ["随机化", "蒙特卡洛"],
    "复杂DP模型": ["插头dp", "轮廓线dp"],
    "构造": ["构造"],
}

# NOI级知识点
NOI_TOPICS = {
    "后缀数组": ["后缀数组", "sa"],
    "后缀自动机": ["后缀自动机", "sam"],
    "AC自动机": ["ac自动机"],
    "树链剖分": ["树链剖分", "树剖"],
    "LCT": ["lct", "link-cut"],
    "CDQ分治": ["cdq"],
    "点分治": ["点分治"],
    "虚树": ["虚树"],
    "FFT/NTT": ["fft", "ntt", "多项式"],
    "FWT": ["fwt"],
    "斜率优化DP": ["斜率优化"],
    "四边形不等式": ["四边形不等式"],
    "基与线性基": ["线性基", "基"],
    "Pollard-Rho": ["pollard", "rho"],
    "Miller-Rabin": ["miller", "rabin"],
    "杜教筛": ["杜教筛"],
    "Min_25筛": ["min_25"],
    "圆方树": ["圆方树"],
    "2-SAT": ["2-sat"],
    "差分约束": ["差分约束"],
    "无源汇上下界": ["上下界"],
    "有源汇上下界": ["有源汇"],
    "KM算法": ["km算法"],
    "一般图匹配": ["一般图匹配", "带花树"],
    "莫比乌斯反演": ["莫比乌斯", "反演"],
    "Burnside引理": ["burnside"],
    "Polya定理": ["polya"],
    "原根": ["原根"],
    "BSGS": ["bsgs", "大步小步"],
    "扩展BSGS": ["扩展bsgs"],
    "单位根反演": ["单位根"],
    "单纯形": ["单纯形"],
    "计算几何基础": ["计算几何", "凸包", "叉积"],
    "半平面交": ["半平面交"],
    "旋转卡壳": ["旋转卡壳"],
    "扫描线(几何)": ["扫描线"],
    "k-d树": ["kd树", "k-d"],
    "替罪羊树": ["替罪羊"],
    "Treap": ["treap"],
    "Splay": ["splay"],
    "WBLT": ["wblt"],
    "析合树": ["析合树"],
    "笛卡尔树(高级)": ["笛卡尔树"],
}


def _normalize_syllabus_text(raw_text: str) -> str:
    text = (raw_text or "").replace("\x00", "")
    raw_lines = [line.strip() for line in text.splitlines()]

    def _is_noise_line(line: str) -> bool:
        if not line:
            return True
        if line in {
            "LoN",
            "OV",
            "NOI",
            ">>>>",
            "CHINA COMPUTERFEDERATION",
            "CHINACOMPUTERFEDERATION",
            "CCFNOI科学委员会全体审定",
            "中国计耳栈学会",
            "中国計算栈学会",
            "全国青少年信息学奥林匹克系列竞赛大纲",
        }:
            return True
        if line.startswith("Email:") or line.startswith("网址：http"):
            return True
        if re.fullmatch(r"[A-Za-z][A-Za-z\s]{0,30}", line):
            return True
        if re.fullmatch(r"\d+", line):
            return True
        return False

    def _is_new_block(line: str) -> bool:
        if re.match(r"^[（(]\d+[）)]", line):
            return True
        if re.match(r"^\d+(?:\.\d+)*\s*", line):
            return True
        if re.match(r"^[·•\-]", line):
            return True
        if line in {"序", "目录", "简介", "内容", "附录", "索引"}:
            return True
        return False

    filtered_lines: list[str] = []
    skip_preface_credit_block = False
    kept_titles: set[str] = set()
    for line in raw_lines:
        if _is_noise_line(line):
            continue
        if re.match(r"^20(?:21|23|25)版(?:修订|调研|起草|审阅)：", line):
            skip_preface_credit_block = True
            continue
        if skip_preface_credit_block:
            if line in {"2025年4月", "日期：2025-4-15", "目录"}:
                skip_preface_credit_block = False
            else:
                continue
        # 去掉 OCR 误识别的多余空格与英文拼写断裂。
        line = re.sub(r"\s+", " ", line).strip()
        line = line.replace("OlympiadinInformatics", "Olympiad in Informatics")
        line = line.replace("Olympiad inInformatics", "Olympiad in Informatics")
        line = line.replace("ChinaTeam", "China Team")
        line = line.replace("NOl级", "NOI级")

        if line.startswith("序") and len(line) > 3:
            filtered_lines.append("序")
            line = line[1:].strip()

        if re.match(r"^全国青少年信息学奥林匹克系列竞赛大纲[（(].*版[）)]?$", line):
            if "title-main" in kept_titles:
                continue
            kept_titles.add("title-main")
        elif line == "全国青少年信息学奥林匹克竞赛（CCF NOI）":
            if "title-sub" in kept_titles:
                continue
            kept_titles.add("title-sub")

        if line:
            filtered_lines.append(line)

    merged_lines: list[str] = []
    for line in filtered_lines:
        if not merged_lines:
            merged_lines.append(line)
            continue

        prev = merged_lines[-1]
        if (
            not _is_new_block(line)
            and not _is_new_block(prev)
            and not re.search(r"[。！？；：:）)]$", prev)
        ):
            merged_lines[-1] = prev + line
        else:
            merged_lines.append(line)

    compact = "\n".join(line for line in merged_lines if line)
    compact = re.sub(r"\n{3,}", "\n\n", compact)
    return compact.strip()


def _extract_text_with_pypdf(pdf_path: Path) -> str:
    try:
        from pypdf import PdfReader
    except Exception:
        return ""

    try:
        reader = PdfReader(str(pdf_path))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception:
        return ""


def _extract_text_with_pymupdf(pdf_path: Path) -> str:
    try:
        import fitz
    except Exception:
        return ""

    try:
        doc = fitz.open(str(pdf_path))
        return "\n".join(doc.load_page(i).get_text() for i in range(doc.page_count))
    except Exception:
        return ""


def _extract_text_with_ocr(pdf_path: Path) -> str:
    """
    针对扫描版 PDF 的 OCR 回退方案。
    依赖: pymupdf + rapidocr_onnxruntime + numpy
    """
    try:
        import fitz
        import numpy as np
        from rapidocr_onnxruntime import RapidOCR
    except Exception:
        return ""

    try:
        doc = fitz.open(str(pdf_path))
        engine = RapidOCR()
        page_texts: list[str] = []

        for page_index in range(doc.page_count):
            page = doc.load_page(page_index)
            # 2x 放大提升 OCR 效果，同时保持可接受的处理时间。
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
            result, _ = engine(img)
            lines = [str(item[1]).strip() for item in (result or []) if len(item) > 1 and str(item[1]).strip()]
            if lines:
                page_texts.append("\n".join(lines))

        return "\n\n".join(page_texts)
    except Exception:
        return ""


def load_syllabus_context(max_chars: int = 20000) -> dict[str, Any]:
    """
    优先使用 2025 大纲文本缓存；若缓存不存在，则尝试从 PDF 自动提取并生成缓存。
    返回内容与来源元数据，便于报告中明确标注数据来源。
    """
    if SYLLABUS_2025_TEXT.exists():
        raw_cached = SYLLABUS_2025_TEXT.read_text(encoding="utf-8", errors="replace")
        content = _normalize_syllabus_text(raw_cached)
        if content:
            if content != raw_cached.strip():
                SYLLABUS_2025_TEXT.write_text(content, encoding="utf-8")
            return {
                "content": content[:max_chars],
                "source": "txt-cache",
                "path": str(SYLLABUS_2025_TEXT),
                "full_length": len(content),
            }

    if SYLLABUS_2025_PDF.exists():
        for extractor_name, extractor in (
            ("pypdf", _extract_text_with_pypdf),
            ("pymupdf", _extract_text_with_pymupdf),
            ("ocr-rapidocr", _extract_text_with_ocr),
        ):
            extracted = _normalize_syllabus_text(extractor(SYLLABUS_2025_PDF))
            if len(extracted) >= 500:
                SYLLABUS_2025_TEXT.write_text(extracted, encoding="utf-8")
                return {
                    "content": extracted[:max_chars],
                    "source": f"pdf-extracted:{extractor_name}",
                    "path": str(SYLLABUS_2025_PDF),
                    "full_length": len(extracted),
                }
        return {
            "content": "",
            "source": "pdf-unextractable",
            "path": str(SYLLABUS_2025_PDF),
            "full_length": 0,
        }

    return {"content": "", "source": "missing", "path": str(SYLLABUS_2025_PDF), "full_length": 0}


def _match_topic(tag_name: str, keywords: list[str]) -> bool:
    """检查标签名是否匹配知识点的关键词"""
    tag_lower = tag_name.lower()
    for kw in keywords:
        if kw.lower() in tag_lower:
            return True
    return False


def _count_topic_ac(top_tags: list[dict], topic_keywords: list[str]) -> int:
    """统计某知识点对应的 AC 题数"""
    total = 0
    for item in top_tags:
        tag_name = str(item.get("name") or "")
        count = int(item.get("count", 0))
        if _match_topic(tag_name, topic_keywords):
            total += count
    return total


def _compute_topic_difficulty(
    top_tags: list[dict],
    topic_keywords: list[str],
    tag_difficulty_map: dict[str, list[int]] | None = None,
) -> int:
    """统计某知识点对应的平均难度（1-7，0 表示无数据）。

    算法：
    1. 优先用 tag_difficulty_map（外部传入的真实题目难度聚合），
       找出所有匹配本知识点的标签，取所有题目的平均 difficulty。
    2. 退回：从 top_tags 里的 difficulty 字段取均值（如果有）。
    3. 都没有 → 0。
    """
    matched_diffs: list[int] = []

    # 方式 1：用外部传入的 tag → 难度列表
    if tag_difficulty_map:
        for tag_name, diffs in tag_difficulty_map.items():
            if _match_topic(tag_name, topic_keywords):
                matched_diffs.extend(int(d) for d in diffs if d and d > 0)

    # 方式 2：从 top_tags 自身取 difficulty 字段（容错）
    if not matched_diffs:
        for item in top_tags:
            tag_name = str(item.get("name") or "")
            if not _match_topic(tag_name, topic_keywords):
                continue
            d = item.get("difficulty")
            if d is None:
                continue
            try:
                di = int(d)
                if di > 0:
                    matched_diffs.append(di)
            except (TypeError, ValueError):
                continue

    if not matched_diffs:
        return 0
    # 四舍五入到最近的整数（1-7）
    avg = sum(matched_diffs) / len(matched_diffs)
    rounded = int(round(avg))
    return max(1, min(7, rounded))


def evaluate_topic_level(ac_count: int) -> str:
    """根据 AC 题数评定掌握等级"""
    if ac_count >= 20:
        return "🟢 精通"
    elif ac_count >= 10:
        return "🟡 熟练"
    elif ac_count >= 3:
        return "🟠 入门"
    elif ac_count >= 1:
        return "🔵 初窥"
    else:
        return "🔴 空白"


def evaluate_all_topics(
    top_tags: list[dict],
    tag_difficulty_map: dict[str, list[int]] | None = None,
) -> dict[str, Any]:
    """
    对所有知识点进行评估，返回分级统计结果

    Parameters
    ----------
    top_tags : list[dict]
        标签聚合数据 [{name, count, ...}, ...]
    tag_difficulty_map : dict[str, list[int]], optional
        外部传入的 tag → 题目难度列表（1-7），用于计算每个知识点的平均难度。
        没有则 difficulty=0。
    """
    def _evaluate_group(topics: dict[str, list[str]]) -> dict[str, Any]:
        results = []
        green = yellow = orange = blue = red = 0
        total_ac = 0
        for topic_name, keywords in topics.items():
            ac = _count_topic_ac(top_tags, keywords)
            diff = _compute_topic_difficulty(top_tags, keywords, tag_difficulty_map)
            level = evaluate_topic_level(ac)
            results.append({
                "topic": topic_name,
                "ac_count": ac,
                "level": level,
                "difficulty": diff,
            })
            if "精通" in level:
                green += 1
            elif "熟练" in level:
                yellow += 1
            elif "入门" in level:
                orange += 1
            elif "初窥" in level:
                blue += 1
            else:
                red += 1
            total_ac += ac
        return {
            "details": sorted(results, key=lambda x: x["ac_count"], reverse=True),
            "stats": {
                "total": len(topics),
                "精通": green,
                "熟练": yellow,
                "入门": orange,
                "初窥": blue,
                "空白": red,
            },
            "coverage": round((len(topics) - red) / len(topics) * 100, 1) if topics else 0,
            "total_ac": total_ac,
        }

    csp_j = _evaluate_group(CSP_J_TOPICS)
    csp_s = _evaluate_group(CSP_S_TOPICS)
    provincial = _evaluate_group(PROVINCIAL_TOPICS)
    noi = _evaluate_group(NOI_TOPICS)

    all_total = len(CSP_J_TOPICS) + len(CSP_S_TOPICS) + len(PROVINCIAL_TOPICS) + len(NOI_TOPICS)
    all_red = csp_j["stats"]["空白"] + csp_s["stats"]["空白"] + provincial["stats"]["空白"] + noi["stats"]["空白"]
    all_coverage = round((all_total - all_red) / all_total * 100, 1) if all_total else 0

    return {
        "csp_j": csp_j,
        "csp_s": csp_s,
        "provincial": provincial,
        "noi": noi,
        "overall": {
            "total_topics": all_total,
            "covered_topics": all_total - all_red,
            "coverage_rate": all_coverage,
        },
        "source": load_syllabus_context(max_chars=0),
    }


def format_syllabus_report(evaluation: dict[str, Any]) -> str:
    """将大纲评估结果格式化为 Markdown 报告"""
    lines = []
    lines.append("## CSP-NOI 2025 大纲知识点对标")
    lines.append("")
    source_info = evaluation.get("source", {}) or {}
    source_name_map = {
        "txt-cache": "2025 大纲文本缓存",
        "pdf-unextractable": "2025 大纲 PDF（未能自动提取正文，当前按内置 2025 主题表匹配）",
        "missing": "未找到 2025 大纲文件",
    }
    source_label = source_name_map.get(source_info.get("source"), source_info.get("source", "未知来源"))
    lines.append(f"**当前考纲来源**: {source_label}")
    if source_info.get("path"):
        lines.append(f"**来源路径**: {source_info.get('path')}")
    lines.append("")
    lines.append("**口径说明**: 本节只根据题目的算法标签评估知识点覆盖，不直接等同于做过某级别来源题。题目级别经历请以下方程序统计表为准。")
    lines.append("")
    lines.append("评级标准: 🟢 精通(20+) | 🟡 熟练(10-19) | 🟠 入门(3-9) | 🔵 初窥(1-2) | 🔴 空白(0)")
    lines.append("")

    overall = evaluation.get("overall", {})
    lines.append(f"**总覆盖率**: {overall.get('covered_topics', 0)}/{overall.get('total_topics', 0)} = {overall.get('coverage_rate', 0)}% 有接触")
    lines.append("")

    def _format_group(name: str, data: dict):
        stats = data.get("stats", {})
        lines.append(f"### {name}")
        lines.append(f"共 {stats.get('total', 0)} 项知识点 | 🟢{stats.get('精通',0)} 🟡{stats.get('熟练',0)} 🟠{stats.get('入门',0)} 🔵{stats.get('初窥',0)} 🔴{stats.get('空白',0)} | 覆盖率: {data.get('coverage', 0)}%")
        lines.append("")
        lines.append("| 知识点 | AC题数 | 掌握等级 |")
        lines.append("|--------|--------|----------|")
        for item in data.get("details", []):
            lines.append(f"| {item['topic']} | {item['ac_count']} | {item['level']} |")
        lines.append("")

    _format_group("入门级 CSP-J", evaluation.get("csp_j", {}))
    _format_group("提高级 CSP-S", evaluation.get("csp_s", {}))
    _format_group("省选级", evaluation.get("provincial", {}))
    _format_group("NOI级", evaluation.get("noi", {}))

    return "\n".join(lines)


def get_weak_topics(evaluation: dict[str, Any], top_n: int = 5) -> list[str]:
    """获取最薄弱的知识点列表（空白+初窥）"""
    weak = []
    for group_name, group_data in [
        ("CSP-S", evaluation.get("csp_s", {}).get("details", [])),
        ("省选", evaluation.get("provincial", {}).get("details", [])),
        ("NOI", evaluation.get("noi", {}).get("details", [])),
    ]:
        for item in group_data:
            if "空白" in item["level"] or "初窥" in item["level"]:
                weak.append(f"[{group_name}] {item['topic']}")
    return weak[:top_n]


def get_strong_topics(evaluation: dict[str, Any], top_n: int = 3) -> list[str]:
    """获取最强的知识点列表（精通）"""
    strong = []
    for group_name, group_data in [
        ("CSP-J", evaluation.get("csp_j", {}).get("details", [])),
        ("CSP-S", evaluation.get("csp_s", {}).get("details", [])),
    ]:
        for item in group_data:
            if "精通" in item["level"]:
                strong.append(f"[{group_name}] {item['topic']} ({item['ac_count']}题)")
    return strong[:top_n]
