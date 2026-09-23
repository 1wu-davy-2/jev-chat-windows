# 项目级 AI 协作规范

> 项目：jev-chat-windows（微信 Windows 4.x 旁挂回复辅助，Python + PySide6，两个进程）。
> 本文件只管「怎么干活」的规矩；代码地图、模块细节、易踩的坑见仓库根 `CLAUDE.md`，两边不重复写。

## 1. 最高优先级硬规则（必须置顶）

- **严禁 AI 署名**：commit / 注释 / PR / 文档里绝不出现 `Co-Authored-By: Claude`、`Generated with Claude Code`
  或任何形式的 AI 署名。GitHub 据此把 AI 算成仓库贡献者，用户不接受。
- **隐私红线**：代码、日志、报错、提交里不出现真实 API Key、内网 IP、带凭据的完整 URL。
  密钥只从环境变量 / 注册表读（`JEV_API_KEY` 判断、`LLM_API_KEY` 起草，历史名见 `core/providers.LEGACY`），
  文档里一律写 `<YOUR_API_KEY>`；报错文本必须过 `core/jev_client.redact_secrets()`；对外只留 `scheme://host`。
- **KICKOFF 硬约束**（`docs/KICKOFF.md`，改代码前先看）：只读自己有权看的对话；采集只用窗口截图 +
  本地离线 OCR，不 hook / 不注入 / 不碰微信进程与数据库；截图不落盘（代码里不该出现 `.save()`）；
  绝不自动发送（只粘文字，不发回车）；不碰转账 / 红包 / 收款元素。

## 2. 测试与资源限制

- **本项目没有 pytest、没有测试文件**。自测 = 各模块 `if __name__ == "__main__":` 里的断言块，
  一律不联网、不碰微信（`app/update.py` 靠 monkeypatch 掉 `urlopen`）。
- 固化入口，直接跑这些，别自己现编：
  `python -m core.providers` / `core.llm` / `core.draft` / `core.relay` / `core.jev_client` / `app.update`
- **CPU 杀手是 OCR**（onnxruntime 默认吃满所有核）。任何跑 `app/ocr.py`、`probe/probe_ocr*.py` 的命令
  必须先限核：`$env:OMP_NUM_THREADS=2; $env:ORT_NUM_THREADS=2`；**不得**裸跑 `probe_ocr_speed.py`。
- 看界面用 `tools/preview_ui.py`（合成数据，不采集不联网），加 `--screenshot` 出图即退；
  `tools/demo.py` 是真实联网调用，只在明确要端到端冒烟时跑。
- **强制子代理**：所有测试 / 冒烟交给独立 sub-agent 执行，主对话只收「失败文件 + 行号 + 错误类型」，
  绝不把完整日志、堆栈、OCR 输出拉回主对话。

## 3. 架构与语义约束

- `core/` 平台无关。出网只有三处：`core/draft.py`（起草）、`core/jev_client.py` + `core/llm.py`（判断）、
  `app/update.py`（查版本）。新增联网点 = 破坏架构，先问。
- `core/providers.py` 是来源表的**唯一真源**（协议 / 地址 / 默认模型 / 思考开关），且**永不出现 key**。
  协议层只认 `openai` / `anthropic` / `gemini`，一律走官方 SDK，不手写 HTTP；
  加来源只改这张表，别在别处硬编码地址或模型名。
- `core/engine.analyze()` 是整条链唯一入口（悬浮窗和 `tools/demo.py` 都只调它），返回字段固定
  `{candidates, best_index, best_reply, scores, answers, usage, reply_to}`，改结构要同步所有调用方。
- `settings.configured()` 与 `main.config_problem()` 必须**分头看起草和判断两步**；加 provider 时两处都要改，
  否则会放行「保存得下去、之后每条消息都失败」的配置。
- **两个进程**：父进程只管界面和网络，子进程 `app/worker.py` 跑截图 + OCR，只往队列丢纯 tuple。
  **Qt 不能跨线程碰**：网络调用在 `threading.Thread`，界面改动只发生在主线程 `tick()` 里。
- 会话状态在 `main.chats`，靠 `rev` 版本号作废过期的异步结果，别绕过它直接写 `result`。
- 设置分两处：key → 注册表 `HKCU\Environment`（不落盘）；其余 → `config.json`（`_read()` 每次重读，
  文件坏了退默认值——它在启动路径上，崩了界面都出不来）。
- `probe/` 是一次性探针（结论已写进 README），别改别删；`ponytail:` 注释是有意为之的简化，不是 TODO，别清。

## 4. Git 与构建交付

- 提交范围严格遵守当轮指令；**不提交测试文件**（`test_*.py` / `*_test.py` / `tests_*/` 留在工作区）。
- `requirements.txt` 和 `.bat` 必须纯 ASCII（中文 Windows 的 pip / cmd 按 GBK 读会炸）；`.py` 一律 UTF-8。
- 打包走 `build.bat`（或 `pyinstaller --noconfirm --clean jev.spec`），产物 `dist\jev-chat-windows\` 整个文件夹一起发。
- **别手改 `app/version.py`**：推 `v*` tag 时 CI 会覆盖它。加功能同步更新 README 的「设置说明」「项目结构」「更新记录」三节。

## 5. 执行流程约定

- 按里程碑推进，每完成一个阶段停下等确认。
- 修改代码前，先说明要改哪些文件、为什么，再动手。
- 上下文到 40% 主动 `/compact`，并丢弃无关日志与探针输出。
- 注释和 docstring 一律中文，讲「为什么」而不是「做了什么」。
