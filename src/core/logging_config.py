"""シークレット・個人情報マスキング対応のロギング設定。"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from src.core.security import sanitize_for_log


class SecretMaskingFilter(logging.Filter):
    """すべてのログ出力からAPIキーや個人情報を自動除去するフィルタ。"""

    def filter(self, record: logging.LogRecord) -> bool:
        """ログメッセージおよび引数をマスキングして出力許可する。"""
        if isinstance(record.msg, str):
            record.msg = sanitize_for_log(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: sanitize_for_log(str(v)) if isinstance(v, str) else v
                    for k, v in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    sanitize_for_log(str(arg)) if isinstance(arg, str) else arg
                    for arg in record.args
                )
        return True


def setup_logging(logs_dir: str | Path = "logs", level: str = "INFO") -> logging.Logger:
    """アプリケーションロガーを構成して返す。"""
    log_path = Path(logs_dir)
    log_path.mkdir(parents=True, exist_ok=True)
    log_file = log_path / "muse_bridge.log"

    root_logger = logging.getLogger("muse_bridge")
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    root_logger.setLevel(numeric_level)

    # 再初期化時にハンドラが重複しないよう既存ハンドラを整理
    if root_logger.handlers:
        root_logger.handlers.clear()

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    mask_filter = SecretMaskingFilter()

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(numeric_level)
    file_handler.setFormatter(formatter)
    file_handler.addFilter(mask_filter)

    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(numeric_level)
    stream_handler.setFormatter(formatter)
    stream_handler.addFilter(mask_filter)

    root_logger.addHandler(file_handler)
    root_logger.addHandler(stream_handler)
    root_logger.propagate = False
    return root_logger


def get_logger(name: str) -> logging.Logger:
    """モジュール別のサブロガーを取得する。"""
    return logging.getLogger(f"muse_bridge.{name}")
