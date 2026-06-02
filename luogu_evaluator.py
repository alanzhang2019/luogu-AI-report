import os
import json
import argparse
import math
import re
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from rich.console import Console
from rich.prompt import Prompt
from rich.markdown import Markdown
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from openai import OpenAI
import pyLuogu
from examples.export_for_ai import _build_tag_maps, _summarize, _pick_record_for_problem

import markdown as md
from jinja2 import Environment, FileSystemLoader
from playwright.sync_api import sync_playwright

console = Console()
DEFAULT_REPORT_MD = "luogu_coach_report.md"
DEFAULT_REPORT_HTML = "luogu_coach_report.html"
DEFAULT_REPORT_PDF = "luogu_coach_report.pdf"
DEFAULT_ASSETS_DIR = "luogu_report_assets"

DIAGNOSTIC_FRAMEWORK = """
【能力评估参考框架】（请对照此框架对用户进行诊断和分级建议）：
1. S级 - 计数与组合推导：赛时容易先写DFS/枚举，缺乏“统计对象集合”思维。需强化：组合数/容斥/DP/生成函数。
2. S级 - 图论建模与最短路变形：模板能写但建图边含义不稳，差分约束/分层图易卡。需强化：图的语义定义、最短路树。
3. A级 - 数据结构维护不变量：基础线段树能做，多标记易WA。需强化：节点信息明确数学定义、merge/pushdown的代数正确性。
4. A级 - DP 状态设计与优化：常规DP能写，维度多易爆复杂度。需强化：树形/区间/状压DP，单调队列优化。
5. A级 - 部分分升级能力：赛时能拿部分分，但不会倒推。需强化：从小n、小值域、树退化等子任务倒推正解。
6. B级 - 高级字符串结构：KMP/Hash有基础，自动机/SAM不稳定。需强化：节点代表的集合、Fail树/link的含义。
7. B级 - 计算几何：缺模板，少边界意识。需强化：向量/叉积、凸包、扫描线基础与eps处理。
8. B级 - 网络流/匹配：缺乏模式识别。需强化：建图谱系、最小割模型、费用流。
9. S级 - 复盘与错因沉淀：盲目改代码AC后就过。需强化：四段式复盘（赛时模型、错因、正解性质、代码不变量）。
"""


def find_chinese_font_path() -> str | None:
    candidates = [
        r"C:\Windows\Fonts\simhei.ttf",
        r"C:\Windows\Fonts\msyh.ttf",
        r"C:\Windows\Fonts\msyhbd.ttf",
        r"C:\Windows\Fonts\simkai.ttf",
        r"C:\Windows\Fonts\simsun.ttc",
    ]
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    return None


def configure_matplotlib_font() -> str | None:
    font_path = find_chinese_font_path()
    if font_path:
        from matplotlib import font_manager

        font_name = font_manager.FontProperties(fname=font_path).get_name()
        plt.rcParams["font.sans-serif"] = [font_name]
    plt.rcParams["axes.unicode_minus"] = False
    return font_path


def register_pdf_font() -> str:
    font_path = find_chinese_font_path()
    if font_path:
        try:
            pdfmetrics.registerFont(TTFont("CoachChinese", font_path))
            return "CoachChinese"
        except Exception:
            pass
    return "Helvetica"


def ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def compute_ability_scores(export_data: dict) -> dict[str, int]:
    summary = export_data.get("summary", {}) or {}
    top_tags = summary.get("top_tags", []) or []
    difficulty_histogram = summary.get("difficulty_histogram", {}) or {}
    solved_count = int(export_data.get("solved_count", 0))
    failed_count = int(export_data.get("failed_count", 0))

    keyword_map = {
        "基础实现": [],
        "搜索 / DFS": ["dfs", "搜索", "回溯", "枚举", "树遍历"],
        "动态规划": ["dp", "背包", "区间", "树形", "状压"],
        "图论": ["图", "tarjan", "lca", "最短路", "并查集", "网络流", "匹配", "树"],
        "数据结构": ["线段树", "树状数组", "bit", "堆", "单调", "平衡树", "st表", "数据结构"],
        "字符串 / 数学": ["字符串", "kmp", "hash", "trie", "sam", "数论", "数学", "组合", "计数", "贪心", "构造", "证明"],
    }

    difficulty_total = 0
    weighted = 0
    for key, value in difficulty_histogram.items():
        if str(key).isdigit():
            difficulty_total += int(value)
            weighted += int(key) * int(value)
    avg_difficulty = weighted / difficulty_total if difficulty_total else 0

    scores: dict[str, int] = {}
    for ability, keywords in keyword_map.items():
        score = 35 + min(20, solved_count * 2) - min(12, failed_count * 2)
        if ability == "基础实现":
            score = 48 + min(28, solved_count * 2) + int(avg_difficulty * 4)
        for item in top_tags:
            tag_name = str(item.get("name") or "").lower()
            count = int(item.get("count", 0))
            if any(keyword in tag_name for keyword in keywords):
                score += min(18, count * 2)
        if ability in {"动态规划", "图论", "数据结构", "字符串 / 数学"}:
            score += int(avg_difficulty * 3)
        scores[ability] = max(20, min(95, int(score)))
    return scores


def generate_chart_images(export_data: dict, output_dir: str) -> dict[str, str]:
    ensure_dir(output_dir)
    configure_matplotlib_font()

    chart_paths: dict[str, str] = {}
    summary = export_data.get("summary", {}) or {}
    difficulty_histogram = summary.get("difficulty_histogram", {}) or {}
    top_tags = summary.get("top_tags", []) or []
    solved_count = int(export_data.get("solved_count", 0))
    failed_count = int(export_data.get("failed_count", 0))

    labels = sorted(difficulty_histogram.keys(), key=lambda x: int(x) if str(x).isdigit() else str(x))
    values = [int(difficulty_histogram[k]) for k in labels]
    if labels:
        fig, ax = plt.subplots(figsize=(7.2, 4.2))
        ax.bar(labels, values, color="#4C78A8")
        ax.set_title("题目难度分布")
        ax.set_xlabel("难度等级")
        ax.set_ylabel("题目数量")
        for idx, value in enumerate(values):
            ax.text(idx, value + 0.1, str(value), ha="center", va="bottom", fontsize=9)
        fig.tight_layout()
        difficulty_path = os.path.join(output_dir, "difficulty_histogram.png")
        fig.savefig(difficulty_path, dpi=180, bbox_inches="tight")
        plt.close(fig)
        chart_paths["difficulty"] = difficulty_path

    fig, ax = plt.subplots(figsize=(6.2, 4.2))
    counts = [solved_count, failed_count]
    labels = ["已通过", "未通过"]
    colors_list = ["#59A14F", "#E15759"]
    if sum(counts) == 0:
        counts = [1]
        labels = ["暂无数据"]
        colors_list = ["#BAB0AC"]
    ax.pie(counts, labels=labels, autopct="%1.0f%%", startangle=90, colors=colors_list)
    ax.set_title("通过 / 未通过占比")
    fig.tight_layout()
    status_path = os.path.join(output_dir, "status_ratio.png")
    fig.savefig(status_path, dpi=180, bbox_inches="tight")
    plt.close(fig)
    chart_paths["status"] = status_path

    selected_tags = top_tags[:8]
    if selected_tags:
        fig, ax = plt.subplots(figsize=(8, 4.8))
        tag_names = [str(item.get("name") or item.get("id")) for item in selected_tags][::-1]
        tag_counts = [int(item.get("count", 0)) for item in selected_tags][::-1]
        ax.barh(tag_names, tag_counts, color="#F28E2B")
        ax.set_title("高频标签 Top 8")
        ax.set_xlabel("出现次数")
        for idx, value in enumerate(tag_counts):
            ax.text(value + 0.1, idx, str(value), va="center", fontsize=9)
        fig.tight_layout()
        tags_path = os.path.join(output_dir, "top_tags.png")
        fig.savefig(tags_path, dpi=180, bbox_inches="tight")
        plt.close(fig)
        chart_paths["tags"] = tags_path

    ability_scores = compute_ability_scores(export_data)
    radar_labels = list(ability_scores.keys())
    radar_values = [ability_scores[key] for key in radar_labels]
    if radar_labels:
        angles = [n / float(len(radar_labels)) * 2 * math.pi for n in range(len(radar_labels))]
        angles += angles[:1]
        radar_plot_values = radar_values + radar_values[:1]
        fig = plt.figure(figsize=(6.6, 6.2))
        ax = plt.subplot(111, polar=True)
        ax.set_theta_offset(math.pi / 2)
        ax.set_theta_direction(-1)
        ax.set_thetagrids([angle * 180 / math.pi for angle in angles[:-1]], radar_labels, fontsize=9)
        ax.set_ylim(0, 100)
        zone_colors = [
            (0, 40, "#FDECEC"),
            (40, 65, "#FFF3E0"),
            (65, 85, "#E8F4FF"),
            (85, 100, "#E7F6EC"),
        ]
        zone_angles = [n / 180.0 * math.pi for n in range(361)]
        for start, end, zone_color in zone_colors:
            ax.fill_between(zone_angles, start, end, color=zone_color, alpha=0.35)
        ax.plot(angles, radar_plot_values, color="#4C78A8", linewidth=2)
        ax.fill(angles, radar_plot_values, color="#4C78A8", alpha=0.25)
        ax.set_rgrids([20, 40, 60, 80, 100], angle=90, fontsize=8, color="#8A96A3")
        ax.set_title("能力雷达图", pad=18)
        radar_path = os.path.join(output_dir, "ability_radar.png")
        fig.savefig(radar_path, dpi=180, bbox_inches="tight")
        plt.close(fig)
        chart_paths["radar"] = radar_path

    return chart_paths


def build_html_and_pdf(report_md: str, export_data: dict, html_path: str, pdf_path: str, chart_paths: dict[str, str]) -> None:
    # 扩展 markdown，支持表格
    report_html = md.markdown(report_md, extensions=['tables', 'fenced_code'])
    
    # 替换错题分页
    # 在 6. **【未通过题目专属题解（从暴力到正解）】** 后面的 h3 题目标题前插入分页符
    report_html = re.sub(r'(<h3>Problem)', r'<div class="page-break"></div>\1', report_html)

    # 动态为表格中的“当前等级”和“优先级”添加圆角徽章颜色样式
    # 使用正则匹配 td 标签里的特定文字，加上 span 标签
    level_color_map = {
        r'(稳|强项|覆盖充分|强项但需精炼)': 'bg-green-100 text-green-800 border-green-200',
        r'(中等偏稳|中上|有基础|基础稳)': 'bg-blue-100 text-blue-800 border-blue-200',
        r'(待强化|偏弱|会但赛时成本高|需要加强证明|基础稳，高级弱)': 'bg-yellow-100 text-yellow-800 border-yellow-200',
        r'(短板|明显短板|基础弱，高级弱)': 'bg-red-100 text-red-800 border-red-200'
    }
    
    for pattern, color_class in level_color_map.items():
        # 匹配 <td> 文字 </td>，并在文字外包一层 badge
        report_html = re.sub(
            f'<td>({pattern})</td>', 
            f'<td><span class="px-2 py-1 rounded-full border text-xs font-semibold {color_class}">\\1</span></td>', 
            report_html
        )
        
    priority_color_map = {
        r'<td>(S)</td>': r'<td><span class="px-2 py-1 rounded border text-xs font-bold bg-red-100 text-red-800 border-red-200">\1</span></td>',
        r'<td>(A)</td>': r'<td><span class="px-2 py-1 rounded border text-xs font-bold bg-orange-100 text-orange-800 border-orange-200">\1</span></td>',
        r'<td>(B)</td>': r'<td><span class="px-2 py-1 rounded border text-xs font-bold bg-blue-100 text-blue-800 border-blue-200">\1</span></td>'
    }
    for old, new in priority_color_map.items():
        report_html = re.sub(old, new, report_html)

    # 准备模板数据
    summary = export_data.get("summary", {}) or {}
    difficulty_histogram = summary.get("difficulty_histogram", {}) or {}
    total = 0
    weighted = 0
    for key, value in difficulty_histogram.items():
        if str(key).isdigit():
            total += int(value)
            weighted += int(key) * int(value)
    avg_difficulty = f"{(weighted / total):.1f}" if total else "0.0"
    
    top_tag = "暂无"
    top_tags = summary.get("top_tags", []) or []
    if top_tags:
        top_tag = str(top_tags[0].get("name") or top_tags[0].get("id"))

    # 渲染 HTML
    env = Environment(loader=FileSystemLoader('.'))
    template = env.get_template('report_template.html')
    
    # 将图表路径转换为绝对路径，并加上 file:/// 协议，确保 Playwright 能够读取本地图片
    abs_chart_paths = {k: f"file:///{os.path.abspath(v).replace(os.sep, '/')}" for k, v in chart_paths.items()}

    rendered_html = template.render(
        export_data=export_data,
        report_html=report_html,
        chart_paths=abs_chart_paths,
        avg_difficulty=avg_difficulty,
        top_tag=top_tag
    )

    with open(html_path, 'w', encoding='utf-8') as f:
        f.write(rendered_html)

    # 导出为 PDF
    console.print("[cyan]正在调用 Playwright 将 HTML 导出为高质量 PDF...[/cyan]")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            # 加上 file:// 协议访问本地 HTML
            file_url = f"file:///{os.path.abspath(html_path).replace(os.sep, '/')}"
            page.goto(file_url)
            page.wait_for_load_state("networkidle")
            page.pdf(
                path=pdf_path,
                format="A4",
                print_background=True,
                margin={"top": "0", "right": "0", "bottom": "0", "left": "0"}
            )
            browser.close()
    except Exception as e:
        console.print(f"[red]PDF 导出失败（Playwright 错误），请确保已运行 `playwright install chromium`。\n错误详情：{e}[/red]")

def load_or_prompt_openai_config():
    key = os.environ.get("OPENAI_API_KEY")
    base_url = os.environ.get("OPENAI_BASE_URL")
    
    if not key:
        console.print(Panel("[yellow]OpenAI API Key not found.[/yellow]\nThis tool requires an OpenAI-compatible API key to evaluate your code and generate suggestions.\nIt supports any third-party platform that provides OpenAI-compatible endpoints (e.g., DeepSeek, Moonshot, SiliconFlow, etc.).", title="Configuration"))
        key = Prompt.ask("Please enter your API Key")
        os.environ["OPENAI_API_KEY"] = key.strip()
        
    if not base_url:
        base_url_input = Prompt.ask("Please enter the API Base URL (leave blank for default OpenAI: https://api.openai.com/v1)")
        if base_url_input.strip():
            os.environ["OPENAI_BASE_URL"] = base_url_input.strip()
            base_url = base_url_input.strip()
            
    # Also ask for model if base URL is provided since different platforms have different model names
    model_name = os.environ.get("OPENAI_MODEL_NAME")
    if not model_name:
        default_model = "gpt-4o" if not base_url else ""
        model_input = Prompt.ask(f"Please enter the model name to use (leave blank for default: {default_model})")
        if model_input.strip():
            os.environ["OPENAI_MODEL_NAME"] = model_input.strip()
        else:
            os.environ["OPENAI_MODEL_NAME"] = default_model
            
    return key, base_url, os.environ.get("OPENAI_MODEL_NAME")

def load_or_prompt_cookies():
    cookie_file = Path("cookies.json")
    if cookie_file.exists():
        try:
            return pyLuogu.LuoguCookies.from_file(str(cookie_file))
        except Exception as e:
            console.print(f"[red]Failed to load cookies.json: {e}[/red]")
            
    console.print(Panel("[yellow]Luogu Cookies not found.[/yellow]\nTo fetch your submissions, we need your Luogu cookies.", title="Configuration"))
    client_id = Prompt.ask("Enter your __client_id cookie value")
    uid = Prompt.ask("Enter your _uid cookie value")
    
    cookies = pyLuogu.LuoguCookies({
        "__client_id": client_id.strip(),
        "_uid": uid.strip()
    })
    
    with open("cookies.json", "w", encoding="utf-8") as f:
        json.dump(cookies.to_json(), f, indent=2)
        
    return cookies

def generate_ai_report(export_data: dict, api_key: str, base_url: str | None, model_name: str) -> str:
    client_kwargs = {"api_key": api_key}
    if base_url:
        client_kwargs["base_url"] = base_url
        
    client = OpenAI(**client_kwargs)
    
    solved_count = export_data.get("solved_count", 0)
    failed_count = export_data.get("failed_count", 0)
    summary = export_data.get("summary", {})
    
    # 提取代码样本（通过的题）
    passed_samples = []
    for item in export_data.get("passed_items", []):
        record = item.get("record")
        if record and isinstance(record, dict) and record.get("sourceCode"):
            passed_samples.append(f"### Problem {item['problem']['pid']} - {item['problem']['title']} (Passed)\n```cpp\n{record['sourceCode'][:800]}\n```\n")
        if len(passed_samples) >= 3:
            break

    # 提取未通过/做错的题
    failed_samples = []
    for item in export_data.get("failed_items", []):
        record = item.get("record")
        pid = item['problem']['pid']
        title = item['problem']['title']
        code_str = ""
        if record and isinstance(record, dict) and record.get("sourceCode"):
            code_str = f"User's failed code snippet:\n```cpp\n{record['sourceCode'][:800]}\n```\n"
        failed_samples.append(f"### Problem {pid} - {title} (Attempted but NOT passed)\n{code_str}")
        if len(failed_samples) >= 5: # Limit failed examples
            break
            
    # Load syllabus contexts if available
    syllabus_context = ""
    for syllabus_file in ["GESP考纲.pdf.txt", "noi大纲.pdf.txt"]:
        syllabus_path = Path(syllabus_file)
        if syllabus_path.exists():
            content = syllabus_path.read_text(encoding="utf-8")
            # Truncate if too long, but they are ~12k chars each which is fine
            syllabus_context += f"【{syllabus_file} 内容摘要】\n{content[:15000]}\n\n"

    prompt = f"""
你是一位顶级的算法竞赛金牌教练。我导出了一位选手的近期洛谷做题记录（包括已通过和尝试但未通过的题目代码）。
请你根据我提供的【能力评估参考框架】以及【官方考纲】，对他进行深度的诊断，并针对他【未做完/做错的题目】给出极具启发性的题解。

{DIAGNOSTIC_FRAMEWORK}

{syllabus_context}

### 选手的全局数据统计
- 本次导出中已通过题数: {solved_count}
- 本次导出中未通过/卡住题数: {failed_count}
- 难度分布直方图: {json.dumps(summary.get('difficulty_histogram'))}
- 偏好的算法标签: {json.dumps(summary.get('top_tags'))}

### 选手最近通过的代码样本（用于评估代码习惯）
{''.join(passed_samples) if passed_samples else '暂无代码'}

### 选手未做完/尝试失败的题目（重点出题解部分）
{''.join(failed_samples) if failed_samples else '暂无未通过的题目'}

请你输出一份结构化的 Markdown 辅导报告，必须包含以下六个部分：

1. **【综合能力雷达表与诊断】**：
   请首先输出一个 Markdown 格式的表格，评估选手在各大能力块的状态。表格必须严格包含以下四列：`| 能力块 | 当前等级 | 数据证据 | 已经具备 |`
   - **能力块**（参考但不限于）：基础实现/代码落地、输入输出/数值意识、搜索/DFS、基础DP/背包、计数DP/组合推导、图论模板、最短路/差分约束、数据结构基础维护、高级数据结构/平衡树、字符串、贪心/构造/证明。
   - **当前等级**：用精炼的词语评级（如：稳、中等偏稳、强项但需精炼、覆盖充分、明显短板、有基础、偏弱、中上、会但赛时成本高、基础稳高级弱、需要加强证明等）。
   - **数据证据**：结合我提供的“全局数据统计”以及“代码样本”来提取证据。
   - **已经具备**：一句话总结该模块选手目前已经掌握的底线能力。

2. **【考纲精准定级与知识点盲区】**（根据提供的 GESP考纲 和 NOI大纲）：
   - **当前对应等级水平**：明确指出该选手目前处于 GESP 的几级水平，以及对应 NOI大纲 的哪个阶段（入门级/提高级/NOI级）。
   - **知识点强弱项**：严格对照考纲中的知识点名词，列出其掌握得最好的 3 个考点，以及最薄弱的 3 个考点。
   - **训练盲区**：指出他在当前等级中“完全没有涉及/刷题数据中缺失”的必考知识点。

3. **【风险诊断与训练闭环表】**：
   输出第二个 Markdown 表格，表头必须严格是：`| 优先级 | 风险项 | 触发场景 | 比赛症状 | 根因判断 | 训练专题 | 验收标准 |`
   - 行数至少 5 行，优先级使用 `S/A/B`。
   - 这个表必须是高度可执行的训练方案，结合他的大纲盲区与错题，指出风险和验收标准。

4. **【代码质量与工程习惯】**：基于他通过的代码样本，指出2个优点和3个必须改掉的坏习惯。

5. **【定制训练题单】**：
   根据上述大纲盲区和薄弱项，定制一份包含 5-8 道题型（可以带洛谷题号或题型描述）的训练题单，明确每一道的训练目标。

6. **【未通过题目专属题解（从暴力到正解）】**：针对上面列出的“未做完/尝试失败的题目”，逐一出题解。
    - 绝不能直接给出最优解！
    - 必须严格遵循**“从暴力到正解的思考过程”**：
      a) **AI 题解摘要**：一句话点出这道题的核心思路或坑点。
      b) 暴力思路怎么想？（复杂度是多少，能拿多少部分分？）
      c) 瓶颈在哪里？（时间卡在哪，空间卡在哪？）
      d) 关键性质/不变量观察（Key Observation）。
      e) 最终正解的推导与核心代码结构。
      f) **推荐同类题**：推荐 1-2 道涉及相同考点或技巧的洛谷题目（标明题号和简要推荐理由）。
 """
    
    response = client.chat.completions.create(
        model=model_name, # 使用用户指定的模型
        messages=[
            {"role": "system", "content": "你是顶级算法竞赛教练，极其擅长引导学生通过“暴力-观察-优化”的过程推导正解，且熟悉各种算法训练框架。"},
            {"role": "user", "content": prompt}
        ]
    )
    
    return response.choices[0].message.content

def extract_problems_from_practice(practice_data, key: str):
    problems = []
    if isinstance(practice_data, dict):
        items = practice_data.get(key)
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict): continue
                pid = item.get("pid")
                if pid:
                    problems.append(
                        pyLuogu.ProblemSummary({
                            "pid": str(pid),
                            "title": item.get("title") or item.get("name") or "",
                            "difficulty": item.get("difficulty"),
                            "type": item.get("type"),
                        })
                    )
    return problems

def main():
    parser = argparse.ArgumentParser(description="Luogu AI Evaluator - Coach Edition")
    parser.add_argument("--max-passed", type=int, default=10, help="Number of passed problems to fetch")
    parser.add_argument("--max-failed", type=int, default=5, help="Number of failed/unsolved problems to fetch")
    parser.add_argument("--report-md", default=DEFAULT_REPORT_MD, help="Markdown report output path")
    parser.add_argument("--report-pdf", default=DEFAULT_REPORT_PDF, help="PDF report output path")
    parser.add_argument("--assets-dir", default=DEFAULT_ASSETS_DIR, help="Directory for generated chart assets")
    args = parser.parse_args()
    
    console.print(Panel.fit("[bold cyan]Welcome to the Luogu AI Evaluator (Coach Edition)[/bold cyan]\n[dim]Incorporating Advanced Diagnostic Framework & Step-by-Step Editorials[/dim]"))
    
    # 收集学生信息
    console.print("\n[bold]为了生成更正式的报告，请填写测评基础信息（直接回车可跳过）：[/bold]")
    student_name = Prompt.ask("姓名", default="未知选手")
    school = Prompt.ask("学校", default="未知学校")
    grade = Prompt.ask("年级", default="未知年级")
    
    api_key, base_url, model_name = load_or_prompt_openai_config()
    cookies = load_or_prompt_cookies()
    
    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), transient=True) as progress:
        task = progress.add_task("[cyan]Connecting to Luogu API...", total=None)
        
        try:
            luogu = pyLuogu.luoguAPI(cookies=cookies)
            me = luogu.me()
            uid = int(me.uid)
            progress.update(task, description=f"[green]Connected as User ID: {uid}[/green]")
            
            tag_by_id, type_by_id = _build_tag_maps(luogu)
            practice = luogu.get_user_practice(uid)
            
            # Fetch Passed
            all_passed_problems = extract_problems_from_practice(practice.data, "passed")
            all_passed_problems.sort(key=lambda p: (p.difficulty if p.difficulty is not None else 10, p.pid), reverse=True)
            passed_problems = all_passed_problems[:args.max_passed]
            
            # Fetch Failed (Attempted but not passed)
            all_failed_problems = extract_problems_from_practice(practice.data, "failed")
            all_failed_problems.sort(key=lambda p: (p.difficulty if p.difficulty is not None else 10, p.pid), reverse=True)
            failed_problems = all_failed_problems[:args.max_failed]
            
            progress.update(task, description=f"[cyan]Fetching submissions for {len(passed_problems)} passed and {len(failed_problems)} failed problems...")
            
            passed_items = []
            for problem in passed_problems:
                try:
                    record = _pick_record_for_problem(luogu=luogu, uid=uid, pid=problem.pid, max_records_to_try=2)
                except Exception as e:
                    record = {"error": str(e)}
                passed_items.append({"problem": problem.to_json(), "record": record})
                
            failed_items = []
            for problem in failed_problems:
                try:
                    record = _pick_record_for_problem(luogu=luogu, uid=uid, pid=problem.pid, max_records_to_try=2)
                except Exception as e:
                    record = {"error": str(e)}
                failed_items.append({"problem": problem.to_json(), "record": record})
                
            summary = _summarize(all_passed_problems + all_failed_problems, tag_by_id=tag_by_id)
            
            import datetime
            export_data = {
                "student_info": {
                    "name": student_name,
                    "school": school,
                    "grade": grade,
                    "eval_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
                },
                "solved_count": len(all_passed_problems),
                "failed_count": len(all_failed_problems),
                "summary": summary,
                "passed_items": passed_items,
                "failed_items": failed_items
            }
            
            progress.update(task, description=f"[cyan]Analyzing with {model_name} (Applying diagnostic framework & generating editorials)...")
            report_md = generate_ai_report(export_data, api_key, base_url, model_name)
            progress.update(task, description="[green]Analysis complete!")
            
        except Exception as e:
            console.print(f"[red]Error during execution: {e}[/red]")
            return

    console.print("\n")
    console.print(Panel(Markdown(report_md), title="[bold magenta]AI Evaluation & Coaching Report[/bold magenta]"))

    with open(args.report_md, "w", encoding="utf-8") as f:
        f.write(report_md)

    chart_paths = generate_chart_images(export_data, args.assets_dir)
    build_html_and_pdf(report_md, export_data, DEFAULT_REPORT_HTML, args.report_pdf, chart_paths)

    console.print(f"\n[green]Markdown 报告已保存到 {os.path.abspath(args.report_md)}[/green]")
    console.print(f"[green]HTML 报告已保存到 {os.path.abspath(DEFAULT_REPORT_HTML)}[/green]")
    console.print(f"[green]PDF 报告已保存到 {os.path.abspath(args.report_pdf)}[/green]")
    if chart_paths:
        console.print(f"[green]图表资源已保存到 {os.path.abspath(args.assets_dir)}[/green]")

if __name__ == "__main__":
    main()
