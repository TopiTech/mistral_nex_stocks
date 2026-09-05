"""Regression coverage for optional JSON request bodies on costly endpoints."""

from __future__ import annotations

import io
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask, request
from werkzeug.test import EnvironBuilder

from app import create_app
from utils.text_utils import _parse_json_request, _parse_optional_json_request


@pytest.fixture
def client():
    app = create_app(
        config_override={"TESTING": True, "WTF_CSRF_ENABLED": False}, skip_bootstrap=True
    )
    with app.test_client() as test_client:
        yield test_client


def test_news_rejects_malformed_json_without_starting_external_fetch(client) -> None:
    """A malformed body must not be treated as an empty news-refresh request."""
    with (
        patch("routes.api_analysis.extract_api_key", return_value="test-key"),
        patch("routes.api_analysis.news_service.get_synchronized_market_news") as fetch_news,
    ):
        response = client.post(
            "/api/news",
            data='{"force":',
            content_type="application/json",
        )

    assert response.status_code == 400
    assert response.get_json()["error_code"] > 0
    fetch_news.assert_not_called()


def test_optional_json_parser_accepts_empty_json_content_type_body() -> None:
    """The dashboard's JSON Content-Type POST with no body remains supported."""
    app = Flask(__name__)
    with app.test_request_context("/api/news", method="POST", content_type="application/json"):
        assert _parse_optional_json_request() == {}


def _chunked_json_environ(body: bytes) -> dict:
    builder = EnvironBuilder(
        path="/api/test",
        method="POST",
        data=body,
        content_type="application/json",
    )
    environ = builder.get_environ()
    environ.pop("CONTENT_LENGTH", None)
    environ["wsgi.input"] = io.BytesIO(body)
    environ["wsgi.input_terminated"] = True
    return environ


@pytest.mark.parametrize("parser", [_parse_json_request, _parse_optional_json_request])
def test_json_parsers_reject_oversized_chunked_bodies(parser) -> None:
    """The JSON limit must apply when a client omits Content-Length."""
    max_size = 64
    body = b'{"value":"' + (b"a" * max_size) + b'"}'
    app = Flask(__name__)
    environ = _chunked_json_environ(body)

    with app.request_context(environ):
        assert request.content_length is None
        assert parser(max_size=max_size) is None


def test_json_parser_accepts_chunked_body_exactly_at_limit() -> None:
    """The sentinel read must not reject a valid body at the configured limit."""
    max_size = 64
    prefix = b'{"value":"'
    suffix = b'"}'
    body = prefix + (b"a" * (max_size - len(prefix) - len(suffix))) + suffix
    app = Flask(__name__)

    assert len(body) == max_size
    with app.request_context(_chunked_json_environ(body)):
        assert _parse_json_request(max_size=max_size) == {"value": "a" * 52}


def test_credentials_verify_rejects_malformed_json_without_using_saved_key(client) -> None:
    """Invalid JSON must not cause verification with a server-stored API key."""
    mock_client = MagicMock()
    with (
        patch("credential_manager.get_mistral_api_key", return_value="stored-secret"),
        patch(
            "routes.api_system.app_state.ai.get_or_create_mistral_client",
            return_value=mock_client,
        ) as get_client,
    ):
        response = client.post(
            "/api/credentials/verify",
            data='{"mistral_api_key":',
            content_type="application/json",
            headers={"Origin": "http://localhost:5000"},
        )

    assert response.status_code == 400
    assert response.get_json()["error_code"] > 0
    get_client.assert_not_called()


def test_credentials_verify_still_allows_a_genuinely_empty_body(client) -> None:
    """The saved-key flow remains available when no body is sent."""
    mock_client = MagicMock()
    mock_client.models.list.return_value = MagicMock(data=[])
    with (
        patch("credential_manager.get_mistral_api_key", return_value="stored-secret"),
        patch(
            "routes.api_system.app_state.ai.get_or_create_mistral_client",
            return_value=mock_client,
        ) as get_client,
    ):
        response = client.post(
            "/api/credentials/verify",
            headers={"Origin": "http://localhost:5000"},
        )

    assert response.status_code == 200
    get_client.assert_called_once_with("stored-secret")
