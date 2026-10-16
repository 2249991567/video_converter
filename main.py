"""
AI 视频格式转换器 —— 程序入口
==============================

启动流程：
1. 加载项目根目录的 `.env`（大模型密钥、ffmpeg 路径等环境变量）；
2. 初始化日志系统（控制台 + 滚动日志文件）；
3. 创建 QApplication 并设置中文字体；
4. 环境守卫：检测 ffmpeg / ffprobe，缺失时弹出友好的安装指引对话框，
   **绝不允许因为缺少依赖而直接崩溃退出**；
5. 创建系统托盘图标（用于后台转换完成后的系统通知）；
6. 创建并展示主窗口。

运行方式：
    python main.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# 确保无论从哪个目录执行，都能正确导入项目内的包
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _load_env() -> None:
    """加载 .env 环境变量（python-dotenv 未安装时静默跳过，不影响启动）。"""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    env_file = PROJECT_ROOT / ".env"
    if env_file.exists():
        load_dotenv(env_file, override=False)


def main() -> int:
    # ---- 1. 环境变量 ----
    _load_env()

    # ---- 2. 日志系统 ----
    from utils.logger import get_logger, setup_logging

    setup_logging()
    logger = get_logger("main")
    logger.info("=" * 68)
    logger.info("启动 AI 视频格式转换器（Python %s）", sys.version.split()[0])

    # ---- 3. Qt 应用对象 ----
    try:
        from PySide6.QtGui import QFont
        from PySide6.QtWidgets import QApplication
    except ImportError:
        print(
            "【启动失败】未安装 PySide6。请先执行：\n"
            "    pip install -r requirements.txt",
            file=sys.stderr,
        )
        logger.critical("未安装 PySide6，无法启动图形界面")
        return 1

    app = QApplication(sys.argv)
    app.setApplicationName("AI 视频格式转换器")
    app.setOrganizationName("VideoConverterAI")
    # 关闭最后一个窗口即退出程序
    app.setQuitOnLastWindowClosed(True)

    # 中文字体：优先使用微软雅黑，保证界面不出现方块字
    for family in ("Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC"):
        font = QFont(family, 10)
        if QFont(family).exactMatch() or family.startswith(("Microsoft", "PingFang")):
            app.setFont(font)
            break

    # ---- 4. 环境检测守卫 ----
    from config.settings import get_system_info
    from ui.components.env_dialog import ensure_environment

    system_info = get_system_info()
    logger.info("系统信息：%s", system_info)

    if not ensure_environment(None):
        logger.warning("用户选择在缺少 ffmpeg 的情况下退出程序")
        print("未检测到 ffmpeg，程序已退出。安装后可重新运行：python main.py", file=sys.stderr)
        return 2

    # ---- 5. 系统托盘图标 ----
    tray = None
    try:
        from PySide6.QtWidgets import QSystemTrayIcon

        if QSystemTrayIcon.isSystemTrayAvailable():
            from ui.main_window import build_app_icon

            tray = QSystemTrayIcon(build_app_icon(), app)
            tray.setToolTip("AI 视频格式转换器")
            tray.show()
            logger.info("系统托盘图标已创建")
        else:
            logger.info("当前系统不支持系统托盘通知，转换完成通知将降级为状态栏提示")
    except Exception as exc:  # noqa: BLE001  托盘失败不应阻塞主界面
        logger.warning("创建系统托盘图标失败：%s", exc)
        tray = None

    # ---- 6. 主窗口 ----
    from ui.main_window import MainWindow

    window = MainWindow()
    window.set_tray_icon(tray)
    window.show()
    logger.info("主窗口已显示")

    exit_code = app.exec()
    logger.info("程序退出，退出码：%s", exit_code)
    return int(exit_code)


if __name__ == "__main__":
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    sys.exit(main())
