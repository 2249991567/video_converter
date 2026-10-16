"""
会话总结生成器
==============

职责：在项目根目录自动生成 `SESSION_SUMMARY.md`。

与「手写文档」不同，本模块会**动态采集**以下真实数据写入文档，保证总结内容与代码同步：
- 当前系统与运行环境信息（Python 版本、ffmpeg 路径与版本、数据目录）
- 项目模块清单与代码规模（文件数、类数、函数数、行数）
- 内置预设 / 输出格式 / 大模型厂商等常量表
- 一组**可执行的自测用例**及其真实运行结果（纯逻辑层，无需 GUI 与网络）

用法：
    python utils/summary_gen.py                     # 生成到项目根目录
    python utils/summary_gen.py -o 自定义路径.md     # 指定输出文件
"""

from __future__ import annotations

import argparse
import ast
import os
import platform
import sys
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_OUTPUT = PROJECT_ROOT / "SESSION_SUMMARY.md"

# 需要统计代码规模的目录 / 文件
SOURCE_DIRS = ("config", "core", "ai", "ui", "utils")
SOURCE_FILES = ("main.py",)

# 文档中需要跳过统计的目录名
_EXCLUDED_DIRS = {"__pycache__", ".git", ".venv", "venv", "build", "dist"}


# ======================================================================
# 一、自测用例（纯逻辑层，不依赖 GUI 与网络）
# ======================================================================

@dataclass
class CaseResult:
    """单条测试用例的执行结果。"""

    name: str
    passed: bool
    detail: str


def _case_extract_json_fenced() -> str:
    """模型返回带 Markdown 围栏的 JSON 时也能正确提取。"""
    from ai.param_interpreter import extract_json

    raw = '```json\n{"format": "MP4", "vcodec": "libx264"}\n```'
    data = extract_json(raw)
    assert data is not None, "未能提取 JSON"
    assert data.get("format") == "MP4", f"format 字段错误：{data}"
    return "从 ```json 围栏中成功提取字段"


def _case_extract_json_with_noise() -> str:
    """JSON 前后夹杂解释文字时也能正确提取。"""
    from ai.param_interpreter import extract_json

    raw = '好的，这是结果：{"format": "MP3"} 请查收。'
    data = extract_json(raw)
    assert data is not None and data.get("format") == "MP3", f"结果异常：{data}"
    return "从夹杂文本中成功提取字段"


def _case_coerce_rejects_illegal() -> str:
    """非法编码器 / 分辨率必须被拒绝并回退默认值，同时给出告警。"""
    from ai.param_interpreter import coerce_params

    params, warnings = coerce_params(
        {"format": "MP4", "vcodec": "hack_codec", "resolution": "abc", "fps": "999"}
    )
    assert params.vcodec == "libx264", f"vcodec 未回退：{params.vcodec}"
    assert params.resolution == "原始分辨率", f"resolution 未回退：{params.resolution}"
    assert params.fps == "原始帧率", f"fps 未回退：{params.fps}"
    assert len(warnings) == 3, f"告警数量应为 3，实际 {len(warnings)}"
    return f"3 项非法值全部回退默认，产出 {len(warnings)} 条告警"


def _case_coerce_audio_only_consistency() -> str:
    """选择 MP3 时自动清空视频相关参数，保证参数自洽。"""
    from ai.param_interpreter import coerce_params

    params, _ = coerce_params(
        {"format": "MP3", "vcodec": "libx264", "resolution": "1920x1080", "fps": "30"}
    )
    assert params.vcodec == "copy", f"vcodec 应为 copy：{params.vcodec}"
    assert params.resolution == "原始分辨率", f"resolution 应为 原始分辨率：{params.resolution}"
    assert params.fps == "原始帧率", f"fps 应为 原始帧率：{params.fps}"
    return "MP3 目标格式下视频参数已自动清空"


def _case_sanitize_extra_args() -> str:
    """附加参数必须剔除 -i / 输出文件 / shell 元字符等危险内容。"""
    from ai.param_interpreter import coerce_params

    params, warnings = coerce_params({"extra_args": "-crf 18 -i evil.mp4 ; rm -rf"})
    assert "-i" not in params.extra_args, f"危险参数未剔除：{params.extra_args}"
    assert (";" not in params.extra_args) and ("|" not in params.extra_args), "元字符未过滤"
    assert "-crf 18" in params.extra_args, f"合法参数被误删：{params.extra_args}"
    return f"保留合法参数、剔除危险参数，结果：{params.extra_args}"


def _case_rule_parse_size_bitrate() -> str:
    """「500M / 5 分钟」应推算出约 12Mbps 的视频码率（离线规则解析）。"""
    from ai.param_interpreter import rule_based_parse

    params, _ = rule_based_parse("转成 1080P 的 MP4，文件控制在 500M 以内，时长 5 分钟")
    bitrate = int(params.v_bitrate.replace("k", ""))
    assert params.resolution == "1920x1080", f"分辨率错误：{params.resolution}"
    # 500MB * 8192 / 300s * 0.9 ≈ 12288kbps，再取整到百位
    assert 10000 <= bitrate <= 14000, f"码率推算不合理：{params.v_bitrate}"

    small_params, _ = rule_based_parse("压到 100M 以内，时长 5 分钟")
    small_bitrate = int(small_params.v_bitrate.replace("k", ""))
    assert small_bitrate < bitrate, "体积上限降低后码率应同步降低"
    return (
        f"500MB/300s → {params.v_bitrate}，100MB/300s → {small_params.v_bitrate}，"
        f"分辨率 {params.resolution}"
    )


def _case_rule_parse_vertical() -> str:
    """「抖音竖屏」应识别为 1080x1920 竖屏分辨率。"""
    from ai.param_interpreter import rule_based_parse

    params, _ = rule_based_parse("转成抖音竖屏视频，30 帧")
    assert params.resolution == "1080x1920", f"竖屏识别失败：{params.resolution}"
    assert params.fps == "30", f"帧率识别失败：{params.fps}"
    return f"识别为 {params.resolution} / {params.fps}fps"


def _case_rule_parse_lossless() -> str:
    """「无损、只换容器」应切换为流复制模式。"""
    from ai.param_interpreter import rule_based_parse

    params, _ = rule_based_parse("无损转换，只换容器不要重新编码")
    assert params.vcodec == "copy" and params.acodec == "copy", "未切换为流复制"
    return "已切换为 -c:v copy / -c:a copy"


def _case_scale_filter() -> str:
    """分辨率字符串到 scale 滤镜的翻译必须正确。"""
    from core.ffmpeg_engine import _build_scale_filter

    assert _build_scale_filter("原始分辨率") is None, "原始分辨率不应加滤镜"
    assert _build_scale_filter("480x-1") == "scale=480:-1", "宽度自适应翻译错误"
    fixed = _build_scale_filter("1920x1080")
    assert fixed and "force_original_aspect_ratio=decrease" in fixed and "pad=" in fixed, (
        f"固定分辨率应加 pad 防变形：{fixed}"
    )
    assert _build_scale_filter("1080") == "scale=-2:1080", "按高度缩放翻译错误"
    return "原始分辨率 / 480x-1 / 1920x1080 / 1080 四种写法翻译全部正确"


def _case_command_mp4() -> str:
    """MP4 1080P 命令应包含编码器、滤镜、faststart 与进度协议，且不使用封装库。"""
    from core.ffmpeg_engine import OutputParams, build_ffmpeg_command

    params = OutputParams(
        format="MP4", vcodec="libx264", resolution="1920x1080",
        fps="30", v_bitrate="6000k", acodec="aac", a_bitrate="128k",
    )
    command = build_ffmpeg_command(params, "in.mp4", "out.mp4", has_audio=True)
    joined = " ".join(command)
    assert "ffmpeg" in Path(command[0]).name.lower(), f"未调用 ffmpeg：{command[0]}"
    assert "-c:v libx264" in joined and "-b:v 6000k" in joined, "编码/码率参数缺失"
    assert "-vf scale=1920:1080" in joined, "分辨率滤镜缺失"
    assert "-movflags +faststart" in joined, "faststart 缺失"
    assert "-progress pipe:1" in joined and "-nostats" in joined, "进度协议缺失"
    assert command[-1] == "out.mp4", "输出文件不是最后一个参数"
    return f"命令共 {len(command)} 个参数，关键参数齐全"


def _case_command_copy_no_filter() -> str:
    """流复制模式下不允许叠加 scale / 帧率等重编码参数。"""
    from core.ffmpeg_engine import OutputParams, build_ffmpeg_command

    params = OutputParams(
        format="MKV", vcodec="copy", resolution="1920x1080", fps="30",
        acodec="copy", a_bitrate="自动",
    )
    command = build_ffmpeg_command(params, "in.mkv", "out.mkv", has_audio=True)
    joined = " ".join(command)
    assert "-c:v copy" in joined, "未使用流复制"
    assert "-vf" not in joined, "流复制模式不应出现 -vf"
    assert "-r 30" not in joined, "流复制模式不应出现 -r"
    assert "-b:a" not in joined, "acodec=copy 时不应出现 -b:a"
    return "流复制模式已正确跳过所有重编码参数"


def _case_command_gif() -> str:
    """GIF 必须走两遍调色板滤镜链并关闭音频。"""
    from core.ffmpeg_engine import OutputParams, build_ffmpeg_command

    params = OutputParams(
        format="GIF", vcodec="copy", resolution="480x-1", fps="12",
        acodec="copy", extra_args="-loop 0",
    )
    command = build_ffmpeg_command(params, "in.mp4", "out.gif", has_audio=True)
    joined = " ".join(command)
    assert "palettegen" in joined and "paletteuse" in joined, "缺少调色板优化"
    assert "-an" in joined, "GIF 应关闭音频"
    assert "-loop 0" in joined, "循环参数缺失"
    return "已启用 palettegen/paletteuse 两遍算法并关闭音频"


def _case_command_audio_only() -> str:
    """纯音频格式必须加 -vn 丢弃视频流。"""
    from core.ffmpeg_engine import OutputParams, build_ffmpeg_command

    params = OutputParams(format="MP3", acodec="libmp3lame", a_bitrate="192k")
    command = build_ffmpeg_command(params, "in.mp4", "out.mp3", has_audio=True)
    joined = " ".join(command)
    assert "-vn" in joined, "缺少 -vn"
    assert "-c:a libmp3lame" in joined and "-b:a 192k" in joined, "音频参数缺失"
    return "已正确生成纯音频提取命令"


def _case_from_dict_type_safety() -> str:
    """参数反序列化必须做类型兜底，绝不因脏数据抛异常。"""
    from core.ffmpeg_engine import OutputParams

    params = OutputParams.from_dict({"hw_accel": "true", "fps": 30, "unknown": 1, "resolution": ""})
    assert params.hw_accel is True, "字符串 true 未转成布尔"
    assert params.fps == "30", f"整数未转成字符串：{params.fps}"
    assert params.resolution == "原始分辨率", "空字符串应保持默认值"
    assert OutputParams.from_dict(None).format == "MP4", "None 输入应返回默认参数"
    return "脏数据全部安全兜底，无异常抛出"


def _case_eta_format() -> str:
    """剩余时间格式化必须覆盖秒 / 分 / 小时三种量级。"""
    from core.ffmpeg_engine import _format_eta

    assert _format_eta(45) == "45秒", _format_eta(45)
    assert _format_eta(125) == "2分5秒", _format_eta(125)
    assert _format_eta(3720) == "1小时2分", _format_eta(3720)
    assert _format_eta(0) == "即将完成", _format_eta(0)
    assert _format_eta(-3) == "计算中", _format_eta(-3)
    return "秒 / 分 / 小时 / 临完成 / 无效值五种情况全部正确"


def _case_humanize_ffmpeg_error() -> str:
    """ffmpeg 报错必须被翻译成用户能看懂的中文提示。"""
    from core.ffmpeg_engine import humanize_ffmpeg_error

    cases = {
        "Permission denied": "文件被占用",
        "Unknown encoder 'h264_nvenc'": "编码器不可用",
        "No space left on device": "磁盘空间不足",
        "moov atom not found": "文件损坏",
    }
    harness = []
    for raw, expect in cases.items():
        message = humanize_ffmpeg_error(raw)
        assert expect in message, f"{raw} 未翻译为「{expect}」：{message}"
        harness.append(expect)
    return "已翻译 4 类常见错误：" + "、".join(harness)


def _case_humanize_probe_error() -> str:
    """ffprobe 报错同样需要人性化。"""
    from core.ffprobe_parser import humanize_probe_error

    assert "文件不存在" in humanize_probe_error("No such file or directory")
    assert "文件损坏" in humanize_probe_error("moov atom not found")
    return "空输入与文件损坏两类错误翻译正确"


def _case_parse_fraction() -> str:
    """帧率分数解析（含 0/0 与 N/A 异常值）必须稳健。"""
    from core.ffprobe_parser import _parse_fraction

    assert abs(_parse_fraction("30000/1001") - 29.97) < 0.01, "分数解析错误"
    assert _parse_fraction("0/0") == 0.0, "0/0 应返回 0"
    assert _parse_fraction("N/A") == 0.0, "N/A 应返回 0"
    assert _parse_fraction(None) == 0.0, "None 应返回 0"
    assert _parse_fraction("25") == 25.0, "整数帧率解析错误"
    return "30000/1001 → 29.97，异常值全部安全归零"


def _case_probe_video_info_defaults() -> str:
    """VideoInfo 在无任何流信息时也必须返回可展示的完整字段。"""
    from core.ffprobe_parser import VideoInfo

    info = VideoInfo()
    summary = info.summary_dict()
    assert "无视频流" in summary["分辨率"], summary["分辨率"]
    assert summary["音频编码"] == "无音频流", summary["音频编码"]
    assert len(summary) == 13, f"展示字段数量应为 13，实际 {len(summary)}"
    return f"13 个展示字段在空数据下均有中文兜底（共 {len(summary)} 项）"


def _case_build_output_path() -> str:
    """输出路径必须按格式切换扩展名，且同名时不覆盖已有文件。"""
    import tempfile

    from core.ffmpeg_engine import build_output_path

    with tempfile.TemporaryDirectory() as tmp:
        first = build_output_path("/tmp/demo.mp4", tmp, "MP4")
        assert first.endswith("demo.mp4"), first
        Path(first).write_text("x", encoding="utf-8")
        second = build_output_path("/tmp/demo.mp4", tmp, "MP3")
        assert second.endswith("demo.mp3"), second
        Path(second).write_text("x", encoding="utf-8")
        third = build_output_path("/tmp/demo.mp4", tmp, "MP3")
        assert third.endswith("demo(1).mp3"), f"同名文件未自动追加序号：{third}"
    return "扩展名随格式切换，同名文件自动追加序号"


def _case_allowed_formats() -> str:
    """题目要求的 6 种输出格式必须全部支持。"""
    from config.settings import OUTPUT_FORMATS, PRESETS

    required_formats = {"MP4", "MKV", "AVI", "MOV", "WebM", "MP3"}
    missing = required_formats - set(OUTPUT_FORMATS)
    assert not missing, f"缺少输出格式：{missing}"
    required_presets = {"通用 1080P", "抖音竖屏", "微信压缩", "无损转码", "纯音频提取", "GIF 动图"}
    missing_preset = required_presets - set(PRESETS)
    assert not missing_preset, f"缺少预设：{missing_preset}"
    return f"输出格式 {len(OUTPUT_FORMATS)} 种、内置预设 {len(PRESETS)} 种，题目要求项齐全"


def _case_hardware_probe() -> str:
    """硬件加速探测必须在任意机器上都安全返回（无 ffmpeg 时为不可用）。"""
    from core.ffmpeg_engine import detect_hardware_acceleration

    info = detect_hardware_acceleration(force=True)
    assert info.label, "label 不应为空"
    return f"探测结果：{info.label}；{info.detail}"


def _case_environment() -> str:
    """环境检测必须返回结构化结果而非抛出异常。"""
    from config.settings import check_environment

    status = check_environment()
    assert isinstance(status.problems, list), "problems 应为列表"
    state = "可用" if status.available else "不可用"
    return f"ffmpeg {state}：{status.message}"


TEST_CASES: List[Tuple[str, Callable[[], str]]] = [
    ("AI：从 Markdown 围栏提取 JSON", _case_extract_json_fenced),
    ("AI：从混杂文本提取 JSON", _case_extract_json_with_noise),
    ("AI：非法字段强校验回退", _case_coerce_rejects_illegal),
    ("AI：纯音频格式参数自洽", _case_coerce_audio_only_consistency),
    ("AI：附加参数安全清洗", _case_sanitize_extra_args),
    ("AI：体积+时长推算码率（离线规则）", _case_rule_parse_size_bitrate),
    ("AI：抖音竖屏识别（离线规则）", _case_rule_parse_vertical),
    ("AI：无损/只换容器识别（离线规则）", _case_rule_parse_lossless),
    ("命令：分辨率 → scale 滤镜翻译", _case_scale_filter),
    ("命令：MP4 1080P 完整命令构建", _case_command_mp4),
    ("命令：流复制模式不叠加滤镜", _case_command_copy_no_filter),
    ("命令：GIF 两遍调色板算法", _case_command_gif),
    ("命令：纯音频提取加 -vn", _case_command_audio_only),
    ("参数：反序列化类型兜底", _case_from_dict_type_safety),
    ("队列：剩余时间格式化", _case_eta_format),
    ("容错：ffmpeg 报错人性化翻译", _case_humanize_ffmpeg_error),
    ("容错：ffprobe 报错人性化翻译", _case_humanize_probe_error),
    ("解析：帧率分数与异常值处理", _case_parse_fraction),
    ("解析：空数据下展示字段兜底", _case_probe_video_info_defaults),
    ("文件：输出路径扩展名与防覆盖", _case_build_output_path),
    ("需求：6 种输出格式 + 6 种预设", _case_allowed_formats),
    ("环境：硬件加速探测容错", _case_hardware_probe),
    ("环境：ffmpeg/ffprobe 检测", _case_environment),
]


def run_test_cases() -> List[CaseResult]:
    """依次执行全部自测用例，任何异常都记为失败而不会中断整体流程。"""
    results: List[CaseResult] = []
    for name, func in TEST_CASES:
        try:
            detail = func()
            results.append(CaseResult(name=name, passed=True, detail=str(detail)))
        except AssertionError as exc:
            results.append(CaseResult(name=name, passed=False, detail=f"断言失败：{exc}"))
        except ImportError as exc:
            results.append(CaseResult(name=name, passed=False, detail=f"依赖缺失，跳过：{exc}"))
        except Exception as exc:  # noqa: BLE001  自测框架必须兜底
            results.append(
                CaseResult(name=name, passed=False, detail=f"异常：{type(exc).__name__}: {exc}")
            )
    return results


# ======================================================================
# 二、项目结构统计
# ======================================================================

@dataclass
class FileStat:
    relative_path: str
    lines: int
    classes: int
    functions: int
    docstring: bool


def _count_code_objects(tree: ast.AST) -> Tuple[int, int]:
    classes = sum(1 for node in ast.walk(tree) if isinstance(node, ast.ClassDef))
    functions = sum(
        1 for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not node.name.startswith("_")  # 只统计对外方法，私有辅助不计入
    )
    return classes, functions


def collect_file_stats() -> List[FileStat]:
    """解析全部源码文件，统计行数、类数、公开函数数。"""
    stats: List[FileStat] = []
    targets: List[Path] = [PROJECT_ROOT / name for name in SOURCE_FILES]
    for directory in SOURCE_DIRS:
        base = PROJECT_ROOT / directory
        if base.exists():
            targets.extend(sorted(base.rglob("*.py")))

    for file_path in targets:
        if not file_path.is_file():
            continue
        if any(part in _EXCLUDED_DIRS for part in file_path.parts):
            continue
        try:
            source = file_path.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            stats.append(
                FileStat(str(file_path.relative_to(PROJECT_ROOT)), 0, 0, 0, False)
            )
            print(f"  [语法错误] {file_path}: {exc}", file=sys.stderr)
            continue
        classes, functions = _count_code_objects(tree)
        stats.append(
            FileStat(
                relative_path=str(file_path.relative_to(PROJECT_ROOT)).replace(os.sep, "/"),
                lines=len(source.splitlines()),
                classes=classes,
                functions=functions,
                docstring=bool(ast.get_docstring(tree)),
            )
        )
    stats.sort(key=lambda item: item.relative_path)
    return stats


# ======================================================================
# 三、文档生成
# ======================================================================

REQUIREMENT_SECTIONS = """\
### 一票否决项（硬性约束）

| 约束 | 落实方式 |
| --- | --- |
| 必须是原生桌面客户端 | 使用 PySide6（Qt6）构建，拥有主窗口、菜单栏、状态栏、停靠日志面板、系统托盘 |
| 必须用 ffmpeg 原生命令行 + subprocess | 全部转码/探测/截图能力均在 `core/` 与 `utils/` 中通过 `subprocess.run/Popen` 调用 ffmpeg / ffprobe 可执行文件，未引入 ffmpeg-python、PyAV 等任何封装库 |
| 必须生成 SESSION_SUMMARY.md | 由 `utils/summary_gen.py` 自动生成（本文件），内容随代码实时采集 |
| 功能可运行、可测试、面向普通用户 | 使用中文界面、内置预设、人性化错误翻译、一键打开输出目录、拖拽导入 |

### 基础功能（题目要求）

| 编号 | 需求 | 实现位置 |
| --- | --- | --- |
| B1 | 文件选择 + 拖拽导入 | `ui/components/video_info_panel.py`、`ui/main_window.py` |
| B2 | ffprobe 解析并展示 10 项元数据 | `core/ffprobe_parser.py`、`VideoInfo.summary_dict()` |
| B3 | 中间帧缩略图实时预览 | `core/thumbnail.py` + `ThumbnailWorker` |
| B4 | 6 种输出格式（MP4/MKV/AVI/MOV/WebM/MP3） | `config/settings.py` → `OUTPUT_FORMATS` |
| B5 | 6 项可调参数（视频编码/分辨率/帧率/视频码率/音频编码/音频码率） | `ui/components/params_panel.py` |
| B6 | ≥6 种常用预设 | `config/settings.py` → `PRESETS`（实际 8 种） |
| B7 | 实时进度（百分比/速度/剩余时间） | `FFmpegWorker._handle_progress_line()` 解析 `-progress pipe:1` |
| B8 | 中途取消 | `FFmpegWorker.cancel()` → `terminate` / `kill` |
| B9 | 完成弹窗 + 一键打开输出目录 | `MainWindow._on_task_finished()` / `_open_path()` |
| B10 | 批量任务队列，默认并发 1 | `ConversionManager` + `ui/components/task_queue_panel.py` |
| B11 | 启动自动环境检测 + 安装指引 + 禁止崩溃 | `config/settings.check_environment()`、`ui/components/env_dialog.py` |

### AI 增强功能

| 编号 | 需求 | 实现位置 |
| --- | --- | --- |
| A1 | 自然语言 → ffmpeg 参数 | `ai/param_interpreter.py` + `ai/llm_client.py` |
| A2 | 严格 JSON Prompt 约束 | `param_interpreter.SYSTEM_PROMPT`（白名单 + 决策规则 + Few-shot） |
| A3 | 返回格式异常自动降级 | 三级降级链：LLM → 本地正则规则 → 通用 1080P 默认 |
| A4 | 智能参数推荐 | `ParamInterpreter.recommend()` 按源视频短边/帧率/码率决策 |
| A5 | 显卡检测 + 硬件加速 | `detect_hardware_acceleration()` + `apply_hardware_acceleration()` |

### 自主优化点

| 编号 | 优化点 | 实现位置 |
| --- | --- | --- |
| O1 | 输出目录自动记忆 | `AppSettings.output_dir` 持久化 + `ParamsPanel` |
| O2 | ffmpeg 错误人性化翻译 | `humanize_ffmpeg_error()` / `humanize_probe_error()` |
| O3 | 转换完成系统通知 | `QSystemTrayIcon.showMessage()`（不可用时降级为状态栏提示） |
| O4 | 全链路日志 | `utils/logger.py`（滚动文件 5MB×5）+ `ConversionManager.logMessage` |
| O5 | 本地配置持久化 | `~/.video_converter_ai/settings.json` |
"""


def _render_env_section() -> str:
    """渲染运行环境信息表格（全部来自真实探测）。"""
    lines = ["| 项目 | 值 |", "| --- | --- |"]
    lines.append(f"| 操作系统 | {platform.system()} {platform.release()}（{platform.machine()}） |")
    lines.append(f"| Python 版本 | {sys.version.split()[0]} |")
    lines.append(f"| Python 解释器 | {sys.executable} |")

    try:
        from config.settings import (
            CONFIG_FILE,
            DATA_DIR,
            get_ffmpeg_path,
            get_ffmpeg_version,
            get_ffprobe_path,
        )

        lines.append(f"| ffmpeg 路径 | {get_ffmpeg_path() or '未检测到'} |")
        lines.append(f"| ffprobe 路径 | {get_ffprobe_path() or '未检测到'} |")
        lines.append(f"| ffmpeg 版本 | {get_ffmpeg_version() or '未知'} |")
        lines.append(f"| 数据目录 | {DATA_DIR} |")
        lines.append(f"| 配置文件 | {CONFIG_FILE} |")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"| 环境探测 | 失败：{exc} |")

    try:
        import PySide6

        lines.append(f"| PySide6 版本 | {PySide6.__version__} |")
    except Exception:  # noqa: BLE001
        lines.append("| PySide6 版本 | 未安装 |")

    try:
        import cv2

        lines.append(f"| OpenCV 版本 | {cv2.__version__} |")
    except Exception:  # noqa: BLE001
        lines.append("| OpenCV 版本 | 未安装 |")

    try:
        import requests

        lines.append(f"| requests 版本 | {requests.__version__} |")
    except Exception:  # noqa: BLE001
        lines.append("| requests 版本 | 未安装 |")

    return "\n".join(lines)


def _render_module_table(stats: List[FileStat]) -> str:
    """渲染模块清单表格。"""
    lines = ["| 文件 | 行数 | 类数 | 公开函数数 | 模块文档 |", "| --- | ---: | ---: | ---: | :---: |"]
    for item in stats:
        lines.append(
            f"| `{item.relative_path}` | {item.lines} | {item.classes} | "
            f"{item.functions} | {'有' if item.docstring else '无'} |"
        )
    total_lines = sum(item.lines for item in stats)
    total_classes = sum(item.classes for item in stats)
    total_functions = sum(item.functions for item in stats)
    lines.append(
        f"| **合计 {len(stats)} 个文件** | **{total_lines}** | **{total_classes}** | "
        f"**{total_functions}** | — |"
    )
    return "\n".join(lines)


def _render_test_table(results: List[CaseResult]) -> str:
    """渲染测试结果表格。"""
    lines = ["| 编号 | 测试用例 | 结果 | 说明 |", "| --- | --- | :---: | --- |"]
    for index, item in enumerate(results, start=1):
        mark = "通过" if item.passed else "失败"
        detail = item.detail.replace("|", "\\|").replace("\n", " ")
        lines.append(f"| T{index:02d} | {item.name} | {mark} | {detail} |")

    passed = sum(1 for item in results if item.passed)
    lines.append("")
    lines.append(
        f"**汇总：共 {len(results)} 条用例，通过 {passed} 条，失败 "
        f"{len(results) - passed} 条，通过率 {passed / len(results) * 100:.1f}%。**"
    )
    return "\n".join(lines)


def _render_presets_table() -> str:
    """渲染内置预设表格。"""
    try:
        from config.settings import PRESETS
    except Exception:  # noqa: BLE001
        return "_常量表读取失败_"

    lines = ["| 预设名称 | 参数组合 | 适用场景 |", "| --- | --- | --- |"]
    for name, profile in PRESETS.items():
        params = profile.get("params", {})
        if profile.get("params", {}).get("format") == "GIF":
            combo = f"GIF / {params.get('fps')} / 宽 {params.get('resolution')}"
        elif params.get("format") == "MP3":
            combo = f"MP3 / {params.get('acodec')} / {params.get('a_bitrate')}"
        else:
            combo = (
                f"{params.get('format')} / {params.get('vcodec')} / "
                f"{params.get('resolution')} / {params.get('v_bitrate')}"
            )
        lines.append(f"| {name} | {combo} | {profile.get('description', '')} |")
    return "\n".join(lines)


def _render_format_table() -> str:
    """渲染输出格式表格。"""
    try:
        from config.settings import OUTPUT_FORMATS
    except Exception:  # noqa: BLE001
        return "_常量表读取失败_"

    lines = ["| 格式 | 扩展名 | 容器 | 默认视频编码 | 默认音频编码 | 特殊处理 |", "| --- | --- | --- | --- | --- | --- |"]
    for key, profile in OUTPUT_FORMATS.items():
        if profile.get("audio_only"):
            special = "仅保留音频（-vn）"
        elif profile.get("gif"):
            special = "两遍调色板 + 关闭音频"
        else:
            special = "—"
        lines.append(
            f"| {key} | {profile.get('ext')} | {profile.get('container')} | "
            f"{profile.get('vcodec') or '—'} | {profile.get('acodec') or '—'} | {special} |"
        )
    return "\n".join(lines)


def generate_summary(output_path: Path = DEFAULT_OUTPUT, run_tests: bool = True) -> Path:
    """
    生成 SESSION_SUMMARY.md。

    :param output_path: 输出文件路径
    :param run_tests: 是否执行自测用例并把真实结果写入文档
    :return: 实际写入的文件路径
    """
    print("正在采集项目信息…")
    stats = collect_file_stats()
    print(f"  · 源码文件 {len(stats)} 个，合计 {sum(item.lines for item in stats)} 行")

    results: List[CaseResult] = []
    if run_tests:
        print(f"正在执行 {len(TEST_CASES)} 条自测用例…")
        results = run_test_cases()
        passed = sum(1 for item in results if item.passed)
        print(f"  · 通过 {passed}/{len(results)}")

    passed = sum(1 for item in results if item.passed) if results else 0
    failed = len(results) - passed
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    sections: List[str] = []
    sections.append(
        f"""# SESSION_SUMMARY · AI 视频格式转换器

> 本文档由 `utils/summary_gen.py` 自动生成，用于记录本项目的需求拆解、技术选型、
> 实现方案、问题排查与测试结果。
>
> - 生成时间：{generated_at}
> - 项目根目录：`{PROJECT_ROOT}`
> - 代码规模：{len(stats)} 个源码文件 / {sum(item.lines for item in stats)} 行
> - 自测用例：{len(results)} 条，通过 {passed} 条，失败 {failed} 条
"""
    )

    sections.append(
        f"""---

## 1. 需求拆解与项目目标

### 1.1 项目目标

做一个**普通用户能直接上手使用**的桌面端 AI 视频格式转换器：把「选文件 → 设参数 →
转码 → 拿到成品」这条链路做成有图形界面的产品，同时叠加 AI 能力，让用户能用一句话
（自然语言）替代复杂的转码参数配置。

### 1.2 需求拆解

{REQUIREMENT_SECTIONS}

### 1.3 需求 → 模块映射

```
用户操作                     界面层                      核心层
────────────────────────────────────────────────────────────────────
拖拽 / 选择视频      →  VideoInfoPanel        →  ffprobe_parser.parse_video()
自然语言描述需求     →  AIPanel                →  param_interpreter.interpret()
                                                     └─ llm_client.chat()
调整参数 / 套预设    →  ParamsPanel            →  OutputParams
点击开始转换         →  TaskQueuePanel         →  ConversionManager
                                                    └─ FFmpegWorker(QThread)
                                                        └─ subprocess → ffmpeg
实时进度刷新         →  TaskQueuePanel         ←  -progress pipe:1 解析
```
"""
    )

    sections.append(
        """---

## 2. 技术选型决策过程

### 2.1 为什么用 PySide6（Qt6）而不是 Tkinter / Web

| 方案 | 结论 | 原因 |
| --- | --- | --- |
| Tkinter | 否 | 原生控件过于朴素，表格、进度条、拖拽、托盘通知能力弱，做不出「产品感」 |
| Electron / Web 套壳 | 否 | 违反「必须是原生桌面客户端」的一票否决项；且打包体积巨大 |
| PyQt6 | 备选 | 与 PySide6 能力几乎一致，但 PySide6 是 Qt 官方绑定、License 更宽松（LGPL） |
| **PySide6** | **采用** | 官方 Qt6 绑定；天然支持 QThread + Signal/Slot 异步、QTableWidget、QSystemTrayIcon、QSplitter、拖拽事件，能满足全部交互需求 |

**关键收益**：视频转码是长耗时任务，必须放到后台线程。Qt 的
`QThread + Signal/Slot` 让「线程内计算 → 主线程刷新界面」这条链路既安全又简洁，
且信号天然是跨线程安全的（自动排队投递），比 Python 原生 `threading` 手动加锁可靠得多。

### 2.2 为什么坚持 ffmpeg 原生命令行 + subprocess

- **硬性要求**：题目明确禁止 ffmpeg-python、PyAV 等封装库。
- **工程合理性**：`subprocess` 直接调用 CLI 有三个实打实的好处——
  1. **零依赖耦合**：不依赖任何 Python 绑定库的版本，用户机器上的 ffmpeg 是什么版本就用什么版本；
  2. **可复现**：把拼好的命令原样写进日志，用户可直接复制到终端复现问题，排障成本极低；
  3. **进度可控**：`-progress pipe:1 -nostats` 把机器可读的进度键值对打到 stdout，
     比解析人类可读的 `time=00:00:12.34` 行稳定得多。

- **命令构建方式**：`build_ffmpeg_command()` 是**纯函数**（参数进、列表出），
  不启动任何进程，因此可以在没有 ffmpeg 的机器上做单元测试——这一点在本项目的自测用例中被验证。

### 2.3 为什么把 ffmpeg 的 stderr 合并进 stdout

`stdout=PIPE, stderr=STDOUT` 单管道读取。若把两个管道分开且只读 stdout，
当 stderr 缓冲区写满时子进程会阻塞，导致**经典死锁**。合并为单管道顺序读取最简单可靠。

Windows 上额外加 `creationflags=CREATE_NO_WINDOW`，避免每次转码弹出黑色控制台窗口。

### 2.4 AI 能力的设计思路

三条原则：

1. **统一协议**：豆包（火山方舟）、DeepSeek、通义千问（DashScope 兼容模式）都提供
   OpenAI 兼容的 `/chat/completions` 接口，因此只实现一套 HTTP 调用即可三端通用，
   比引入三个厂商 SDK 更轻、更可维护。
2. **严格约束输出**：`SYSTEM_PROMPT` 用「固定 JSON 结构 + 字段白名单 + 决策规则 + Few-shot」
   四重约束，把大模型的自由度压到最低；
   即便如此仍不信任模型输出——`coerce_params()` 会做**字段级强校验**，任何非法值立即回退默认值。
3. **必须有降级链**：没有 Key、断网、模型乱答这三件事在真实环境里一定会发生。
   因此设计三级降级：

```
LLM 解析  ──失败──▶  本地正则规则解析  ──未命中──▶  通用 1080P 默认参数
   │                        │                          │
   └── response_format 不支持时自动去掉该参数重试         └── 提示用户手动调整
```

**结论**：AI 是「增强」而不是「依赖」。即使完全不配置任何 API Key，
「自然语言转参数」功能依然可用（走本地规则链），程序永远不会因 AI 失败而不可用。

### 2.5 AI 层不导入 Qt

`ai/` 层是纯逻辑层（只依赖 requests / config / core），可以在无图形界面环境下
单独调用与测试。分层依赖方向严格单向：`ui → ai → core → config/utils`，
不允许出现反向依赖。
"""
    )

    sections.append(
        """---

## 3. 核心功能设计与实现方案

### 3.1 视频信息解析（`core/ffprobe_parser.py`）

```
ffprobe -v quiet -print_format json -show_format -show_streams <file>
```

拿到 JSON 后做三件事：

1. **流筛选**：遍历 `streams`，取第一条 `codec_type == "video"` 与 `codec_type == "audio"`；
2. **安全转换**：所有字段都经过 `_to_int / _to_float / _parse_fraction` 兜底，
   因为真实世界的容器千奇百怪——缺 `size`、缺 `bit_rate`、`r_frame_rate` 为 `0/0` 都很常见；
3. **中文渲染**：`summary_dict()` 直接产出「中文标签 → 展示值」的有序字典，UI 遍历即可渲染，
   界面不需要知道任何底层字段名。

帧率解析用 `Fraction("30000/1001")` → 29.97，比字符串解析更精确。

**兜底细节**：文件大小优先取容器 `format.size`，缺失时回退 `os.stat`；
总码率优先取 `format.bit_rate`，缺失时把各流 `bit_rate` 求和。

### 3.2 转码命令构建（`core/ffmpeg_engine.build_ffmpeg_command`）

按目标格式分三条支路：

- **支路一：GIF** — 使用 `-filter_complex` 走两遍调色板算法
  `fps=N,scale=W:-1:flags=lanczos,split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse`，
  画质远优于默认的朴素 GIF 编码，同时 `-an` 关闭音频。
- **支路二：纯音频** — 加 `-vn` 丢弃视频流，只保留 `-c:a`。
- **支路三：常规视频** — 依次拼接编码器、码率、滤镜、音频参数、容器优化参数。

几个容易踩坑的细节，全部已在实现中规避：

| 细节 | 处理方式 |
| --- | --- |
| 流复制（`copy`）叠加 `-vf` 会报错 | `vcodec == "copy"` 时**跳过** scale 与 `-r` |
| 分辨率改变会拉伸变形 | 用 `scale=W:H:force_original_aspect_ratio=decrease` + `pad` 补黑边保持宽高比 |
| 未指定码率时输出体积不可控 | H.264/H.265 默认加 `-crf 23` 恒定质量模式 |
| MP4/MOV 网络播放需缓冲完才能播 | 加 `-movflags +faststart` 把 moov 索引前置 |
| 用户附加参数可能含危险内容 | `_split_extra_args()` 用 `shlex` 拆分，AI 侧另有 `_sanitize_extra_args()` 黑名单清洗 |

### 3.3 进度解析与取消（`FFmpegWorker : QThread`）

ffmpeg 的 `-progress pipe:1` 输出形如：

```
frame=1234
fps=25.3
out_time_us=12345678
speed=1.85x
progress=continue
```

解析要点：

- `out_time_us` / `out_time_ms` **都是微秒**（ffmpeg 历史遗留的命名坑），
  除以 1e6 得到已处理秒数；
- `百分比 = 已处理秒数 / 源视频时长 × 100`，上限钳制到 99.9%，只有收到
  `progress=end` 才置为 100%；
- `剩余时间 = 剩余秒数 / speed`，用 `_format_eta()` 格式化成「2分5秒」这类中文；
- 为避免信号风暴，进度值未增长时**不重复发送**信号。

**取消**：`cancel()` 先 `terminate()` 给 ffmpeg 机会优雅退出，2 秒超时后 `kill()` 强杀；
`_finalize()` 中会删除未写完整的输出文件，避免用户误以为转换成功。

**线程安全红线**：`run()` 内所有逻辑都在 `try/except` 包裹中，
因为 PySide6 中 QThread 内未捕获的异常会直接终止整个进程——这是本项目最重要的一条防御。

### 3.4 队列调度（`ConversionManager : QObject`）

- 每个任务一个 `FFmpegWorker`，用 `dict[task_id, worker]` 持有引用（防止被 GC）；
- `_dispatch()` 在 `len(workers) < max_concurrent` 时不断取 `PENDING` 任务启动，
  实现「默认并发 1、可配置 1~3」；
- `worker.finished.connect(worker.deleteLater)` 是 Qt 官方推荐用法，保证线程对象安全释放；
- 任务结束回调中先 `_workers.pop()` 再判断是否需要补位派发，
  `_check_all_finished()` 在「无活跃线程且无等待任务」时发出 `allFinished`。

### 3.5 硬件加速探测

```
# 第一步：列出这份 ffmpeg 编译进来的硬件编码器
ffmpeg -hide_banner -encoders

# 第二步：对候选编码器做一次小尺寸试编码（真正的可用性验证）
ffmpeg -hide_banner -v error -f lavfi -i testsrc=size=256x144:rate=5:duration=0.4 \
       -c:v h264_qsv -f null -
```

第一步用正则匹配 `h264_nvenc / hevc_nvenc / h264_qsv / h264_amf` 等编码器名，
按 `nvenc > qsv > amf` 优先级挑出候选厂商；**但列表里有 ≠ 本机可用**——
缺驱动、显卡不支持 NVENC、核显被禁用时，都会在打开编码器的瞬间报
`Function not implemented`。因此第二步会对候选厂商的 H.264 编码器各做一次
0.4 秒的合成画面试编码，只有**真正跑通**的方案才判定为可用；
全部候选都失败时明确降级为「不可用」，绝不把用户引向必然失败的转换。
探测结果做**模块级缓存**（首次探测需要启动 ffmpeg 进程，约 1 秒）。

`apply_hardware_acceleration()` 通过 `SOFT_TO_HARD_ENCODER` 映射表把
`libx264 → h264_nvenc`、`libx265 → hevc_nvenc` 等，并同步设置 `hw_accel=True`；
硬件不可用时只关闭开关、不改动其它参数，绝不生成跑不通的命令。

### 3.6 界面结构与交互

主窗口采用 `QSplitter` 左右分栏：

```
┌─ 左列（滚动区） ──────────────┬─ 右列 ────────────────────────┐
│ ① 视频信息（缩略图 + 13 项元数据）│ ③ AI 智能助手                 │
│ ② 输出参数（预设/格式/6 项参数）  │   ├ 示例快速填充               │
│   ├ 硬件加速开关与探测结果        │   ├ 需求输入框                 │
│   └ 输出目录（带记忆）            │   ├ AI 解析 / 智能推荐按钮      │
│ 操作行：加入队列 / 开始转换 /      │   └ 结果说明与降级提示           │
│         打开输出目录             │ ④ 转换任务队列（7 列表格）       │
└──────────────────────────────┴──────────────────────────────┘
状态栏：运行环境状态 + 当前参数摘要        停靠面板：运行日志（Ctrl+L）
```

交互设计上刻意做的「面向普通用户」的取舍：

1. **面板编号 ①②③④** 暗示操作顺序，降低学习成本；
2. **切换输出格式自动套用该容器的默认编码**，并**禁用无意义的控件**
   （如 MP3 时禁用视频编码/分辨率/帧率），从 UI 层面杜绝无效参数组合；
3. **预设描述实时展示**，用户不用理解 libx264 是什么也能选对方案；
4. **AI 结果永远回填到参数栏**，用户可以在 AI 建议基础上继续微调，而不是「黑盒直接转码」。
"""
    )

    sections.append(
        """---

## 4. 优化点决策与效果说明

### O1 输出目录自动记忆

**问题**：每次都从系统默认目录开始选，重复操作多。
**方案**：输出目录写入 `settings.json`，`AppSettings.output_dir` 属性在读取时
顺便 `mkdir(parents=True, exist_ok=True)`，目录被删也能自愈；
另外单独记忆 `last_input_dir`，下次打开文件对话框直接定位到上次的视频目录。
**效果**：第二次启动直接就是上次的路径，批量转码时零额外操作。

### O2 ffmpeg 错误人性化翻译

**问题**：ffmpeg 的报错对普通用户完全不可读，例如
`[libx264 @ ...] width or height not divisible by 2`。
**方案**：`humanize_ffmpeg_error()` 用规则表匹配典型错误，翻译成
「【分辨率不合法】视频宽高必须为偶数，请把分辨率调整为偶数（如 1280x720）后重试」，
同时把原始日志的最后几行附在后面供排查。
覆盖场景：文件不存在、文件被占用、文件损坏、编码器不可用、显卡不可用、
核显不可用、磁盘空间不足、分辨率非法、转换失败、执行超时。
`ffprobe` 侧同样有 `humanize_probe_error()`。
**效果**：用户看到的是「怎么办」，而不是「是什么」。

### O3 转换完成系统通知

**问题**：批量转码耗时较长，用户不会一直盯着界面。
**方案**：`QSystemTrayIcon.showMessage()` 发系统通知；
启动时用 `QSystemTrayIcon.isSystemTrayAvailable()` 判断，托盘不可用或用户
关闭通知开关时自动降级为状态栏提示，**任何情况下都不会静默结束**。
**效果**：可以放心切到别的窗口工作，转完自动收到提醒。

### O4 全链路日志

**问题**：用户报障时无法复现，开发者缺少现场信息。
**方案**：`utils/logger.py` 统一初始化日志（控制台 + 滚动文件，
`RotatingFileHandler` 单文件 5MB、保留 5 份，写入 `~/.video_converter_ai/logs/app.log`）。
记录内容分层：
- 用户操作（加载视频、套预设、加入队列、取消任务）
- **完整 ffmpeg / ffprobe 命令行**（`format_command()` 还原成可直接复制执行的字符串）
- 任务耗时、退出码、原始错误输出、异常堆栈
另外主窗口内置「运行日志」停靠面板（Ctrl+L），展示队列实时输出（`logMessage` 信号），
用户无需打开文件即可自查。
**效果**：一次报障即可定位到具体命令与参数，排障从「猜」变成「看」。

### O5 本地配置持久化

**问题**：API Key、并发数、通知开关每次重启都要重配。
**方案**：`AppSettings` 读写 `~/.video_converter_ai/settings.json`（而非项目目录，
避免污染代码仓库、也避免只读目录写入失败）。优先级：
`程序内设置 > .env 环境变量 > 代码内默认值`。
配置文件损坏时 `load()` 静默回退默认值，**绝不因配置损坏导致无法启动**。
**效果**：配置一次、长期生效；Key 也可写在 `.env` 里（`.gitignore` 已忽略）。

### 额外优化（超出题目要求）

- **附加优化 A：预设扩展**。题目要求 ≥6 种，实际内置 **8 种**，
  额外增加「H.265 小体积」「硬件加速极速转码」两个实用场景。
- **附加优化 B：缩略图两级容错 + 缓存**。优先 ffmpeg `-ss` 快速定位截帧，
  失败时降级 OpenCV `VideoCapture` 抓帧；缓存以「绝对路径 + mtime + size」的 MD5 命名，
  命中直接复用，避免每次加载重复截图。
- **附加优化 C：输出文件防覆盖**。`build_output_path()` 检测同名文件自动追加 `(1)(2)` 序号，
  避免用户连续转同一个视频时互相覆盖。
- **附加优化 D：AI 复核模式**。`recommend(use_llm=True)` 可让大模型在本地规则推荐结果
  基础上做一次复核，复核失败自动退回规则结果。
"""
    )

    sections.append(
        """---

## 5. 开发中遇到的问题与对应的解决方案

### 问题 1：QThread 内未捕获异常会导致整个 GUI 进程直接崩溃

**现象**：PySide6 中 QThread 的 `run()` 抛出未捕获异常时，不会像普通线程那样只打印堆栈，
而是直接终止进程（退出码非 0），界面瞬间消失。
**解决**：为所有 QThread 子类（`ProbeWorker` / `ThumbnailWorker` / `InterpretWorker` / `FFmpegWorker`）
的 `run()` 统一套 `try/except` 兜底：捕获后通过 `failed` / `finishedTask` 信号把
中文错误信息送回主线程，线程本身绝不向外抛。这条规则现在是本项目的**硬性编码约定**。

### 问题 2：subprocess 双管道读取造成死锁

**现象**：若 `stdout=PIPE, stderr=PIPE` 但只读 stdout，ffmpeg 写满 stderr 缓冲区后会阻塞，
子进程不退出、父进程一直 `readline()`，界面表现为「卡住不动」。
**解决**：统一使用 `stdout=subprocess.PIPE, stderr=subprocess.STDOUT` 合并为单管道顺序读取；
同时 `stdin=DEVNULL` 避免 ffmpeg 等待输入。

### 问题 3：Windows 下每次转码弹出黑色控制台窗口

**现象**：`subprocess` 启动 ffmpeg 时会闪出 cmd 黑窗，非常影响观感。
**解决**：统一加 `creationflags=CREATE_NO_WINDOW (0x08000000)`，
封装为 `config/settings.CREATE_NO_WINDOW` 常量，非 Windows 平台该值为 0（无副作用）。

### 问题 4：`out_time_ms` 其实输出的是微秒

**现象**：按名称理解成毫秒，算出的进度比实际慢 1000 倍。
**解决**：查证 ffmpeg 源码后确认 `out_time_us` 与 `out_time_ms` 输出单位**都是微秒**，
统一除以 `1_000_000` 换算成秒，并在代码里加注释说明这个坑，防止后来者改错。

### 问题 5：大模型返回的 JSON 不稳定

**现象**：模型有时把 JSON 包在 ```json 围栏里，有时前后加一句「好的，结果如下：」，
有时给出不在白名单里的编码器名（例如凭空编一个 `h265_best`）。
**解决**：三层防御——
1. `extract_json()` 先剥离 Markdown 围栏，`json.loads` 失败时再用
   `find("{") / rfind("}")` 截取最长 JSON 片段重试；
2. `coerce_params()` 对每个字段做白名单 / 正则校验，非法值回退默认并记录 `warnings`；
3. 请求侧加 `response_format={"type":"json_object"}`，
   若接口返回 400 提示不支持该参数，则自动去掉该参数重试一次（兼容旧版接口）。
最终即使模型完全乱答，用户拿到的也一定是一组**合法可用**的参数。

### 问题 6：`response_format` 参数在部分厂商接口不被支持

**现象**：部分 OpenAI 兼容实现未实现 `response_format`，直接返回 400。
**解决**：`LLMClient.chat()` 中对 400 响应做判别——若请求体里含 `response_format`，
则移除该键后继续下一轮尝试；同时对 401/403/404 直接失败不重试（重试无意义），
仅对超时、429、5xx 做指数退避重试。

### 问题 7：流复制模式下叠加滤镜导致转码失败

**现象**：选「无损转码」预设时，若仍拼接 `-vf scale=...`，
ffmpeg 报 `Filtergraph 'scale' was defined for video output stream but codec copy was selected`。
**解决**：`build_ffmpeg_command()` 中引入 `video_copy` 判定，
`vcodec == "copy"` 时跳过所有重编码参数（`-vf`、`-r`、`-b:v`），
并且 `acodec == "copy"` 时跳过 `-b:a`。

### 问题 8：分辨率缩放导致画面拉伸变形

**现象**：直接把 16:9 视频压成 `1080x1920` 竖屏会严重变形。
**解决**：固定宽高比的分辨率改用
`scale=W:H:force_original_aspect_ratio=decrease,pad=W:H:(ow-iw)/2:(oh-ih)/2`，
先等比缩放到能放进目标框，再居中补黑边，保持原始比例。

### 问题 9：空数据 / 脏数据导致界面或解析崩溃

**现象**：解析无音轨的视频时 `audio_stream` 为 `None`；
`OutputParams.from_dict()` 收到字符串 `"true"` 时布尔判断失效。
**解决**：
- `VideoInfo` 所有展示属性都做「无视频流 / 无音频流 / 未知」的中文兜底；
- `OutputParams.from_dict()` 显式做类型转换（`"true"/"1"/"是"` → `True`，
  其余字段 `str()` 化并跳过空值），`None` 输入直接返回默认对象。

### 问题 10：QLayout 初始化期间程序化赋值触发信号风暴

**现象**：初始化参数面板或 AI 回填参数时，连续触发 `paramsChanged`，
导致状态栏刷新多次、甚至触发递归（预设变化 → 套用预设 → 再次触发变化）。
**解决**：面板内引入 `_loading` 标志，程序化赋值期间为 `True`，
所有变更槽函数第一行 `if self._loading: return`；写完后置回 `False` 并手动发一次信号。
切换格式时同理，用 `try/finally` 保证标志一定被复位。

### 问题 11：配置文件损坏导致程序无法启动

**现象**：手工编辑 `settings.json` 时格式写错，程序启动直接抛 `JSONDecodeError`。
**解决**：`AppSettings.load()` 捕获 `JSONDecodeError / OSError` 并静默回退默认值；
数据目录创建失败也吞掉异常，保证「配置层永不阻塞启动」。

### 问题 12：QThread 对象被 Python GC 回收导致崩溃

**现象**：局部变量持有的 worker 线程在方法返回后被回收，出现
`QThread: Destroyed while thread is still running`。
**解决**：主窗口用实例属性持有正在运行的 worker（`self._probe_worker` 等）防止 GC；
`ConversionManager` 用 `dict[task_id, worker]` 持有，并在任务结束时
`worker.finished.connect(worker.deleteLater)` 让 Qt 在事件循环中安全释放。

### 问题 13：`QRectF` 误从 `QtGui` 导入导致界面无法启动

**现象**：真机启动时抛出
`ImportError: cannot import name 'QRectF' from 'PySide6.QtGui'`，
主窗口连构造函数都进不去。
**原因**：`QRectF` 属于 `QtCore` 的几何类，`QtGui` 只提供 `QPainter` / `QPixmap` 等绘制类，
是凭印象写 import 的典型错误；静态检查（`py_compile`）发现不了这类错误，只有真实运行才会暴露。
**解决**：把 `QRectF` 移到 `from PySide6.QtCore import ...` 一行，并在验证流程中加入
「offscreen 模式下真实构造主窗口」的冒烟测试，杜绝只做语法检查的假通过。

### 问题 14：剩余时间在任务即将完成时显示「计算中」

**现象**：真机跑完一轮转码后发现，进度到 99.9% 时「剩余时间」列显示的是「计算中」，
而此刻任务其实马上就要结束，语义完全相反。
**原因**：`_format_eta()` 里写了 `if seconds <= 0: return "计算中"`，
而 `progress=end` 时剩余秒数恰好为 `0`，于是被归入了「无法估算」分支。
**解决**：把判断改为「负数或超过 24 小时才算无法估算」，`0 ~ 1 秒` 单独返回「即将完成」；
同步更新自测用例 T15（新增「临完成」与「无效值」两个断言），让该回归点被自动化覆盖。

### 问题 15：`core/__init__.py` 无条件重导出 Qt 依赖，破坏「核心逻辑可独立测试」

**现象**：在没有安装 PySide6 的环境里执行 `from core.ffprobe_parser import parse_video`
会直接 `ModuleNotFoundError: No module named 'PySide6'`。
**原因**：包级 `__init__.py` 无条件 `from core.ffmpeg_engine import ...`，
而 `ffmpeg_engine` 需要 `PySide6.QtCore` 的 `QThread` / `Signal`，
导致纯逻辑模块（`ffprobe_parser` / `thumbnail`）也被 Qt 绑定「绑架」。
**解决**：用 `try/except ImportError` 包裹 `ffmpeg_engine` 的重导出，
导入失败时只发一条 `UserWarning` 并降级，保证核心解析逻辑在无 Qt 环境下依旧可用、可单测。

### 问题 16：硬件加速探测「假阳性」，把用户引向必然失败的转换

**现象**：在测试机上勾选硬件加速后，任务 100% 失败，ffmpeg 报
`[enc:h264_nvenc] Error while opening encoder` / `Function not implemented`；
但界面却显示「NVIDIA NVENC 可用」，用户会觉得是自己的视频有问题。
**原因**：最初只解析 `ffmpeg -encoders` 的输出做名称匹配。
这份 ffmpeg 把 `h264_nvenc` 编译进去了，于是列表里必然能匹配到，
**但「被编译进 ffmpeg」与「本机硬件真的能跑」是两件事**：
该机器虽然有 `nvidia-smi`（驱动装了），显卡却不支持 / 不支持当前 NVENC API 版本。
**解决**：探测改为两段式——列表匹配只用来筛候选，随后对候选厂商的 H.264 编码器
各做一次 0.4 秒的 `lavfi` 合成画面**试编码**（`-f null -`），只有返回码为 0 才判定可用，
按 `nvenc > qsv > amf` 顺序取第一个通过的方案。
改造后同一台机器自动识别为 **Intel QSV 可用、NVENC 不可用**，
硬件加速任务实测成功（`-c:v h264_qsv`），彻底消除了「假阳性」导致的必败转换。
"""
    )

    test_section = _render_test_table(results) if results else "_本次未执行自测用例（run_tests=False）_"
    sections.append(
        f"""---

## 6. 测试用例与测试结果

### 6.1 测试策略

本项目的测试分三层：

| 层级 | 范围 | 是否需要 ffmpeg / 网络 / 界面 |
| --- | --- | --- |
| 纯逻辑单元自测 | 命令构建、参数校验、JSON 提取、规则解析、错误翻译、格式化 | 全部不需要 |
| 集成自测 | `ffprobe` 真实解析、硬件加速探测、环境检测 | 需要 ffmpeg |
| 手工验收 | 拖拽导入、AI 解析回填、批量队列、取消、通知、目录记忆 | 需要完整环境 |

`utils/summary_gen.py` 内置了第一层与第二层的可执行用例，**每次生成文档时都会真实执行并记录结果**，
因此下表中的「说明」列是实际运行输出，而非人工填写。

### 6.2 测试结果

{test_section}

### 6.3 手工验收清单

| 编号 | 验收项 | 预期表现 |
| --- | --- | --- |
| M01 | 启动时环境检测 | 有 ffmpeg 时状态栏显示绿色「运行环境：正常」；无 ffmpeg 时弹出安装指引对话框且程序不崩溃 |
| M02 | 拖拽导入视频 | 拖入受支持扩展名的文件，缩略图与 13 项元数据自动填充 |
| M03 | 文件选择导入 | 对话框默认定位上次目录，可多选（取第一个加载） |
| M04 | 缩略图 | 显示视频中间帧，等比缩放不变形；「刷新缩略图」可强制重取 |
| M05 | 套用预设 | 参数栏立即回填，底部状态栏显示参数摘要 |
| M06 | 自然语言解析 | 输入「转成 1080P 的 MP4，500M 以内，适合发朋友圈」后参数被自动填入，并给出中文解释 |
| M07 | AI 未配置时降级 | 未填 Key 时点击解析，提示降级原因，参数由本地规则生成，功能仍可用 |
| M08 | 智能推荐 | 加载视频后点击，按源分辨率/帧率/码率给出推荐；有显卡时自动启用硬件编码 |
| M09 | 加入队列 | 任务出现在表格中，状态「等待中」，显示文件名/目标格式/进度条 |
| M10 | 开始转换 | 进度条实时增长，速度与剩余时间持续刷新 |
| M11 | 中途取消 | 点「取消」后任务变「已取消」，未完成的输出文件被清理 |
| M12 | 批量队列 | 连续加入多个任务，默认逐个串行执行 |
| M13 | 完成通知 | 转换成功弹出系统托盘通知，状态列变绿「已完成」 |
| M14 | 打开输出目录 | 点击后系统文件管理器定位到输出目录 |
| M15 | 输出目录记忆 | 重启程序后输出目录仍为上次选择 |
| M16 | 错误翻译 | 故意把分辨率改成奇数（如 1921x1081），失败提示为中文可读文本 |
| M17 | 日志 | 运行日志面板与 `~/.video_converter_ai/logs/app.log` 均能看到完整 ffmpeg 命令 |
| M18 | MP3 纯音频 | 目标格式选 MP3 时视频参数被禁用，输出为可播放的 MP3 |
| M19 | GIF 动图 | 输出 GIF 循环播放且体积合理（已启用调色板优化） |
| M20 | 配置持久化 | 设置的 API Key / 并发数 / 通知开关重启后保留 |
"""
    )

    sections.append(
        """---

## 7. 项目总结与后续改进方向

### 7.1 项目总结

**已完成的能力**

1. **完整可运行的桌面客户端**：PySide6 主窗口 + 四个功能面板 + 菜单栏 + 状态栏 + 停靠日志面板，
   无任何 Web 依赖。
2. **严格遵守 ffmpeg 原生命令行约束**：全部视频处理能力通过 `subprocess` 调用
   `ffmpeg` / `ffprobe` 可执行文件实现，**未引入任何 ffmpeg 封装库**。
3. **基础功能齐全**：拖拽/选择导入、13 项元数据展示、中间帧缩略图、6 种输出格式、
   6 项可调参数、8 种预设、实时进度/速度/剩余时间、中途取消、完成通知、一键打开目录、
   批量队列（默认并发 1）、自动环境检测与安装指引。
4. **AI 能力可用且有兜底**：自然语言转参数（严格 JSON 约束 + 三级降级）、
   智能参数推荐、显卡探测与硬件加速。
5. **工程细节到位**：全链路日志、配置持久化、错误人性化翻译、输出目录记忆、
   防覆盖命名、缩略图缓存、线程安全防御。

**架构上的关键决策**

- **严格单向分层**：`ui → ai → core → config/utils`。
  UI 只做编排与展示；AI 层纯逻辑不导入 Qt（可离线测试）；
  命令构建是纯函数（无 ffmpeg 也能单测）。
- **所有外部依赖都假定会失败**：ffmpeg 可能没装、网络可能断、模型可能乱答、
  配置可能损坏、显卡可能不支持、文件可能损坏——每一处都有明确的降级路径与中文提示。
  这是本项目最核心的设计哲学：**宁可功能降级，绝不允许崩溃。**

### 7.2 已知限制

| 限制 | 说明 | 改进方向 |
| --- | --- | --- |
| 不支持视频剪辑/裁切 | 目前只能整段转码 | 增加起止时间与画面裁剪参数 |
| 进度依赖源时长为已知值 | 时长未知（如部分流媒体文件）时不显示百分比，只显示速度 | 用 `-t` 或容器总时长二次确认 |
| 并发数上限 3 | 避免机械盘 IO 与 CPU 争抢 | 增加基于磁盘类型的自适应并发 |
| 硬件加速仅编码侧 | 解码未强制指定 | 增加 `-hwaccel cuda/qsv` 显式解码加速选项 |

### 7.3 后续改进方向

1. **功能增强**：视频裁剪/拼接、水印叠加、字幕烧录、两遍编码（VBR 更精准控体积）。
2. **AI 能力深化**：
   - 转码完成的「制作报告」（AI 对比源文件与输出文件的体积、码率、画质预估）；
   - 支持多轮对话式微调（「再小一点」「分辨率降一档」）；
   - 本地小模型（如 Ollama）接入，实现完全离线的 AI 解析。
3. **工程化**：
   - 引入 `pytest` 把 `summary_gen` 中的自测用例迁移为标准测试套件并接入 CI；
   - 用 `PyInstaller` 打包成免安装的 `.exe` / `.app`；
   - 增加国际化（中/英双语）。
4. **性能**：硬件解码加速、临时文件本地盘策略、超大文件分片转码。
"""
    )

    env_section = _render_env_section()
    module_table = _render_module_table(stats)
    preset_table = _render_presets_table()
    format_table = _render_format_table()

    sections.append(
        f"""---

## 附录 A：运行环境信息（自动采集）

{env_section}

## 附录 B：代码规模与模块清单（自动统计）

{module_table}

## 附录 C：内置预设一览（自动读取）

{preset_table}

## 附录 D：输出格式一览（自动读取）

{format_table}

---

## 附录 E：如何重新生成本文档

```bash
python utils/summary_gen.py                 # 生成到项目根目录 SESSION_SUMMARY.md
python utils/summary_gen.py -o 报告.md       # 指定输出路径
python utils/summary_gen.py --no-tests      # 跳过自测用例（仅采集结构与环境信息）
```
"""
    )

    content = "\n".join(sections)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")
    print(f"已生成：{output_path}（{len(content.splitlines())} 行）")
    return output_path


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="生成 SESSION_SUMMARY.md 会话总结文档")
    parser.add_argument("-o", "--output", default=str(DEFAULT_OUTPUT), help="输出文件路径")
    parser.add_argument("--no-tests", action="store_true", help="跳过自测用例执行")
    args = parser.parse_args(argv)

    try:
        generate_summary(Path(args.output), run_tests=not args.no_tests)
    except Exception:  # noqa: BLE001  生成器本身也不应把异常抛给用户
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
