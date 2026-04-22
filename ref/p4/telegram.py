"""Telegram Bot 推送"""

import requests

from push.base import PushBase
from utils.logger import logger


class TelegramPusher(PushBase):
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    def send(self, message: str) -> bool:
        if not self.bot_token or not self.chat_id:
            logger.warning("Telegram bot_token 或 chat_id 未配置")
            return False
        try:
            resp = requests.post(
                self.api_url,
                json={"chat_id": self.chat_id, "text": message, "parse_mode": "HTML"},
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json().get("ok", False)
        except Exception as e:
            logger.error(f"Telegram 推送异常: {e}")
            return False
