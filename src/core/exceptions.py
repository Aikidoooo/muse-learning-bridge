"""システム全体で使用する例外クラスとエラーコード定義。"""

from __future__ import annotations


class MuseBridgeError(Exception):
    """Muse Learning Bridge の基底例外クラス。"""

    code: str = "INTERNAL_ERROR"
    retryable: bool = False
    http_status: int = 500

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        retryable: bool | None = None,
        http_status: int | None = None,
        details: dict | None = None,
    ) -> None:
        """例外を初期化する。"""
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if retryable is not None:
            self.retryable = retryable
        if http_status is not None:
            self.http_status = http_status
        self.details = details or {}


class LoginFailedError(MuseBridgeError):
    """Muse側でのログイン失敗エラー（再試行不可）。"""

    code = "LOGIN_FAILED"
    retryable = False
    http_status = 401


class MaterialNotFoundError(MuseBridgeError):
    """対象教材が見つからない場合のエラー（再試行不可）。"""

    code = "MATERIAL_NOT_FOUND"
    retryable = False
    http_status = 404


class PermissionDeniedError(MuseBridgeError):
    """学校によるAI利用許可・自動提出許可が確認できない場合のエラー（再試行不可）。"""

    code = "PERMISSION_DENIED"
    retryable = False
    http_status = 403


class CredentialLeakageError(MuseBridgeError):
    """Classi認証情報やCookie・セッショントークンの混入を検出した場合のエラー（再試行不可）。"""

    code = "CREDENTIAL_DATA_REJECTED"
    retryable = False
    http_status = 400


class DuplicateJobError(MuseBridgeError):
    """同一教材に対する二重実行エラー（再試行不可）。"""

    code = "DUPLICATE_JOB"
    retryable = False
    http_status = 409


class BudgetExceededError(MuseBridgeError):
    """API利用料金の上限を超過した場合のエラー（再試行不可）。"""

    code = "BUDGET_EXCEEDED"
    retryable = False
    http_status = 429


class ProviderNotFoundError(MuseBridgeError):
    """未対応または無効化されたAIプロバイダー指定エラー（再試行不可）。"""

    code = "PROVIDER_NOT_FOUND"
    retryable = False
    http_status = 400


class ProviderAuthError(MuseBridgeError):
    """外部AI APIキー無効・認証失敗エラー（再試行不可）。"""

    code = "PROVIDER_AUTH_ERROR"
    retryable = False
    http_status = 401


class ProviderRateLimitError(MuseBridgeError):
    """外部AI APIの使用量制限エラー（一時的な制限として最大3回再試行可能）。"""

    code = "PROVIDER_RATE_LIMIT"
    retryable = True
    http_status = 429


class NetworkConnectionError(MuseBridgeError):
    """インターネット・外部AI API接続エラー（再試行可能）。"""

    code = "NETWORK_CONNECTION_ERROR"
    retryable = True
    http_status = 503


class ProviderTimeoutError(MuseBridgeError):
    """外部AI APIタイムアウトエラー（再試行可能）。"""

    code = "PROVIDER_TIMEOUT"
    retryable = True
    http_status = 504


class InvalidJsonFormatError(MuseBridgeError):
    """JSON形式異常エラー（再試行可能だが上限超過時は確認対象または失敗）。"""

    code = "INVALID_JSON_FORMAT"
    retryable = True
    http_status = 502


class InvalidAIResponseError(MuseBridgeError):
    """AIレスポンスの構造または内容異常エラー。"""

    code = "INVALID_AI_RESPONSE"
    retryable = True
    http_status = 502


class MuseCommunicationError(MuseBridgeError):
    """Museクライアントとの通信切断・プロトコル異常エラー。"""

    code = "MUSE_COMMUNICATION_ERROR"
    retryable = True
    http_status = 502


class PageStructureChangedError(MuseBridgeError):
    """WebページのDOM構造変更により問題データを抽出できない場合のエラー（再試行不可）。"""

    code = "PAGE_STRUCTURE_CHANGED"
    retryable = False
    http_status = 422


class JobNotFoundError(MuseBridgeError):
    """指定されたジョブIDが存在しない場合のエラー。"""

    code = "JOB_NOT_FOUND"
    retryable = False
    http_status = 404


class JobCancelledError(MuseBridgeError):
    """ジョブがキャンセルされたことを示す例外。"""

    code = "JOB_CANCELLED"
    retryable = False
    http_status = 409
