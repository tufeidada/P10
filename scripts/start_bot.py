"""
P10-AlphaRadar Telegram Bot 启动脚本。

用法：
    python scripts/start_bot.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import structlog

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
)


def main() -> None:
    from bot.telegram_bot import main as bot_main
    bot_main()


if __name__ == "__main__":
    main()
