# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

本文件是**代码地图**：模块职责、数据怎么流、哪里容易踩。干活时的规矩（测试怎么跑、隐私红线、
提交约定）在 `.claude/CLAUDE.md`，两边不重复写。

## 这是什么

微信 Windows 4.x 旁挂的回复辅助：窗口截图 → 本地离线 OCR → Jev 判断意图/情绪 → 3 条候选 → 一键填入微信输入框。
判断内核（`core/`）沿用安卓原版 [jev-chat-JARVIS](https://github.com/Finderchangchang/jev-chat-JARVIS)，采集换成
Windows Graphics Capture + RapidOCR。README 是用户手册，细节（设置项、模型表、已知限制、项目结构）都在那里。

## 命令

```powershell
# 跑起来（开发）
python -m venv .venv; .venv\Scripts\activate
pip install -r requirements.txt
python main.py

# 界面预览：合成数据，不采集、不联网、不碰真实微信；--screenshot 出图后退出
# 状态：ready / waiting / loading / error / setup / settings / settings-custom / paused / debug
$env:PYTHONPATH="."; python tools/preview_ui.py --state settings-custom --screenshot docs/ui_settings.png

# 打包（onedir，产物在 dist\jev-chat-windows\）
build.bat                 # 或 pyinstaller --noconfirm --clean jev.spec
```

`tools/` 和 `probe/` 里的脚本假设项目根在 `PYTHONPATH` 里（代码里没有 `sys.path` 补丁），命令行跑必须自己带上；
`core/`、`app/` 用 `python -m` 跑。推 `v*` tag 时 `.github/workflows/release.yml` 会用 tag 名覆盖 `app/version.py`
并打出 zip 挂 Release，别手改那个文件。

## 架构

**两个进程。** 父进程 `main.py` 只管界面和网络；子进程 `app/worker.py` 跑截图 + OCR（一帧 250~800ms，放 Qt 主线程界面会僵）。

- 子进程往 `multiprocessing.Queue` 丢纯 tuple，父进程的 `drain()` 是唯一消费者。消息类型：
  `("area", rect)` 窗口挪了、`("chat", 会话名)` 微信切了会话、`("status", 文本)` 单帧失败、
  `("paused",)` / `("resumed",)` 采集开关确认、`("dead", 原因)` 采集彻底停了、
  `("debug", 一帧)` 调试视图（窗口不在就直接丢）、`("lines", 会话名, [(who,name,text)], rect)` 新消息。
- 两个父子共用的 `multiprocessing.Event`：`capture_on` 置位=采集（清掉=暂停，WGC 会话一起停，Win10 那圈黄框
  也跟着消失）；`debug_on` 置位=子进程往队列里送整帧给调试窗（一帧 2~3MB，默认清着）。
- **Qt 不能跨线程碰。** `analyze_bg()` / `check_update_bg()` 在 `threading.Thread` 里只做网络调用，结果丢 `results` /
  `update_result` 队列；所有界面改动都发生在主线程的 `tick()` 里（`ov.after(50, tick)` 每 50ms 一次）。

**采集链**（`app/capture.py` → `app/ocr.py`）：

```
chat_area()   像素锚点定位消息区：面板底色取右半边众数色，左右边界找底色占比>30% 的列，
              分隔线找整行单色且非底色的行；不写死坐标，深浅主题通用
read_title()  头部 OCR 出会话名，当所有状态的 key（头部像素没变就不重跑，一次 ~60ms）
Reader.read() who_said() 按气泡底色和文字对比度分 me / her / gray（gray = 引用块/时间戳/发言人名/链接卡片）；
              群聊里灰字发言人名摘出来挂到它下面那条气泡上
new_lines()   跟上一帧比去重，只报新出现的行（往上滚翻出来的旧消息不算）
```

`app/debugwin.py` 是设置里那个「调试视图」：把子进程送来的帧和识别框画出来（绿=我 / 蓝=对方 / 灰=过滤 /
橙=发言人名 / 红=图片丢弃 / 黄=小字丢弃），只在内存里画，不存图。

**会话状态**全在 `main.py` 的 `chats` = `{会话名: {history, result, rev, target, senders}}`。`rev` 是版本号，每来新消息 +1，
异步分析回来时 `revision != chat["rev"]` 就丢弃——过期结果的保护就靠它（`("dead",)` 时把每个会话 rev 都 +1 让在跑的作废）。

**`core/` 平台无关**，只用 stdlib + 各家官方 SDK：

- `engine.analyze()` 是唯一入口，悬浮窗和 `tools/demo.py` 都只调它，返回
  `{candidates, best_index, best_reply, scores, answers, usage, reply_to}`。
- **三段式**：Jev 先答 7 道判断题 → 判断折成中文小抄（`questions.guidance_text()`）喂进起草提示词 →
  Jev 只排序。一次分析两次 Jev 调用，usage 相加。判断那次挂了退回老路（盲起草 + 判断排序一次合问）；
  排序挂了按第一条推荐。起草不再是盲起草——小抄在，它就顺着判断写。
- `providers.py` 是**来源表的唯一真源**（判断 `JEV_PROVIDERS` / 起草 `DRAFT_PROVIDERS`：协议、地址、
  默认模型、思考开关），纯数据、永不出现 key。加来源只改这张表。
- `llm.py` 是三种协议的薄适配层（openai / anthropic / gemini），一律走官方 SDK，不手写 HTTP；
  `jev_client.py` 是判断客户端，TypeSafe 走 SDK，OpenRouter 和自定义判断口共用一条手写 urllib 的
  decisions 协议（SDK 把路径写死成 `/v1/systemone`，打不到别的口）。
- `relay.py` 只干两件事：拼自定义判断口的地址（各家路径叫法不同，`judge_url()`），和几种思考开关的传法
  （`THINKING_STYLES`）。起草的地址由 `providers.py` 的 Base URL 直接给 SDK，不经这里。
- `draft.py` 拼提示词、解析候选、防注入过滤、不足 3 条时追问补齐。`questions.py` 是 7 道固定判断题 +
  `build_rank_question()` + 判断小抄 + 中文标签 `CHOICE_LABELS`（界面和起草小抄共用一份）。

**设置分两处存**（`app/settings.py`）：

- key → 注册表 `HKCU\Environment`，任何文件都不落。**全程只有两把**：判断 `JEV_API_KEY`、起草 `LLM_API_KEY`，
  跟选哪家来源无关，换来源就是换同一个槽里的值。读时进程环境优先，没有再读注册表（IDE 启动后环境快照是旧的，
  只靠 `os.environ` 会「保存了下次打开还是没有」）。老名字 `OPENROUTER_API_KEY` / `DEEPSEEK_API_KEY`
  经 `providers.LEGACY` 还能读到，保存一次自动抄进新名字。
- 其余 → 项目根（frozen 时是 exe 旁）的 `config.json`。`_read(name, default)` 每次重读，改设置不用重启；
  文件读不到/坏了/不是对象一律退默认值——`configured()` 那类判断在启动路径上，这里崩了整个界面都出不来。
- `save()` 的整个 dict 必须在 `open(..., "w")` **之前**拼好：`open` 一上来就把文件截断，之后再 `_read()`
  读到的是空文件，那些「传 None = 保留原值」的项就变成被清空了。

## 易踩的坑

- **`main.config_problem()` 必须分头看起草和判断两步**（地址、模型名也一起看）。「起草=OpenRouter +
  判断=自定义口」这种组合只看判断会放行一个「保存得下去、之后每条消息都失败」的配置。加 provider 时这里要改。
- **自定义来源的思考开关选错派系不报错、只被无视**：思考照开，`max_tokens` 全被推理吃掉，起草回来是空 content，
  表现为状态栏「起草结果解析不出候选」。`relay.THINKING_STYLES` = `thinking`（DeepSeek 式）/ `reasoning`
  （OpenRouter 式）/ `none`。表里那 11 家预设各自认什么是写死的（`providers.DRAFT_PROVIDERS[..].extra`），
  不受这个设置影响。
- **中转地址写法不统一**，一律过 `relay.py` 归一。默认判断路径自带 `/api` 所以按站点根拼（不然叠成 `/api/api`），
  用户自填的路径按 API 根拼（他是照报错原文抄的）。地址少了 scheme 要明确报错，不然 urllib 抛的是看不懂
  也脱不了敏的 `unknown url type`。
- **自定义判断口列不出模型**（decisions 协议只有 POST，没有列模型的口），`jev_client.list_models("custom", …)`
  会抛一句人话，设置页的「获取模型」按钮对它不适用——模型名手打。
- **`ocr.who_said()` 的「文字必须落在平底色上」只对精确像素的帧成立**（框里众数颜色占比 <45% 就当图片里的字丢掉）。
  缩放或压缩过的图底色会糊成几百种颜色，整屏都会被当成图片——probe 里拿预览窗再截一次的图就是这样。
- **`fill.py` 里带句柄/指针的 Win32 函数必须显式声明 `restype`/`argtypes`**：64 位下默认 32 位 `c_int` 会把
  HGLOBAL 截断成垃圾值，`GlobalLock(垃圾)` 返回 NULL，`memmove(NULL,…)` 直接 `access violation writing 0x0`。
- **RapidOCR 的 `det_limit_type` 必须 `'max'`**：默认 `'min'` 会把小图放大到短边 736，裁小反而更慢。
- **`draft.py` 的 `_clean` / `_sanitize` / SYSTEM prompt 是冲着「去人机感」和防提示词注入去的**，别当冗余清理掉。

## 约定

- 注释和 docstring 一律中文，讲「为什么」而不是「做了什么」；`ponytail:` 前缀标的是有意为之的简化或已知限制，
  不是 TODO，别顺手清掉。
- README 是用户手册：加功能要同步更新它的「设置说明」表、「项目结构」和「更新记录」三节。
- `probe/` 是一次性探针，结论已经写进 README，留着是为了可复现，别顺手改或删。
