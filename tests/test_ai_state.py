"""tests/test_ai_state.py - Unit tests for AIState usage statistics and residual token aggregation."""

from ai_state import AIState


def test_ai_state_initial_stats():
    ai = AIState()
    mistral_stats = ai.mistral_usage_stats()
    assert mistral_stats["call_count"] == 0
    assert mistral_stats["prompt_tokens"] == 0
    assert mistral_stats["completion_tokens"] == 0
    assert mistral_stats["total_tokens"] == 0
    assert mistral_stats["estimated_cost_usd"] == 0.0
    assert mistral_stats["by_model"] == {}

    openai_stats = ai.openai_usage_stats()
    assert openai_stats["call_count"] == 0
    assert openai_stats["prompt_tokens"] == 0
    assert openai_stats["completion_tokens"] == 0
    assert openai_stats["total_tokens"] == 0
    assert openai_stats["by_model"] == {}


def test_openai_usage_named_model_only():
    ai = AIState()
    ai.record_ai_usage(
        {"prompt_tokens": 150, "completion_tokens": 50},
        model="gpt-4o-mini",
        provider="openai_compatible",
    )
    stats = ai.openai_usage_stats()
    assert stats["call_count"] == 1
    assert stats["prompt_tokens"] == 150
    assert stats["completion_tokens"] == 50
    assert stats["total_tokens"] == 200
    assert "gpt-4o-mini" in stats["by_model"]
    assert "unknown" not in stats["by_model"]

    model_entry = stats["by_model"]["gpt-4o-mini"]
    assert model_entry["calls"] == 1
    assert model_entry["prompt_tokens"] == 150
    assert model_entry["completion_tokens"] == 50
    assert model_entry["total_tokens"] == 200


def test_openai_usage_residual_tokens_without_model():
    ai = AIState()
    # Call with empty model name
    ai.record_openai_usage({"prompt_tokens": 300, "completion_tokens": 120}, model="")
    # Call with unknown model name
    ai.record_openai_usage({"prompt_tokens": 100, "completion_tokens": 40}, model="unknown")
    # Call with whitespace-only model name
    ai.record_openai_usage({"prompt_tokens": 50, "completion_tokens": 10}, model="   ")

    stats = ai.openai_usage_stats()
    assert stats["call_count"] == 3
    assert stats["prompt_tokens"] == 450
    assert stats["completion_tokens"] == 170
    assert stats["total_tokens"] == 620

    assert "unknown" in stats["by_model"]
    unknown_entry = stats["by_model"]["unknown"]
    assert unknown_entry["calls"] == 3
    assert unknown_entry["prompt_tokens"] == 450
    assert unknown_entry["completion_tokens"] == 170
    assert unknown_entry["total_tokens"] == 620


def test_openai_usage_mixed_models_reconciliation():
    ai = AIState()
    # 2 calls with named model
    ai.record_openai_usage(
        {"prompt_tokens": 200, "completion_tokens": 80},
        model="gpt-4o-mini",
    )
    ai.record_openai_usage(
        {"prompt_tokens": 300, "completion_tokens": 120},
        model="gpt-4o-mini",
    )
    # 1 call with a second named model
    ai.record_openai_usage(
        {"prompt_tokens": 500, "completion_tokens": 200},
        model="deepseek-chat",
    )
    # 2 calls without model (residual)
    ai.record_openai_usage(
        {"prompt_tokens": 150, "completion_tokens": 50},
        model="",
    )
    ai.record_openai_usage(
        {"prompt_tokens": 250, "completion_tokens": 70},
        model="unknown",
    )

    stats = ai.openai_usage_stats()
    assert stats["call_count"] == 5
    assert stats["prompt_tokens"] == 1400
    assert stats["completion_tokens"] == 520
    assert stats["total_tokens"] == 1920

    by_model = stats["by_model"]
    assert set(by_model.keys()) == {"gpt-4o-mini", "deepseek-chat", "unknown"}

    # Validate exact match of totals against sum of all by_model entries
    sum_calls = sum(m["calls"] for m in by_model.values())
    sum_prompt = sum(m["prompt_tokens"] for m in by_model.values())
    sum_completion = sum(m["completion_tokens"] for m in by_model.values())
    sum_total = sum(m["total_tokens"] for m in by_model.values())

    assert sum_calls == stats["call_count"]
    assert sum_prompt == stats["prompt_tokens"]
    assert sum_completion == stats["completion_tokens"]
    assert sum_total == stats["total_tokens"]

    assert by_model["unknown"]["calls"] == 2
    assert by_model["unknown"]["prompt_tokens"] == 400
    assert by_model["unknown"]["completion_tokens"] == 120
    assert by_model["unknown"]["total_tokens"] == 520


def test_openai_usage_zero_token_call_without_model():
    ai = AIState()
    ai.record_openai_usage({"prompt_tokens": 0, "completion_tokens": 0}, model="")
    stats = ai.openai_usage_stats()
    assert stats["call_count"] == 1
    assert stats["prompt_tokens"] == 0
    assert stats["completion_tokens"] == 0
    assert stats["total_tokens"] == 0
    assert "unknown" in stats["by_model"]
    assert stats["by_model"]["unknown"]["calls"] == 1
    assert stats["by_model"]["unknown"]["prompt_tokens"] == 0
    assert stats["by_model"]["unknown"]["completion_tokens"] == 0
    assert stats["by_model"]["unknown"]["total_tokens"] == 0


def test_openai_usage_dirty_and_invalid_tokens():
    ai = AIState()
    # Dirty formatted strings
    ai.record_openai_usage(
        {"prompt_tokens": "1,200", "completion_tokens": "300.5"},
        model="custom-model",
    )
    stats = ai.openai_usage_stats()
    assert stats["prompt_tokens"] == 1200
    assert stats["completion_tokens"] == 300
    assert stats["total_tokens"] == 1500

    # Negative tokens ignored
    ai.record_openai_usage(
        {"prompt_tokens": -50, "completion_tokens": 100},
        model="custom-model",
    )
    stats = ai.openai_usage_stats()
    assert stats["call_count"] == 1  # not incremented for negative tokens

    # Non-dict usage ignored
    ai.record_openai_usage("not-a-dict", model="custom-model")  # type: ignore[arg-type]
    stats = ai.openai_usage_stats()
    assert stats["call_count"] == 1


def test_provider_separation_and_ai_usage_stats():
    ai = AIState()
    ai.record_mistral_usage({"prompt_tokens": 100, "completion_tokens": 50}, model="mistral-large")
    ai.record_openai_usage({"prompt_tokens": 300, "completion_tokens": 150}, model="gpt-4o")

    combined = ai.ai_usage_stats()
    assert "mistral" in combined
    assert "openai_compatible" in combined

    assert combined["mistral"]["prompt_tokens"] == 100
    assert combined["mistral"]["completion_tokens"] == 50
    assert "mistral-large" in combined["mistral"]["by_model"]

    assert combined["openai_compatible"]["prompt_tokens"] == 300
    assert combined["openai_compatible"]["completion_tokens"] == 150
    assert "gpt-4o" in combined["openai_compatible"]["by_model"]
