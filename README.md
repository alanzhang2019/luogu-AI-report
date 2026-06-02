# luogu-AI-report

基于 `pyLuogu`（`luogu-api-python`）的洛谷数据导出 + AI 测评报告生成器：拉取做题/提交/代码样本，并生成更“正式测评系统风格”的 Markdown/HTML/PDF 报告（含图表、能力分层表、风险闭环表、错题“从暴力到正解”的题解）。

## 功能

- 从洛谷拉取个人练习数据（已通过/未通过题目列表），并抽样拉取提交记录与 `sourceCode`
- OpenAI-compatible 评估：支持 OpenAI 以及任意第三方 OpenAI 兼容接口（自定义 `base_url` + `model`）
- 报告输出：
  - Markdown：便于二次加工
  - HTML：模板化排版（封面信息、目录、图表页、正文）
  - PDF：通过 Playwright 导出高质量 PDF（可打印、可存档）
- 图表：难度分布、通过/未通过占比、高频标签 Top、能力雷达图
- 隐私保护：默认忽略 cookies、导出 JSON、报告与大纲文件（见 `.gitignore`）

## 项目结构

- `pyLuogu/`：Luogu API 封装（同步/异步），用于数据抓取与登录态访问
- `examples/export_for_ai.py`：导出与记录抽样的复用逻辑
- `luogu_evaluator.py`：一键测评入口（交互式配置 + 生成 Markdown/HTML/PDF）
- `report_template.html`：HTML 报告模板

## 安装

建议使用 Python 3.10+，并在虚拟环境中安装：

```bash
python -m venv .venv
.\.venv\Scripts\activate
python -m pip install -U pip
python -m pip install -e .
```

安装 Playwright 运行时（用于 HTML 转 PDF）：

```bash
python -m playwright install chromium
```

## 配置

### OpenAI-compatible（必需）

支持 OpenAI 及任何 OpenAI 兼容平台（例如 DeepSeek、Moonshot、SiliconFlow 等）。推荐用环境变量（也可以在运行时按提示交互输入）：

- `OPENAI_API_KEY`：API Key
- `OPENAI_BASE_URL`：可选，第三方平台的 Base URL（OpenAI 默认是 `https://api.openai.com/v1`）
- `OPENAI_MODEL_NAME`：模型名（不同平台模型名不同）

### Luogu Cookies（必需）

脚本优先读取当前目录下的 `cookies.json`；如果不存在，会提示你输入 `__client_id` 和 `_uid` 并生成 `cookies.json`。

注意：cookies 属于敏感信息，请勿提交到 Git 仓库或分享给他人。

## 使用

直接运行测评入口：

```bash
python .\luogu_evaluator.py
```

可选参数：

```bash
python .\luogu_evaluator.py ^
  --max-passed 30 ^
  --max-failed 10 ^
  --report-md luogu_coach_report.md ^
  --report-pdf luogu_coach_report.pdf ^
  --assets-dir luogu_report_assets
```

运行后会生成：

- `luogu_coach_report.md`：Markdown 报告
- `luogu_coach_report.html`：HTML 报告
- `luogu_coach_report.pdf`：PDF 报告
- `luogu_report_assets/`：图表 PNG 等资源

## 常见问题

- PDF 导出失败：先执行 `python -m playwright install chromium`
- 图表在 PDF 中不显示：确保 `luogu_report_assets/` 目录存在且生成成功；不要手动改动 HTML 里的图片路径

## Upstream

本项目基于 `luogu-api-python` 的 API 封装能力构建：

- Luogu API docs（上游文档）：https://github.com/sjx233/luogu-api-docs
- 原始库仓库（上游实现）：https://github.com/NekoOS-Group/luogu-api-python

## License

GPL-3.0，详见 [LICENSE](LICENSE)。
