"""Regression test for the chat 500 error envelope.

An unexpected exception used to be echoed to the browser (truncated to 120
characters) even though the SSE chat path and the unified error envelope
return a fixed message. Unhandled exception text can carry absolute paths,
SDK internals and internal identifiers, so the API must log it server-side
and return only the fixed user-facing message.
"""

from flask import Flask, g

from routes.api_analysis import _chat_error_response


def test_unexpected_exception_text_is_not_returned_to_client() -> None:
    app = Flask(__name__)
    with app.test_request_context("/api/chat"):
        g.request_id = "req-chat-redaction"
        exc = RuntimeError("disk path C:\\internal\\state.json unusable")
        resp, status = _chat_error_response(exc, g)
        assert status == 500
        data = resp.get_json()
        assert data is not None

        for key in ("error", "message", "reply"):
            assert data[key] == "チャット処理に失敗しました"
            assert "state.json" not in data[key]
            assert "internal" not in data[key]
        assert data["details"]["reason"] == "チャット処理に失敗しました"
        assert data["details"]["error_type"] == "RuntimeError"
        assert data["error_code"] == "ERR_CHAT_500"
