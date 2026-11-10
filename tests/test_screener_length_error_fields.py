"""Screener の文字数超過エラーがフィールド単位で正しく報告されることの回帰テスト。

`_screener_query_error_from_validation()` は以前 `"string_too_long" in type` を
フィールド非依存に拾っていたため、`sector`(max_length=100)の超過でも
「検索ワードは200文字以内で入力してください」を返し、`details.fields` も
欠落していた。上限は pydantic の ctx から引き、フィールド別の語彙を返す。
"""

import pytest
from flask import Flask, request
from pydantic import BaseModel, Field, ValidationError

from route_helpers import _parse_screener_request, _screener_query_error_from_validation

SCREENER_URL = "/api/stocks/screener"


@pytest.fixture
def test_app():
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["SECRET_KEY"] = "test-secret-key"
    return app


def _screener_error(test_app, query_string):
    with test_app.test_request_context(f"{SCREENER_URL}?{query_string}"):
        req, err = _parse_screener_request(request.args)
    assert req is None
    assert err is not None
    return err


def test_sector_too_long_reports_sector_field(test_app):
    """sector 超過は検索ワードのメッセージを流用しない。"""
    resp, code = _screener_error(test_app, "sector=" + "S" * 101)
    assert code == 400
    data = resp.get_json()
    assert data["ok"] is False
    details = data["details"]
    assert details["fields"] == ["sector"]
    assert "sector" in details["reason"]
    assert "100文字以内" in details["reason"]
    assert "検索ワード" not in details["reason"]


def test_q_too_long_reports_query_field(test_app):
    """q 超過は従来どおり検索ワードのメッセージを保ちつつ fields を付ける。"""
    resp, code = _screener_error(test_app, "q=" + "A" * 201)
    assert code == 400
    details = resp.get_json()["details"]
    assert details["fields"] == ["q"]
    assert "200文字以内" in details["reason"]


@pytest.mark.parametrize(
    ("query_string",),
    [
        ("sector=" + "S" * 100,),
        ("q=" + "A" * 200,),
    ],
)
def test_length_limits_accept_boundary_values(test_app, query_string):
    """上限ちょうどは拒否しない(境界の退行防止)。"""
    with test_app.test_request_context(f"{SCREENER_URL}?{query_string}"):
        req, err = _parse_screener_request(request.args)
    assert err is None
    assert req is not None


def test_unknown_long_field_uses_pydantic_max_length(test_app):
    """スキーマに長さ制約付きフィールドが増えても ctx の上限で報告できる。"""

    class _Probe(BaseModel):
        custom_field: str = Field(default="", max_length=50)

    with pytest.raises(ValidationError) as excinfo:
        _Probe(custom_field="x" * 60)

    with test_app.app_context():
        resp, code = _screener_query_error_from_validation(excinfo.value)

    assert code == 400
    details = resp.get_json()["details"]
    assert details["fields"] == ["custom_field"]
    assert "50文字以内" in details["reason"]
