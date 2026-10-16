# AI 视频格式转换器（Video Converter AI）

一个**原生桌面客户端**形态的 AI 视频格式转换工具：把视频转码这件「需要记一堆参数」的事，
做成有图形界面、普通用户能直接上手的产品，并用大模型把「一句话需求」翻译成 ffmpeg 参数。

> 本项目的视频处理能力**全部通过 `subprocess` 调用系统 `ffmpeg` / `ffprobe` 原生命令行**实现，
> 未使用 ffmpeg-python、PyAV 等任何封装库。

---

## 目录

- [功能特性](#功能特性)
- [界面预览](#界面预览)
- [技术栈](#技术栈)
- [项目结构](#项目结构)
- [快速开始](#快速开始)
- [AI 能力配置](#ai-能力配置)
- [使用指南](#使用指南)
- [内置预设](#内置预设)
- [常见问题](#常见问题)
- [开发与测试](#开发与测试)
- [已知限制](#已知限制)

---

## 功能特性

### 基础功能

| 功能 | 说明 |
| --- | --- |
| 视频导入 | 支持**文件选择对话框**与**拖拽导入**两种方式 |
| 元数据解析 | 调用 `ffprobe` 解析并展示：文件名、文件大小、时长、分辨率、视频编码、帧率、码率、音频编码、采样率、声道数、像素格式、封装格式 |
| 缩略图预览 | 自动提取视频**中间帧**生成缩略图；`-ss` 快速定位 + 缓存命中，刷新秒开 |
| 多格式输出 | MP4、MKV、AVI、MOV、WebM、MP3（纯音频提取）、GIF（动图） |
| 参数可调 | 视频编码、分辨率、帧率、视频码率、音频编码、音频码率、附加参数 |
| 内置预设 | **8 种**常用场景一键套用（见[内置预设](#内置预设)） |
| 实时进度 | 解析 `ffmpeg -progress` 协议，展示百分比、处理速度（x 倍速）、剩余时间 |
| 中途取消 | 随时取消任务，自动清理未写完整的输出文件 |
| 完成反馈 | 系统托盘通知 + 弹窗 + 一键打开输出目录 |
| 批量队列 | 支持批量添加任务、队列式执行，并发数默认 1（可调 1~3） |
| 环境自检 | 启动自动检测 ffmpeg/ffprobe，缺失时给出**分平台安装指引**，绝不崩溃 |

### AI 增强功能

| 功能 | 说明 |
| --- | --- |
| 自然语言转参数 | 输入「转成 1080P 的 MP4，文件控制在 500M 以内，适合发朋友圈」，自动生成并回填 ffmpeg 参数 |
| 严格 JSON 约束 | System Prompt 限定字段白名单 + 决策规则 + Few-shot 示例，大幅提升解析稳定性 |
| 三级容错降级 | **LLM 解析 → 本地正则规则 → 通用 1080P 默认**，AI 不可用时功能依然可用 |
| 字段级强校验 | 模型返回非法编码器/分辨率/帧率时自动回退默认值并提示用户 |
| 智能参数推荐 | 加载视频后按源视频分辨率、帧率、码率与目标格式推荐最优编码/码率组合 |
| 硬件加速 | 自动探测 NVIDIA NVENC / Intel QSV / AMD AMF，并对候选编码器做**试编码验证**，只认真正能跑通的方案 |
| 多厂商兼容 | 通过 OpenAI 兼容协议对接豆包（火山方舟）、DeepSeek、通义千问 |

### 体验优化

| 优化 | 说明 |
| --- | --- |
| 输出目录记忆 | 记住上次输出目录与输入目录，重启后免重复选择 |
| 错误人性化 | ffmpeg 技术报错翻译为「怎么办」的中文提示（如「分辨率必须为偶数…」） |
| 全链路日志 | 所有操作与**完整 ffmpeg 命令行**写入日志文件，界面内可实时查看 |
| 配置持久化 | API 密钥与偏好设置保存在本机 `settings.json` |
| 输出防覆盖 | 同名文件自动追加 `(1)(2)` 序号，不会静默覆盖已有文件 |
| 界面防误操作 | 切换目标格式时自动禁用无意义参数（如 MP3 时禁用视频编码/分辨率/帧率） |

---

## 界面预览

```
┌─ 状态栏：运行环境状态 ────────────────────────── 当前参数摘要 ─┐
│ ┌── 左列 ──────────────────────┐ ┌── 右列 ──────────────────┐ │
│ │ ① 视频信息                    │ │ ③ AI 智能助手             │ │
│ │    [缩略图]  文件名 / 大小 …   │ │    示例 → 需求输入框      │ │
│ │ ② 输出参数                    │ │    [AI 解析] [智能推荐]   │ │
│ │    预设 / 格式 / 编码 / 分辨率 │ │    解析结果与降级提示      │ │
│ │    帧率 / 码率 / 音频 / 目录   │ │ ④ 转换任务队列           │ │
│ │  [加入队列][开始转换][打开目录]│ │    文件名|格式|状态|进度  │ │
│ └──────────────────────────────┘ └──────────────────────────┘ │
│ 停靠面板：运行日志（Ctrl+L）                                     │
└────────────────────────────────────────────────────────────────┘
```

面板编号 ①②③④ 即推荐的操作顺序。

---

## 技术栈

| 层次 | 技术 |
| --- | --- |
| 开发语言 | Python 3.10+ |
| 桌面 GUI | PySide6（Qt6） |
| 视频处理 | ffmpeg + ffprobe **系统命令行**（`subprocess` 调用） |
| 缩略图 | opencv-python（ffmpeg 截帧失败时兜底） |
| AI 接入 | requests 调用 OpenAI 兼容 `/chat/completions` 接口 |
| 环境变量 | python-dotenv |

---

## 项目结构

```
video_converter/
├── main.py                        # 程序入口（加载 .env → 日志 → 环境守卫 → 主窗口）
├── requirements.txt               # 依赖清单
├── README.md                      # 本文档
├── SESSION_SUMMARY.md             # 会话总结（由 utils/summary_gen.py 自动生成）
├── .env.example                   # 环境变量示例
├── config/
│   └── settings.py                # 配置管理、常量表、环境检测、安装指引
├── core/
│   ├── ffprobe_parser.py          # 视频信息解析（调用 ffprobe）
│   ├── ffmpeg_engine.py           # 转码核心（subprocess 调用 ffmpeg + QThread 异步 + 队列调度）
│   └── thumbnail.py               # 缩略图生成（ffmpeg 截帧 + OpenCV 兜底 + 缓存）
├── ai/
│   ├── llm_client.py              # 大模型 API 封装（OpenAI 兼容协议）
│   └── param_interpreter.py       # 自然语言 → ffmpeg 参数（三级降级 + 强校验）
├── ui/
│   ├── main_window.py             # 主窗口（跨模块编排）
│   └── components/                # 自定义组件
│       ├── video_info_panel.py    # ① 视频信息面板（含拖拽）
│       ├── params_panel.py        # ② 输出参数面板
│       ├── ai_panel.py            # ③ AI 交互面板
│       ├── task_queue_panel.py    # ④ 任务队列面板
│       ├── env_dialog.py          # 环境检测 / 安装指引对话框
│       ├── settings_dialog.py     # 设置对话框
│       └── workers.py             # 解析 / 缩略图 / AI 的后台线程
└── utils/
    ├── logger.py                  # 日志工具（控制台 + 滚动文件）
    └── summary_gen.py             # 会话总结生成器
```

分层依赖方向**严格单向**：`ui → ai → core → config / utils`。
`ai/` 层不导入 Qt，可脱离界面单独测试。

---

## 快速开始

### 1. 安装 ffmpeg（必须）

程序不自带 ffmpeg，需要系统已安装 `ffmpeg` 与 `ffprobe`。

**Windows**

```powershell
winget install Gyan.FFmpeg
# 或：scoop install ffmpeg   /   choco install ffmpeg-full
```

**macOS**

```bash
brew install ffmpeg
```

**Ubuntu / Debian**

```bash
sudo apt update && sudo apt install -y ffmpeg
```

安装完成后在终端执行 `ffmpeg -version` 验证。若仍未识别，
可在程序内 **工具 → 设置 → 高级设置 → 手动指定 ffmpeg 路径** 手动指定。

### 2. 安装 Python 依赖

```bash
cd video_converter
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

### 3. 启动程序

```bash
python main.py
```

启动后会先做环境自检：检测到 ffmpeg 时状态栏显示绿色「运行环境：正常」；
未检测到时弹出**安装指引对话框**，可选择继续（部分功能不可用）或退出，程序不会崩溃。

---

## AI 能力配置

AI 功能为**增强项**：不配置任何密钥时，「自然语言转参数」会自动降级为本地规则解析，功能仍然可用。

### 方式一：在程序内填写（推荐）

菜单 **工具 → 设置 → AI 设置**，选择厂商 → 填写 API Key / 接口地址 / 模型 ID → 可点「测试连接」验证 → 保存。

配置保存在 `~/.video_converter_ai/settings.json`，下次启动自动生效。

### 方式二：环境变量

复制 `.env.example` 为 `.env`，按需填写（`.env` 已在 `.gitignore` 中忽略，不会误提交）：

```ini
LLM_PROVIDER=deepseek

DOUBAO_API_KEY=你的火山方舟密钥
DOUBAO_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
DOUBAO_MODEL=ep-2024xxxxxx-xxxxx      # 豆包需填推理接入点 ID

DEEPSEEK_API_KEY=sk-xxxxxxxx
DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
DEEPSEEK_MODEL=deepseek-chat

QWEN_API_KEY=sk-xxxxxxxx
QWEN_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
QWEN_MODEL=qwen-plus

# 可选：自定义 ffmpeg 路径与默认输出目录
# FFMPEG_PATH=D:\ffmpeg\bin\ffmpeg.exe
# FFPROBE_PATH=D:\ffmpeg\bin\ffprobe.exe
# DEFAULT_OUTPUT_DIR=D:\Videos\output
```

配置优先级：**程序内设置 > `.env` 环境变量 > 代码内默认值**。

---

## 使用指南

1. **加载视频**：把视频文件拖到窗口上任意位置，或点 **① 视频信息** 里的「选择视频文件…」（快捷键 `Ctrl+O`）。
   稍等片刻，缩略图与全部元数据会自动填充。
2. **配置输出**：在 **② 输出参数** 里选一个预设（推荐新手）、或手动调整格式与各项参数。
   鼠标悬停 / 切换预设时会显示该预设的适用场景说明。
3. **（可选）用 AI 描述需求**：在 **③ AI 智能助手** 的输入框里用大白话写清需求，
   例如「转成抖音竖屏，30 帧，尽量小一点」，点「AI 解析」→ 参数自动填入参数栏。
   也可点「智能推荐」让程序根据源视频属性给出建议。
4. **加入队列**：点「添加到任务队列」（`Ctrl+Q`）。可重复添加多个视频，队列式执行，默认同时执行 1 个。
5. **开始转换**：点「开始转换」。任务列表中实时显示进度百分比、速度与剩余时间；
   随时可点「取消」或「暂停」。
6. **拿到成品**：转换完成后弹出系统通知，点「打开输出目录」直达文件位置。

**快捷键**：`Ctrl+O` 打开视频 ｜ `Ctrl+Q` 加入队列 ｜ `Ctrl+L` 运行日志 ｜ `Ctrl+,` 设置 ｜ `Ctrl+W` 退出

---

## 内置预设

| 预设 | 参数组合 | 适用场景 |
| --- | --- | --- |
| 通用 1080P | MP4 / H.264 / 1920x1080 / 原始帧率 / 自动码率（CRF 23）/ AAC 192k | 通用场景，兼容性最好 |
| 抖音竖屏 | MP4 / H.264 / 1080x1920 / 30fps / 6000kbps / AAC 128k | 抖音、快手、视频号等竖屏平台 |
| 微信压缩 | MP4 / H.264 / 854x480 / 24fps / 800kbps / AAC 64k | 微信发送，体积小、清晰度可接受 |
| 无损转码 | MKV / 视频音频均 `copy` | 只换封装不重编码，速度极快且零画质损失 |
| 纯音频提取 | MP3 / libmp3lame / 192kbps | 从视频中提取音频 |
| GIF 动图 | GIF / 宽 480（`480x-1`）/ 12fps / 两遍调色板 / `-loop 0` | 制作表情包、动图素材 |
| H.265 小体积 | MP4 / libx265 / 1920x1080 / 自动码率（CRF 23）/ AAC 128k | 追求更小体积、设备支持 H.265 |
| 硬件加速极速转码 | MP4 / H.264 硬件编码器 / 原始分辨率 / 6000kbps / AAC 128k | 有独立显卡时大幅提升转码速度 |

> 上述参数与 `config/settings.py` 中的 `PRESETS` 一一对应，可在此文件中自行增删预设。

---

## 常见问题

**Q：提示「未检测到 ffmpeg」怎么办？**
按对话框里的指引安装 ffmpeg（见[快速开始](#快速开始)），安装后点 **工具 → 重新检测运行环境**；
若已安装但仍未识别，用 **设置 → 高级设置 → 手动指定 ffmpeg 路径** 手动选择。

**Q：AI 解析提示降级 / 失败？**
检查是否已填 API Key 与模型 ID（豆包需填 `ep-` 开头的推理接入点 ID）。
即使完全不配置 AI，也可以手动或用预设完成转换；「AI 解析」此时走本地规则链仍可用。

**Q：转换后文件比原文件大？**
默认使用恒定质量模式（H.264 CRF 23）。想要更小体积，请把视频码率设为具体值（如 `1500k`），
或套用「微信压缩」预设；想换 H.265 可用「H.265 小体积」预设。

**Q：提示「分辨率不合法」？**
H.264/H.265 要求宽高均为偶数。把分辨率改成 1280x720 这类偶数尺寸即可。

**Q：转换速度很慢 / 想看到更多细节？**
按 `Ctrl+L` 打开运行日志面板，可实时看到完整 ffmpeg 输出；
日志文件位于 `~/.video_converter_ai/logs/app.log`。

**Q：配置文件在哪？**
全部运行数据都在 `~/.video_converter_ai/`：
`settings.json`（配置）、`logs/app.log`（日志）、`thumbnails/`（缩略图缓存）。

---

## 开发与测试

### 重新生成会话总结文档

```bash
python utils/summary_gen.py                 # 生成 SESSION_SUMMARY.md
python utils/summary_gen.py --no-tests      # 只采集结构与环境信息，跳过自测
python utils/summary_gen.py -o report.md    # 指定输出路径
```

`summary_gen.py` 会**真实执行** 20+ 条纯逻辑自测用例（命令构建、参数校验、JSON 提取、
规则解析、错误翻译、路径处理等），并把运行结果写入文档，因此
[SESSION_SUMMARY.md](SESSION_SUMMARY.md) 中的测试结论始终与当前代码一致。

### 各模块单独验证

```bash
# 1) 检查 ffmpeg 环境
python -c "from config.settings import check_environment; s=check_environment(); print(s.available, s.ffmpeg_version)"

# 2) 用 ffprobe 解析一个视频
python -c "from core.ffprobe_parser import parse_video; print(parse_video(r'D:\demo.mp4').summary_dict())"

# 3) 只看命令不执行（纯函数，安全）
python -c "from core.ffmpeg_engine import OutputParams, build_ffmpeg_command; print(' '.join(build_ffmpeg_command(OutputParams(format='MP4',resolution='1920x1080'), 'in.mp4', 'out.mp4')) )"

# 4) 离线规则解析（不走网络）
python -c "from ai.param_interpreter import interpret_text; r=interpret_text('转成抖音竖屏，30 帧'); print(r.source_label, r.params.describe())"
```

---

## 已知限制

- 暂不支持视频剪辑（起止时间裁切）、拼接、水印与字幕烧录。
- 源视频时长未知时（部分流媒体容器），进度条只显示处理速度，不显示百分比。
- 队列并发上限为 3，避免机械盘 IO 与 CPU 争抢。
- 硬件加速启用后，画质/体积与软件编码存在差异，需要更小体积时建议关闭。

---

## 许可证

本项目为示例/面试练习项目，可自由用于学习与演示。
