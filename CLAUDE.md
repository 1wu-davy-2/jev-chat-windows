# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 这是什么

Windows 上挂在微信（4.x，`Weixin.exe`）旁边的回复助手：截自己的微信窗口 → 本地离线 OCR 读对话 →
`core/engine.py` 起草 3 条候选并让 Jev 判断排序 → 悬浮窗 → **只填入输入框，发送永远由人按**。
判断内核来自安卓原版 `Finderchangchang/jev-chat-JARVIS`，本仓库只把采集换成了截图 + OCR。

注释、UI 文案、commit message、文档全部是中文，保持一致。代码里没有测试框架、没有 linter 配置。

## 硬约束（改代码时不能破）

来自 `docs/KICKOFF.md` 和 README「隐私与边界」，这几条是写进代码的：

1. 只读自己设备上、自己有权查看的对话。
2. 采集只用窗口级截图 + 本地离线 OCR。不 hook、不注入、不读微信数据库、不解密、不碰微信进程。
3. **截图不落盘**：帧始终是内存里的 numpy 数组，`main.py` / `app/` / `core/` 里没有一处写图。
   仅有的例外都在工具和探针里、且不碰运行时的帧：`tools/preview_ui.py --screenshot`（合成界面图）、
   `tools/make_icon.py`（图标）、`probe/probe_win.py`（调试用 `wechat_probe.png`，已在 .gitignore）。
4. **绝不自动发送**：`app/fill.py` 到 Ctrl+V 为止，不发回车、不点发送按钮。
5. 不碰钱：转账/红包/收款相关的界面元素一律不碰，起草的 system prompt 里也禁了这几个话题。
6. **API key 只进环境变量**（`OPENROUTER_API_KEY` / `DEEPSEEK_API_KEY` / `RELAY_API_KEY`，落
   `HKCU\Environment`），任何文件里不出现 key，也绝不进日志——报错文本一律过
   `core/jev_client.py:redact_secrets()`（加新 key 记得把它加进那个元组）。
7. 静默期零调用：只有对方来了新消息（或群里换了回复对象）才调模型。

## 常用命令

```bash
# 源码运行（首次会弹设置页填 key）
python main.py

# 内置自测（就这两处，跑完打印 ok）
python core/draft.py        # 候选解析器 + 防注入过滤的断言
python -m app.update        # 版本号比较，monkeypatch urlopen，不联网

# 端到端冒烟：写死的一段对话跑完整链，需 key + 联网
set PYTHONPATH=. && python tools/demo.py

# 界面预览：合成数据，不采集、不联网、不碰微信；可出 README 那几张截图
python tools/preview_ui.py --state ready
python tools/preview_ui.py --state ready --screenshot docs/ui_home.png

# 打包（onedir，产物 dist\jev-chat\ 整个文件夹才是成品）
build.bat
pyinstaller --noconfirm --clean jev.spec
```

`tools/` 和 `probe/` 下的脚本按「项目根在 `PYTHONPATH` 里」写（PyCharm 默认会加），代码里没有
`sys.path` 补丁。`probe/` 是一次性探针，结论已写进 README，留着是为了可复现——别当生产代码改。

CI：推 `v*` tag → `.github/workflows/release.yml` 在 windows-latest 上打包、用 tag 覆盖
`app/version.py` 的 `VERSION`、压 zip 挂 Release；手动触发只出 artifact。

## 架构

**两个进程 + 两条队列**，这是理解全局的关键：

```
main.py（父进程，只管界面和网络）
  ├─ multiprocessing.Process → app/worker.py（子进程：截图 → 定位 → OCR → 去重）
  │     一帧 OCR 250~800ms，放 Qt 主线程界面会僵；父子靠 multiprocessing.Queue 传消息
  │     子进程只丢纯 tuple/str 过队列，底色 bg（numpy）留在子进程
  └─ 队列里冒出新的 her 消息 → threading.Thread 跑 core.engine.analyze()（网络）→ 结果丢 queue
        UI 只在主线程的 tick()（每 50ms）里动——Qt 不能跨线程碰
```

采集链（都在 `app/`）：`capture.find_wechat_hwnd()` 找窗口 → `Capture`（WGC）盯帧，`settled()`
在画面停稳后才交整帧（滚动动画/半截气泡全跳过）→ `capture.chat_area()` 用像素锚点定位消息区
（认底色和分隔线，不写死坐标）→ `ocr.read_title()` 读头部会话名当 key → `ocr.Reader.read()`
按框内底色分类 me/her/gray 并合并多行 → `Reader.new_lines()` 跟已见集合比，只报新出现的。

判断链（都在 `core/`，平台无关，不依赖 Windows）：`engine.analyze()` 是**唯一入口**——
先 `draft.draft_candidates()` 盲起草 3 条（不喂 Jev 判断），再 `jev_client.ask()` 一次问完
7 道判断题 + 一道「哪条候选最合适」，概率就是卡片上的百分比。`questions.py` 里是固定题目口径，
跟安卓原版一致。`core/` 里两个模块有 `try: from .x import / except ImportError: from x import`
的双导入写法，为了既能当包也能当脚本直接跑。

**两个出网口子都可以改走第三方中转**（`provider="custom"` + `base_url`，地址归一在 `core/relay.py`）：
起草走 `{root}/v1/chat/completions`（标准口，中转一般都转）；判断走的不是标准口——OpenRouter 官方是
`{root}/api/alpha/decisions`，各家中转叫法不同（PackyCode 的 typesafe 通道是 `/v1/systemone`），
所以路径可配（`relay_judge_path`）。`judge_relay` 默认关、仍走 OpenRouter，没验过别默认它一定通。
`probe/probe_relay.py` 是验这件事的探针。

**宠物优先形态**（`pet_enabled`，默认开）：平时桌面上只有宠物，有消息才弹候选条。这是**三个独立
顶层窗**，不是「一个窗变形」——改 `setWindowFlags` 会重建 HWND，位置会丢还会闪：

| 窗口 | 类 | 说明 |
| --- | --- | --- |
| 宠物 | `app/pet.py:PetWindow` | 透明置顶，`Qt.Tool` 不进任务栏；拖拽/点击靠 4px 阈值区分 |
| 候选条 | `app/overlay.py:_CandidateBar` | 280px，贴宠物上方（放不下翻下方） |
| 面板 | `app/overlay.py:_MainWindow` | 就是原来那个悬浮窗，默认收起 |

宠物右键菜单（`Overlay._build_pet_menu()`，三项：暂停采集 / 全屏（主页）/ 设置）里，「暂停采集」
**不是**直接调 `on_toggle_capture`，而是 `captureSwitch.setChecked(...)` 让信号走一遍
`checkedChanged → _capture_toggled`——直接调回调的话开关自己还停在旧状态，两边就各说各话了。
菜单内容拆在 `_build_pet_menu()` 里而不是塞进 `contextMenuEvent`，是为了 `tools/preview_ui.py
--state pet-menu` 能摆出来截图（`exec()` 是嵌套事件循环，截图回调进不去）。

`phase`（idle/scanning/notify/thinking/ready）是**派生**出来的，不是事件流水账：
`main.py:phase_now()` 是纯函数，`refresh_phase()` 是唯一写者、唯一调用点是 `tick()` 的出口。
**别在 `drain()` 中途写 phase**，否则 `tick` 里「busy=False 紧跟 start_analyze 又置 True」那一瞬间
的中间态会闪出来。`Overlay.set_phase()` 首行幂等，少了它每 50ms 重放一次、候选条会不停重建。

**设计 token 在 `app/theme.py`**：界面代码一律从那儿取色取字号，别再写死十六进制值。Qt 没有
box-shadow，卡片阴影只能靠 `theme.apply_shadow()`（一层 `QGraphicsDropShadowEffect`），
**用了它的卡片，父容器边距和卡片间距都不能小于 `SHADOW_PAD`**，否则阴影被裁或被下一张卡盖住。

**按会话隔离**：`main.py` 的 `chats[会话名]` 各存 history（deque maxlen=60）、上次结果、
`senders`（群里发过言的人）、`target`（用户挑的回复对象）。每个会话一个 `ocr.Reader`，
去重状态互不干扰。`rev` 是版本号：会话来了新消息就 +1，回来的结果 `revision` 对不上就丢掉
（在跑的分析作废）。`state["rerun"]` 让分析期间又来的新消息接着跑最新的，不并发堆积。

**队列消息形状**（`main.py:drain()` 是唯一的消费点，加新的 kind 要两边一起改）：

| kind | 载荷 | 含义 |
| --- | --- | --- |
| `area` | `(x0, y0, x1, y1)` | 窗口挪了，更新填入坐标 |
| `chat` | `title` | 微信切了会话 |
| `lines` | `title, [(who, name, text)], rect` | 这一帧新出现的消息 |
| `status` / `dead` | `文本` | 单帧失败提示 / 采集彻底停了 |
| `paused` / `resumed` | — | 子进程确认采集开关状态 |

## 跨文件约定（改一处得跟着改的地方）

- **加/改一个设置项**要同时动四处：`app/settings.py`（读函数 + `save()` 签名 + `config.json` 的键）、
  `app/overlay.py`（`_build_settings()` 建控件、`_load_settings()` 回填、`_save()` 提交）、
  `tools/preview_ui.py` 的 `patch.multiple(...)` 列表（漏了预览就炸）、README 的设置说明表。
- **加一个新模块**要进 `jev.spec` 的 `hiddenimports`——spawn 出来的子进程和运行时才 import 的
  `core/` 静态分析扫不到，漏了就是打包后启动即炸。**加一个资源文件**要进同文件的 `datas`
  （吉祥物那三张 PNG 就是），而且加载路径得认 `sys._MEIPASS`，别照抄 `settings._ROOT` 那种
  「exe 旁边」的写法——素材在 onedir 下位于 `_internal/`。
- **消息格式**统一是 `[(who, text)]` 或 `[(who, text, name)]`（也可 dict），`who ∈ {her, me}`，
  name 是群里的发言人，最新一条在最后。`engine.analyze()`、`questions.build_state()`、
  `draft.draft_candidates()` 都吃这一套，别在中间层换形状。
- **消息区元组**：`capture.chat_area()` 返回 6 元 `(x0, y0, x1, y1, bg, y_pane)`（子进程内部用，
  `y_pane` 是面板第一行、头部从这里起算），过队列给父进程的 `rect` 只有前 4 个——`fill.fill()`
  按 `(x0, _, _, y1)` 算输入框位置（底线下方 40px、左边界右侧 60px，微信改布局就得跟着调）。
- **文案与编码**：Python 读写文件一律 `encoding='utf-8'`；`requirements.txt` 必须纯 ASCII
  （中文 Windows 上 pip 按 GBK 读会炸）；`build.bat` 同理。
- UI 里显示的判断题选项中文在 `app/overlay.py` 的 `_CHOICES`，题目本身在 `core/questions.py`——
  加一个 choice 要两处都加，不然界面显示「暂未判断」。

## 技术坑（都踩过，别重踩）

- **RapidOCR 必须 1.4.x 且 `det_limit_type="max"`**：默认 `'min'` 会把小图放大到短边 736，裁小反而更慢；
  1.2.x 构造参数会 KeyError。引擎全进程共用一个实例（~40MB），每个会话一个 `Reader` 但共用引擎。
- **中转的思考开关选错派系不报错、只被无视**（`core/relay.py` 的 `THINKING_STYLES`）：DeepSeek 式
  `thinking.type=disabled` 和 OpenRouter 式 `reasoning.enabled=false` 互相不认，传错了思考照开，
  `max_tokens` 全被推理吃掉 → 起草回来是空 content → 报「起草结果解析不出候选」。看到这个报错先查这里。
- **`jev_client.ask()` 的 url/model 是参数，别在函数体里退回模块常量**：`API_URL`/`MODEL` 只是默认值，
  改签名时漏改 `urllib.request.Request(...)` 里那一行，就会拿着中转的 key 去打 OpenRouter 官方口，
  症状是莫名其妙的 401。
- **64 位下 ctypes 句柄必须显式声明 `restype`/`argtypes`**（`app/fill.py` 开头那一串）：
  `ctypes.windll` 默认返回 32 位 `c_int`，`GlobalAlloc` 的 64 位句柄会被截断 → `memmove(NULL)` 崩。
- **微信窗口不能最小化**：Windows 不渲染最小化窗口，什么截图法都拿不到画面。`capture.unminimize()`
  会无激活还原再压到最底层（不抢焦点）；被别的窗口盖住不影响 WGC。
- **Win10 上 WGC 会在微信窗口外画一圈黄框**，系统不给关（Win11 才行）。暂停采集会停掉 WGC 会话，
  黄框跟着消失——所以 `worker.run()` 里暂停是真的 `cap.stop()`，不是跳过帧。
- **OCR 的「文字必须落在平底色上」规则只对精确像素的帧成立**（众数颜色占比 <45% 判为图片里的字）：
  缩放/压缩过的图（拿预览窗再截一次）底色会糊成几百种颜色，整屏都会被当图片丢掉。
- **`chat_area()` 靠「面板 45% 高度以下第一根分隔线」找输入框顶**：输入框拉高超过面板一半会认错。
- **PyInstaller 用 onedir**（`jev.spec`）：onefile 有 ~150MB 每次启动都要解压。
  `console=False`，所以 exe 里的 `print` 是看不到的，状态都走界面。
- `app/settings.py` 读 key 时先看进程环境、没有再读注册表：IDE 启动时把环境快照拿走了，
  只靠 `os.environ` 会「保存了下次打开还是没有」。
