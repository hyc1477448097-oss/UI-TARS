# 测试 / 上线工作流桌面应用

本机单人工具：选择项目后点 **测试** 或 **上线**。Git 等确定性步骤由脚本执行；内部网页通过 Playwright 连上已登录的 Chrome，截图后交给豆包视觉模型，按 UI-TARS 动作空间点击填表。

第一期用浏览器打开 Vue 前端即可。用 pywebview 包成独立窗口放到第二期。

## 准备

### 1. 豆包 / 火山方舟

复制环境变量并填入密钥：

```powershell
cd d:\AIsome\UI-TARS\workflow-desktop
copy .env.example .env
```

`.env` 字段：

| 变量 | 含义 |
|------|------|
| `ARK_API_KEY` | 火山方舟 API Key |
| `ARK_BASE_URL` | 默认 `https://ark.cn-beijing.volces.com/api/v3` |
| `ARK_MODEL` | 带视觉的豆包模型名，例如 `doubao-1.5-thinking-vision-pro-250428` |
| `CHROME_CDP_URL` | 默认 `http://127.0.0.1:9222` |

模型必须按 UI-TARS 格式输出 `Thought` / `Action`。解析失败会停机，不会盲点。

### 2. 用调试端口启动已登录的 Chrome

不要和「没开调试口的日常 Chrome」抢同一份用户数据。建议单独快捷方式：

```text
chrome.exe --remote-debugging-port=9222 --user-data-dir="C:\ChromeWorkflow"
```

先在这个 Chrome 里完成内部站 SSO 登录，再点应用里的测试/上线。后端使用 `connect_over_cdp`，只新开 Tab，不会去关你的浏览器。

### 3. 知识库

编辑 [`knowledge/projects.yaml`](knowledge/projects.yaml)：

- `repo_path`、`test.branch`、`test.merge_from`（`current` 表示合并点击时的当前分支）
- `test.deploy_url` / `release.url`
- `test.uitars_goal` / `release.uitars_goal`
- `release.form` 里可用 `{{ticket}}`，对应界面上的工单号
- 本地没有仓库、只想试浏览器时，把 `test.skip_git` 设为 `true`

改 YAML 后可点界面「重新加载配置」，或重启后端。

## 启动（两个终端）

后端（在 `backend` 目录，以便找到 `app` 包）：

```powershell
cd d:\AIsome\UI-TARS\workflow-desktop\backend
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
pip install -e ../../codes
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

`requirements.txt` 是后端依赖；还需要把本仓库的 `codes/ui-tars` 以可编辑方式装上（解析 Action 用）。

前端：

```powershell
cd d:\AIsome\UI-TARS\workflow-desktop\frontend
npm install
npm run dev
```

浏览器打开 Vite 提示的地址（默认 http://127.0.0.1:5173）。

## 行为说明

- 同时只允许一条运行中的任务。
- **测试**：`git fetch` → 切测试分支 → merge → 打开部署页 → 视觉循环；识别到部署/启动类点击时会暂停，需点「确认执行」。
- **上线**：默认不做 Git；打开发布页并带上表单字段；识别到发布/提交类点击时同样要确认。失败不会自动重试。
- 最多 25 步（可用 `MAX_UITARS_STEPS` 调整）。可随时中止。
- 截图与 SQLite 库在 `workflow-desktop/data/`。

## 风险

- Chrome 必须带 `--remote-debugging-port` 启动，否则连不上登录态。
- 豆包若未按 Action 格式回答，流程失败并展示原文。
- 内部站 iframe 第一期按整页截图点击，必要时再补 frame。
