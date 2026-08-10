"""Regression guard for the standalone successful-bootstrap CI smoke."""

from pathlib import Path


def test_ci_runs_bootstrap_smoke_without_pytest_conftest():
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "python tests/startup_smoke_runner.py" in workflow
    assert "MNS_SKIP_BOOTSTRAP: \"0\"" in workflow
