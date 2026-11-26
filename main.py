"""Muse Learning Bridge エントリポイント（CLIおよびFastAPIサーバー起動）。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import uvicorn

from src.api.app import create_app
from src.core.config import AppConfig, load_config
from src.core.config_manager import ConfigManager
from src.core.exceptions import MuseBridgeError
from src.core.logging_config import get_logger, setup_logging
from src.jobs.manager import JobManager
from src.schemas.enums import ProviderName
from src.schemas.question import MaterialInput


def build_parser() -> argparse.ArgumentParser:
    """CLI引数パーサーを構築する。"""
    parser = argparse.ArgumentParser(
        prog="muse-learning-bridge",
        description="Meta Museと外部AI APIを連携させる学習支援自動化システム",
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="設定YAMLファイルパス（既定: config.yaml）",
    )
    parser.add_argument(
        "--env",
        default=".env",
        help="環境変数ファイルパス（既定: .env）",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # 稼働・設定確認
    subparsers.add_parser("health", help="設定・環境・稼働状態を確認する")
    subparsers.add_parser("providers", help="対応AIプロバイダーと設定一覧を表示する")

    # APIサーバー起動
    serve_parser = subparsers.add_parser("serve", help="FastAPIサーバーを起動する")
    serve_parser.add_argument("--host", default=None, help="バインドホスト")
    serve_parser.add_argument("--port", type=int, default=None, help="ポート番号")

    # CLIジョブ実行
    run_parser = subparsers.add_parser("run", help="構造化JSONから解析ジョブを実行する")
    run_parser.add_argument(
        "--input",
        required=True,
        help="問題データのJSONファイルパス（'-' で標準入力）",
    )
    run_parser.add_argument("--provider", default=None, help="使用AIプロバイダー")
    run_parser.add_argument("--model", default=None, help="使用AIモデル名")
    run_parser.add_argument(
        "--output",
        default=None,
        help="レポートJSONの出力先ファイルパス（未指定時は標準出力）",
    )

    # ステータス・結果・中止
    status_parser = subparsers.add_parser("status", help="ジョブ実行状況を取得する")
    status_parser.add_argument("job_id", help="対象ジョブID")

    result_parser = subparsers.add_parser("result", help="ジョブ解析結果レポートを取得する")
    result_parser.add_argument("job_id", help="対象ジョブID")

    cancel_parser = subparsers.add_parser("cancel", help="ジョブを中止する")
    cancel_parser.add_argument("job_id", help="対象ジョブID")

    return parser


def _print_json(data: Any) -> None:
    """UTF-8で整形済みJSONを標準出力へ書き出す。"""
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def _handle_health(config: AppConfig) -> int:
    """ヘルスチェック結果を出力する。"""
    _print_json(
        {
            "status": "ok",
            "app_name": config.app.name,
            "version": config.app.version,
            "environment": config.app.environment,
            "default_provider": config.providers.default_provider,
            "db_path": config.app.db_path,
        }
    )
    return 0


def _handle_providers(config: AppConfig) -> int:
    """利用可能なAIプロバイダー一覧を出力する（APIキー自体は決して出力しない）。"""
    items = []
    for name, entry in config.providers.all_providers().items():
        items.append(
            {
                "name": name,
                "display_name": entry.display_name,
                "enabled": entry.enabled,
                "default_model": entry.default_model,
                "models": entry.models,
                "api_key_configured": bool(entry.resolve_api_key()),
            }
        )
    _print_json({"default_provider": config.providers.default_provider, "providers": items})
    return 0


def _read_input_json(input_arg: str) -> dict[str, Any]:
    """ファイルまたは標準入力から問題データJSONを読み込む。"""
    if input_arg == "-":
        raw = sys.stdin.read()
    else:
        raw = Path(input_arg).read_text(encoding="utf-8")
    return json.loads(raw)


def _handle_run(config: AppConfig, args: argparse.Namespace) -> int:
    """CLI経由で解析ジョブを作成・実行し、結果レポートを出力する。"""
    raw_payload = _read_input_json(args.input)
    if args.provider:
        raw_payload["provider"] = args.provider
    if args.model:
        raw_payload["model"] = args.model

    material = MaterialInput.model_validate(raw_payload)
    manager = JobManager(config)
    created = manager.create_job(material)
    asyncio.run(manager.execute_job(created.job_id))
    report = manager.get_result(created.job_id)

    report_dict = report.model_dump(mode="json")
    if args.output:
        Path(args.output).write_text(
            json.dumps(report_dict, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    _print_json(report_dict)
    return 0 if not report.has_error else 1


def main(argv: list[str] | None = None) -> int:
    """CLIメイン関数。"""
    parser = build_parser()
    args = parser.parse_args(argv)

    config = load_config(config_path=args.config, env_path=args.env)
    setup_logging(logs_dir=config.app.logs_dir, level=config.app.log_level)
    logger = get_logger("cli")

    try:
        if args.command == "health":
            return _handle_health(config)
        if args.command == "providers":
            return _handle_providers(config)
        if args.command == "serve":
            host = args.host or config.app.host
            port = args.port or config.app.port
            cfg_manager = ConfigManager(config, config_path=args.config)
            app = create_app(config_manager=cfg_manager)
            logger.info("FastAPIサーバーを起動します: http://%s:%d", host, port)
            uvicorn.run(app, host=host, port=port, log_level=config.app.log_level.lower())
            return 0
        if args.command == "run":
            return _handle_run(config, args)
        if args.command == "status":
            manager = JobManager(config)
            _print_json(manager.get_status(args.job_id).model_dump(mode="json"))
            return 0
        if args.command == "result":
            manager = JobManager(config)
            _print_json(manager.get_result(args.job_id).model_dump(mode="json"))
            return 0
        if args.command == "cancel":
            manager = JobManager(config)
            _print_json(manager.cancel_job(args.job_id).model_dump(mode="json"))
            return 0
    except MuseBridgeError as exc:
        logger.error("CLI実行エラー (%s): %s", exc.code, exc.message)
        _print_json(
            {
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "retryable": exc.retryable,
                    "details": exc.details,
                }
            }
        )
        return 1
    except Exception as exc:
        logger.exception("予期しないCLIエラー")
        _print_json({"error": {"code": "CLI_ERROR", "message": str(exc)}})
        return 1

    return 1


if __name__ == "__main__":
    sys.exit(main())
