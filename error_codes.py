"""
統一エラーコードとメッセージ定義
Pythonバックエンド・JavaScriptフロントエンド両方で一貫したエラーハンドリング
"""

from enum import IntEnum


class ErrorCode(IntEnum):
    """アプリケーション内で使用するエラーコード"""

    # 検証エラー (1000-1099)
    INVALID_SYMBOL = 1001
    INVALID_MARKET = 1002
    INVALID_API_KEY = 1003
    INVALID_PERIOD = 1004
    INVALID_INPUT = 1005
    MISSING_REQUIRED_FIELD = 1006
    INVALID_SYMBOL_FORMAT = 1007
    MALFORMED_INPUT = 1008
    UNSAFE_INPUT = 1009

    # データ取得エラー (1100-1199)
    FETCH_FAILED = 1101
    SYMBOL_NOT_FOUND = 1102
    NO_DATA_AVAILABLE = 1103
    DATA_PARSE_ERROR = 1104
    TIMEOUT_ERROR = 1105

    # API エラー (1200-1299)
    API_AUTH_FAILED = 1201
    API_QUOTA_EXCEEDED = 1202
    API_SERVICE_ERROR = 1203
    API_RATE_LIMITED = 1204

    # システムエラー (1300-1399)
    CONFIG_ERROR = 1301
    FILE_ERROR = 1302
    CACHE_ERROR = 1303
    INTERNAL_SERVER_ERROR = 1304
    CIRCUIT_BREAKER_OPEN = 1305

    # 未分類エラー (9999)
    UNKNOWN = 9999


# エラーコード→メッセージのマッピング
ERROR_MESSAGES_JA = {
    ErrorCode.INVALID_SYMBOL: "無効なシンボルです",
    ErrorCode.INVALID_MARKET: "無効な市場コードです (us/jp/idx を指定してください)",
    ErrorCode.INVALID_API_KEY: "APIキーが不正です。設定を確認してください",
    ErrorCode.INVALID_PERIOD: "無効な期間です (1d/5d/1mo/3mo/6mo/1y/2y/5y/max を指定してください)",
    ErrorCode.INVALID_INPUT: "入力が不正です",
    ErrorCode.MISSING_REQUIRED_FIELD: "必須フィールドが不足しています",
    ErrorCode.INVALID_SYMBOL_FORMAT: "シンボルの形式が無効です",
    ErrorCode.MALFORMED_INPUT: "入力データの形式が不正です",
    ErrorCode.UNSAFE_INPUT: "安全でない入力が検出されました",
    ErrorCode.FETCH_FAILED: "データ取得に失敗しました。しばらく後に再度お試しください",
    ErrorCode.SYMBOL_NOT_FOUND: "シンボルが見つかりません",
    ErrorCode.NO_DATA_AVAILABLE: "このシンボルのデータが利用できません",
    ErrorCode.DATA_PARSE_ERROR: "データの解析に失敗しました",
    ErrorCode.TIMEOUT_ERROR: "リクエストがタイムアウトしました",
    ErrorCode.API_AUTH_FAILED: "APIの認証に失敗しました",
    ErrorCode.API_QUOTA_EXCEEDED: "API クォーターを超過しました",
    ErrorCode.API_SERVICE_ERROR: "API サービスエラーが発生しました",
    ErrorCode.API_RATE_LIMITED: "リクエストのレート制限が適用されています",
    ErrorCode.CONFIG_ERROR: "設定ファイルの読み込みに失敗しました",
    ErrorCode.FILE_ERROR: "ファイル操作に失敗しました",
    ErrorCode.CACHE_ERROR: "キャッシュエラーが発生しました",
    ErrorCode.INTERNAL_SERVER_ERROR: "サーバーの内部エラーが発生しました",
    ErrorCode.CIRCUIT_BREAKER_OPEN: "一時的にリクエストが制限されています。しばらくお待ちください。",
    ErrorCode.UNKNOWN: "不明なエラーが発生しました",
}

ERROR_MESSAGES_EN = {
    ErrorCode.INVALID_SYMBOL: "Invalid symbol",
    ErrorCode.INVALID_MARKET: "Invalid market code (use us/jp/idx)",
    ErrorCode.INVALID_API_KEY: "Invalid API key. Please check your settings",
    ErrorCode.INVALID_PERIOD: "Invalid period (use 1d/5d/1mo/3mo/6mo/1y/2y/5y/max)",
    ErrorCode.INVALID_INPUT: "Invalid input",
    ErrorCode.MISSING_REQUIRED_FIELD: "Missing required field",
    ErrorCode.INVALID_SYMBOL_FORMAT: "Invalid symbol format",
    ErrorCode.MALFORMED_INPUT: "Malformed input data",
    ErrorCode.UNSAFE_INPUT: "Unsafe input detected",
    ErrorCode.FETCH_FAILED: "Failed to fetch data. Please try again later",
    ErrorCode.SYMBOL_NOT_FOUND: "Symbol not found",
    ErrorCode.NO_DATA_AVAILABLE: "No data available for this symbol",
    ErrorCode.DATA_PARSE_ERROR: "Failed to parse data",
    ErrorCode.TIMEOUT_ERROR: "Request timeout",
    ErrorCode.API_AUTH_FAILED: "API authentication failed",
    ErrorCode.API_QUOTA_EXCEEDED: "API quota exceeded",
    ErrorCode.API_SERVICE_ERROR: "API service error",
    ErrorCode.API_RATE_LIMITED: "Rate limited",
    ErrorCode.CONFIG_ERROR: "Failed to load configuration",
    ErrorCode.FILE_ERROR: "File operation failed",
    ErrorCode.CACHE_ERROR: "Cache error",
    ErrorCode.INTERNAL_SERVER_ERROR: "Internal server error",
    ErrorCode.CIRCUIT_BREAKER_OPEN: "Request temporarily limited. Please try again later.",
    ErrorCode.UNKNOWN: "Unknown error occurred",
}


def get_error_message(error_code: int, lang: str = "ja") -> str:
    """エラーコードからメッセージを取得"""
    messages = ERROR_MESSAGES_JA if lang == "ja" else ERROR_MESSAGES_EN
    return messages.get(error_code, messages[ErrorCode.UNKNOWN])
