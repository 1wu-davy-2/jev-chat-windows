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
   `app/voice.py` 是**唯一**一处会点微信界面的代码，边界写死在那儿：只在用户点了候选条上的
   「转文字」之后才动，只右键用户指定的那条语音、只点菜单**第一项**「语音转文字」，不碰任何
   别的项（那个菜单往下第 6 项是「删除」——只点第一项、且菜单是往右下展开的，点歪最坏是点个空，
   够不到它；理由和实测数据写在 `app/voice.py` 头上，**别改成往下点更多行**）。
   发送/删除/撤回/转账/红包一律不碰。
5. 不碰钱：转账/红包/收款相关的界面元素一律不碰，起草的 system prompt 里也禁了这几个话题。
6. **API key 只进环境变量**（`OPENROUTER_API_KEY` / `DEEPSEEK_API_KEY` / `RELAY_API_KEY`，落
   `HKCU\Environment`），任何文件里不出现 key，也绝不进日志——报错文本一律过
   `core/jev_client.py:redact_secrets()`（加新 key 记得把它加进那个元组）。
7. 静默期零调用：只有对方来了新消息（或群里换了回复对象）才调模型；而且消息来了还要先过
   静默窗口（见下），连着发的几条攒成一次问。

## 常用命令

```bash
# 源码运行（首次会弹设置页填 key）
python main.py

# 内置自测（就这五处，跑完打印 ok）
python core/draft.py        # 候选解析器 + 防注入过滤的断言
python core/styles.py       # 场景模板：每段 ≤200 字、键/名字不重、拼接顺序
python -m app.update        # 版本号比较，monkeypatch urlopen，不联网
python app/ocr.py           # 语音消息过滤正则（纯正则，不加载 OCR 引擎）
python app/capture.py       # 消息区定位：输入框顶那根细横线（合成帧，不截图）

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

**`analyze()` 不轻易抛**：判断那两次调用（七道题、排序）挂了都只记进返回值的 `trouble`，
不再往外抛——候选已经起草好了，没概率也照样能用，抛出去等于把结果和钱一起扔了，界面还只剩
一句「生成失败」。返回值里 `judged`（判断题答上了吗）/ `ranked`（排序做了吗）给界面判断要不要
标「推荐」，`ranked=False` 时 `best_index` 只是退回第一条、不是模型选的，**别标**。
只有起草失败或者候选被过滤光才抛 `JevError`，那条由 `main.analyze_bg` 收成 `("err", 原文)`，
界面走 `Overlay.set_error()`：状态栏一句短的 + 「查看详情」弹 `_ErrorBox` 看服务器返回的原文
（原文一个字都不改，脱敏在 `jev_client` 那层已经做过了）。

判断链（都在 `core/`，平台无关，不依赖 Windows）：`engine.analyze()` 是**唯一入口**——
先 `draft.draft_candidates()` 盲起草 3 条（不喂 Jev 判断），再 `jev_client.ask()` 一次问完
7 道判断题 + 一道「哪条候选最合适」，概率就是卡片上的百分比。`questions.py` 里是固定题目口径，
跟安卓原版一致。`core/` 里两个模块有 `try: from .x import / except ImportError: from x import`
的双导入写法，为了既能当包也能当脚本直接跑。

**攒一波再问**（`main.schedule_analyze()`）：对方来消息**不立刻**调模型，而是把静默窗口推到
`now + 5 + rand(1~5)` 秒之后；窗口到期由 `tick()` 点火——那是唯一动界面的地方。这期间来的消息
照常记进 `chats[title]["history"]`，到期时现取一次全喂进去，所以连着发的几条是**一次**调用、
上下文也完整（以前是每条都触发，或者边跑边 `rerun` 再补一次）。语音（`voice`）和表情包/图片
（`noise`）读不出正文、不进上下文，但同样算「还在说」，有等着的那次就把窗口往后推；没有待办时
它们不新开一次判断——光一张图没得判断。自己回了话（最新一条是 me）就把待办取消（`mark_replied()`）。

**「自己回了话」要把语音算上**：语音没有正文，以前判「最后说话的是不是我」只看文字行，于是
对方来一句、自己回两条语音，应用照样问一次模型（真机上报的）。谁说了最后一句由子进程算
（`ocr.bottom_speaker`：文字和语音一起比 y，微信新消息在最下面），父进程两个分支都看它——
`lines` 分支决定点不点火，`voice` 分支管「对方的语音 = 还在说 / 我的语音 = 已经回了」。
子进程**先发 `lines` 再发 `voice`**：自己发的语音得能把同一帧里 `lines` 刚点着的那次取消掉，顺序反了就不生效。

**转文字出来的字不是「新消息」**（`drain()` 里那个 `said`），它只是把屏幕上那条语音气泡的内容
补进记录（界面按时长并回「🔊 语音消息 N"」）。分两种：**对方那条**语音转出来的算他「说了句话」，
照常点火；**自己那条**转出来的不是——拿它点火会白问一次模型（真机上报过：自己语音转完，候选条
上写着「对方刚说 ……」），拿它走 `mark_replied()` 又会把对方刚来、正等着的那次判断取消掉。
所以这种一律不进 `said`：不点火、不取消、不动 `rev`、不 `invalidate_replies()`，只补 history 和记录。从第一条消息
起最多等 `_QUIET_MAX`（30 秒），免得对方一直发就一直不问。等待期间 `notify_until` 一起推到期
时刻，所以 phase 停在 notify：宠物角标留着、候选条上写着「读到新消息，等他发完再判断」，
面板空态走 `Overlay.set_waiting()` 那两句（别摆「等待对方的新消息」）。

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
同一件事在**子控件**上也成立：挂了这个效果的控件，会把自己的子控件渲进内部分画布，**超出它矩形
的部分直接裁掉**。宠物的红色角标就是这么被裁成半个的——它按窗口宽度摆，而两张贴图宽窄差一半
（`mascot-idle.png` 带两个甜甜圈，比 `mascot-alert.png` 宽不少），一换姿势角标就跑到 `frame`
外面去了。现在钉在**贴图**的右上角（`pet.py:_place_badge()`），另外补了 `resizeEvent`——布局是
延迟重排的，`adjustSize()` 之后立刻读宽度拿到的可能还是旧值。

**聊天记录是气泡不是文本流**（`app/overlay.py:_ChatLog` / `_Bubble` / `_Note`）：对方在左、我在右，
`_ChatLog.message()` 收 `(who, text, name, timestamp, peer, voice)`，`Overlay.feeds[会话名]` 里存的
也是这个形状，切会话时整体重建。`voice` 非空的是语音转出来的字、值是那条语音的时长
（`3"`），气泡里多一行「🔊 语音消息 3"」的小字——转写气泡跟普通文字消息长得一模一样，不标
一下分不出来（而且那条语音气泡我们本来就不显示）。

**语音和它的转写要并成一条**（`Overlay._merge_voice()`）：语音到了先记一条
「🔊 语音消息 N"」占位，转出来的字到达时按**时长**回头找那条占位、就地换成带时长的转写
（时间沿用语音那条的）。微信那边本来就是一条，分成两条看着像对方说了两句话。对不上
（应用启动前那条语音就在屏幕上、自己手动转的老语音）就不并，单独一条、标一行时长。
并的时候整屏重画（`_render_feed()`），所以它要先记住用户是不是贴着底看——正翻旧记录时
别把人拽回底部。占位那条的时长走 `main._voice_label()` 归一成 `N"`（OCR 会读成 `)3`
`3″` 之类），两边归一之后才对得上。三条 Qt 坑：① 开了 `wordWrap` 的 QLabel 会挑一个「看着合适」的**窄**宽度，
气泡被挤成细细一条——宽度得按 `QFontMetrics` 量出来自己定（`_fit_width()`），量之前先 `setFont`，
QSS 的 `font-size` 不保证那会儿已经生效；② 新加的行要等下一轮事件循环布局才重算，追加完立刻读
scrollbar 的 `maximum()` 还是旧值，跟底得 `QTimer.singleShot(0, ...)`；③ 页面竖向空间是靠**拉伸
因子**分的，`addStretch` 那根弹簧和记录区都是 1，展开记录时得把弹簧 `setStretch(…, 0)` 松掉，
否则余量被弹簧吸走、记录区永远长不大；但弹簧**不能直接删**——删了余量会落到页面里一堆
`Preferred` 的控件头上（`Preferred` 带 GrowFlag），空态排版会散开。

**按会话隔离**：`main.py` 的 `chats[会话名]` 各存 history（deque maxlen=60）、上次结果、
`senders`（群里发过言的人）、`target`（用户挑的回复对象）。每个会话一个 `ocr.Reader`，
去重状态互不干扰。`rev` 是版本号：会话来了新消息就 +1，回来的结果 `revision` 对不上就丢掉
（在跑的分析作废）。`state["rerun"]` 让分析期间又来的新消息接着跑最新的，不并发堆积。

**去重是两道**：子进程 `Reader.seen` 一道，父进程 `drain()` 里再拿 `chats[title]["history"]` 兜一道
（`_already_read()`，往回看 `_HISTORY_DUP` 条）。子进程那份状态是会丢的——进程重开（微信关了又开，
「dead」→ 下次开采集 `spawn_worker()` 新起一个）、标题 OCR 抖出一个没见过的名字（新 Reader）——
一丢就把整屏当新消息重报一遍。父进程这道拦下来：整批都是旧的就 `continue`，一条也不记、不触发。
判断「屏幕上最新那条要不要分析」看的是 `new[-1]` 本身新不新（`fresh[-1] is new[-1]`），不是过滤后
剩下的最后一条——不然往上翻一屏，最后剩个中间的旧消息也能把判断点着。

**队列消息形状**（`main.py:drain()` 是唯一的消费点，加新的 kind 要两边一起改）：

| kind | 载荷 | 含义 |
| --- | --- | --- |
| `area` | `(x0, y0, x1, y1)` | 窗口挪了，更新填入坐标 |
| `chat` | `title` | 微信切了会话 |
| `lines` | `title, [(who, name, text, 语音时长或 "")], rect, 最底下那条谁说的` | 这一帧新出现的消息；第 4 位只给界面用（并/标「🔊 语音消息 N"」），喂模型的 `history` 里不带它。末位见 `ocr.bottom_speaker` |
| `voice` | `title, [(x0,y0,x1,y1,时长,谁)], [(同上)], 最底下那条谁说的` | 第一份 = 这一帧所有**还没转过文字**的语音气泡，坐标给「转文字」用；第二份 = 其中**新出现**的（可能不止一条），父进程拿它往聊天记录里记「🔊 语音消息 N"」。谁 = 气泡底色分出来的 me/her。新的一来第一份必然跟着变，所以「变了才发」不会漏 |
| `noise` | `title` | 画面变了却没认出新文字（多半是表情包/图片）：只用来把静默窗口往后推，不进上下文 |
| `status` / `dead` | `文本` | 单帧失败提示 / 采集彻底停了 |
| `paused` / `resumed` | — | 子进程确认采集开关状态 |

## 跨文件约定（改一处得跟着改的地方）

- **加/改一个设置项**要同时动四处：`app/settings.py`（读函数 + `save()` 签名 + `config.json` 的键）、
  `app/overlay.py`（`_build_settings()` 建控件、`_load_settings()` 回填、`_save()` 提交）、
  `tools/preview_ui.py` 的 `patch.multiple(...)` 列表（漏了预览就炸）、README 的设置说明表。
  设置页是**三个页签**（回复偏好 / 模型设置 / 个人风格）：那一排按钮在 `_build_settings()` 的
  `tabButtons` 循环里、卡片显隐在 `_switch_tab()` 里，加一页要两处一起加。第三页「个人风格」
  是场景模板 + 自定义口吻，截图用 `tools/preview_ui.py --state settings --tab style`。
- **场景模板**：内置正文只有一份，在 `core/styles.py` 的 `PRESETS` 里；**加一型**只要往那儿加一条，
  设置页的下拉（`app/overlay.py` 的 `_PRESET_CHOICES` 按它拼）和 `resolve()` 就都认了。
  每段 **≤200 字**（`LIMIT`，自测卡着），超了会盖过对话本身、模型开始照着说明造句。
  存两个键：`style_preset`（选了哪一型，`""` = 不用）和 `style_texts`（**只存跟内置原文不一样的**
  那几型，按类型分开）——这样以后改内置文案，没动过手的用户能跟着更新，动过手的那型保留他改的。
  设置页那个大框就是**会拼进 prompt 的原文**，可以直接改；换类型时 `_preset_stash()` 先把框里的字
  收进内存再换，不然正在改的东西一切就没了。只喂起草（`draft_candidates(scene=...)` 收的是
  **归一好的正文**，不是键），不喂判断——Jev 判的是意图和紧张度，跟措辞无关。
  老配置里那句自由文本 `style` 会被当成「自定义」读出来，保存一次之后就没了。
- **候选的「显示位置」和「原始下标」是两回事**，别混。候选条和面板都按推荐顺序重排过
  （`_ordered` 里存的就是「位置 → (原始下标, 概率, 是否推荐)」），界面上看到的「1/2/3」是位置，
  只有推荐那条本来就排第一时才跟 `cands` 的下标重合。所以：`_fill()` / `_copy()` 收的、
  `_ReplyCard` 拿的、候选条每一行 `_BarRow.index` 存的，**全都是原始下标**；界面上的位置
  要用 `_cand_at(位置)` 折一下（Ctrl+1/2/3 就走它）。踩过：`_BarRow` 一开始存的是行号，
  点第 1 条填出来的是第 2 条的内容。
  **显示给用户的编号一律是「位置 + 1」**（面板卡片和候选条都是），跟 Ctrl+N 和候选条那个
  序号方块对齐——以前 `_BarRow` 的方块是 1 起、旁边那句「备选 N」是 0 起，同一个行里两个数对不上。
  排过序时看到的因此是「推荐回复 / 备选 2 / 备选 3」；没排序时是「备选 1/2/3」（`ranked=False`）。
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
- **语音消息气泡里没有正文，只有「时长 + 喇叭图标」**，OCR 出来是 `8"` 这种碎片。不拦的话它会被
  当成对方说的一句 `8"`，白白触发一整套 Jev 判断（还花钱）。两道关：
  1. `_VOICE` 正则，锚点是时长后面那个引号——13 次实测它每次都读得出来，只是会被读成 `(` `)` `?`；
     引号后再放最多两个字符，因为喇叭图标偶尔被读成字母（2.5 倍缩放下读成过 `G`）。**必须有引号**，
     这样 `6` `666` `5G` `8点见` `8-9` 这些真消息不会被误伤。
  2. `_has_icon`——引号被**整个读丢**时只剩个裸数字（真机上出现过「3"」→「3」，日志里就多一句
     「对方：3」，还拿它当最新消息去判断）。这时文字层面没法区分，只能看像素：语音气泡的数字旁边
     紧挨着喇叭图标，真发一个「3」旁边是空的。系数按真机截图量的（her：图标 11x16px、在数字左边
     9px；me 在右边），都乘字高 h，换 DPI 跟着缩放。
  3. `_VOICE_ICON`——**喇叭图标自己**被读成一个后括号、跑到时长前面：「3"」→「)3」（真机上出现过，
     日志里多一条「)3」的假消息，还可能拿它去触发判断）。这条**不走** `_has_icon`：图标已经被并进
     那个 OCR 框里了，框左边是空的，量不出来。所以只认「后括号 + 数字」这个形状，且要整条就是它——
     「)3」这种写法真消息里几乎没有，而「（3）」「(3」这类**前**括号开头的很常见，别误伤。
  改这三处先跑 `python app/ocr.py`（自测里有合成的正反例，不需要截图）。
- **微信的「语音转文字」是插在那条语音气泡正下方的，不是追加到聊天末尾。** 转一条老语音，结果落在
  「已知行」上面，而 `new_lines()` 的规则是「只认已知行下方的」（防滚动重报）——转完了应用根本看不见。
  `_under_voice()` 就是给这一类开的口子：紧跟在语音气泡下面、又没见过的文字行，无视 floor 直接放行。
  它同时返回**那条语音是谁发的**，转写那行的 who 以它为准——微信把转文字画在一个灰白气泡里，
  自己那条语音转出来也是这个颜色，按底色分会被认成对方说的（真机上报过）。
  同一条「紧贴」关系反着用一次：**下面贴着文字行的语音 = 已经转过了**，不再往候选条上报
  （`last_voice` 全量留着给 `_under_voice` 用，`last_voice_open` 才是发给父进程的那份）。
  少了这一步，用户转完文字、转写气泡一出现、布局一移，子进程重报一次位置，「转文字」就又冒回来了。
  实测间距：转写气泡贴 17px，下一条普通消息隔 69px（字高 lh=13），2.5×lh 这个阈值两边都分得开。
- **哪条语音算「新出现的」，由子进程按顺序判**（`Reader.new_voices()`），父进程只管往聊天记录里写。
  以前是父进程拿「这一帧比上一帧条数多」判、而且只记 `items[-1]`，两个毛病：一帧里冒出两条
  （刚启动那帧整屏都是新的、或者对方连发两条）只记最底下那条——真机上就是「我回了两条语音，
  记录里只有一条」；往上翻时条数也会变多，又把旧语音当新消息重报一遍。
  现在的判据是**顺序**：语音只会从底下加进来、从顶上滚出去，顺序永远不变，所以拿报过的那串
  （`seen_voice`）在眼前这串里找最长的一段，后面剩的才是新的。在 `last_voice`（全部）里找位置、
  只报 `last_voice_open` 里的——转过文字的那条会从 open 里消失，拿 open 找会中间缺一格对不齐。
  报过的一条都不在眼前时（翻远了）返回空：宁可漏记一行，也不能把旧语音当新消息记。（`python app/ocr.py` 自测里有一整串用例）
- **上面那个口子必须只在「刚点过转文字」时开**（`new_lines(lines, under_voice=...)`，别常开）。
  常开就是真机上报的那个 bug：往上翻记录、或者把微信窗口拉高，一条老语音连着它底下那条**早就转过**
  的转写一起露出来——几何关系跟「刚转完」一模一样（都是紧贴在气泡下面、都在已知行上方），
  于是被当成新消息报上去，白触发一次判断，候选条还张冠李戴地把那条老话当成「对方刚说」。
  唯一能区分两者的是**时间**：刚转的那条是用户几秒前点的按钮。所以父进程点成了
  `voice_until.value = time.monotonic() + _VOICE_WINDOW`（`multiprocessing.Value`，两个进程同一个
  monotonic 基准），子进程只在窗口内认这个口子。代价：你自己在微信里右键转老语音，转写接不住
  （最新那条不受影响，走正常的新消息规则）。
- **转文字走右键菜单，不走悬停药丸。**（这条推翻过一次，下面是量出来的最终结论。）
  微信在鼠标悬停语音气泡时会在气泡外侧冒一个内联的「转文字」药丸，看着比菜单安全，曾经改成走它。
  但真机上量下来：**合成鼠标叫不出那个药丸**——`SetCursorPos` 直跳 / 先停旁边再 `mouse_event`
  相对移入 / 分 30 步一点点挪过去 / 停在气泡正中 4 秒，四种全试了，屏幕上 OCR 不到「转文字」；
  同一时刻拿微信右上角那排窗口按钮做对照，合成悬停一划过去按钮就亮、移开就暗，所以不是
  「合成输入进不去微信」，就是那颗药丸不吃合成悬停。真人鼠标悬停时它会出现（截图见过），
  但程序复现不了，所以这条路作废。
  右键菜单则是合成右键一按就出来，第一项就是「语音转文字」（实测从上到下：语音转文字 / 收藏 /
  多选 / 提醒 / @引用 / …）。**盲点的风险靠方向兜住**：菜单往右下展开，只点第一项中心
  （`voice.FIRST_ITEM`，实测 +60,+21）；万一菜单翻到光标上方（屏幕底下没地方摆），那 +21 那一下
  落在**菜单外面**，点个空，够不到下面的「删除」。这条是选它、而不是「按固定偏移点第 6 项」的
  全部理由——**别改成往下点更多行**。
  菜单本身 WGC 看不见（按窗口抓图，抓不到微信另开的浮层），所以点完没法当场确认，界面上说的是
  「点过了」不是「转成了」。UIA 那条路始终堵死（微信界面自绘在 GPU 画布上，控件树是空的，
  见 README「为什么走 OCR」）。
- **量偏移量时记得先 `SetProcessDPIAware()`。** 探针不设的话，`ImageGrab` 抓的框和
  `DwmGetWindowAttribute` 给的坐标不在一个尺度上，量出来的位置全是错的（这个坑把上面那轮
  排查带偏过好几次：明明坐标是对的，截出来却是另一块地方）。app 和 worker 都设了，探针也得设。
- **「转文字」的气泡范围仍然按气泡边缘算，不能按 OCR 框算。** 右键要点在气泡正中，
  而 OCR 框只框住里面的字（图标+时长），离气泡边还差一大截，所以 `last_voice` 存的是
  `_bubble_extent()` 扫出来的**整个气泡**范围。`_bubble_extent` 横向必须扫**文字框上方**那一行
  ——扫文字框正中会被字本身打断，量出来只有 11px 宽（正好是数字那一小截），这个坑踩过。
- **`chat_area()` 找输入框顶靠的是「细横线」，不是「整行同色」**（`capture._thin_lines()`：
  整行非底色 >75% + 非底色像素基本同色 + 厚度 ≤4px）。微信 4.x 的输入框画的是**圆角框**，
  左右两头留着面板底色，整行是混色的 → 老判据（`std < 4`）漏判，`y_in` 一路退到面板底，
  裁剪把输入框连底下那条工具栏一起吃了进去。工具栏最左那个笑脸图标 OCR 出来正好是「?」
  （众数占比 0.41，离「当图片丢掉」的 0.45 就差一点），于是被当成对方发来的消息，
  白触发一次判断——真机上报上来的就是这条「对方刚说 ?」。输入框非空时还会多报一条绿色的
  「发送」（绿底 → 分类成 me，进 history）。改判据先跑 `python app/capture.py`（合成帧，不用截图）。
- **输入框拉高超过面板一半会认错**：上面那条 45% 的线。
- **PyInstaller 用 onedir**（`jev.spec`）：onefile 有 ~150MB 每次启动都要解压。
  `console=False`，所以 exe 里的 `print` 是看不到的，状态都走界面。
- `app/settings.py` 读 key 时先看进程环境、没有再读注册表：IDE 启动时把环境快照拿走了，
  只靠 `os.environ` 会「保存了下次打开还是没有」。
