"""Regression guard: third-party GitHub Actions must use immutable revisions."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_USES_REF = re.compile(r"^\s*uses:\s*([^\s#]+)", re.MULTILINE)
_IMMUTABLE_ACTION_REF = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")


def test_all_github_actions_are_pinned_to_full_commit_shas():
    """A mutable tag can change CI behavior without a repository code change."""
    violations = []
    workflows_dir = ROOT / ".github" / "workflows"
    for workflow in sorted(workflows_dir.glob("*.y*ml")):
        contents = workflow.read_text(encoding="utf-8")
        for action_ref in _USES_REF.findall(contents):
            if not _IMMUTABLE_ACTION_REF.fullmatch(action_ref):
                violations.append(f"{workflow.relative_to(ROOT)}: {action_ref}")

    assert not violations, "GitHub Actions must be pinned to full commit SHAs:\n" + "\n".join(
        violations
    )
