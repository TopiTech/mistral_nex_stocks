"""Regression guards for CI configuration and repository metadata."""

import re
import subprocess
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


def test_ci_verifies_exported_production_requirements():
    """Prevent removal of the CI check that catches stale requirements exports."""
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "Verify exported production requirements" in workflow
    assert "uv export --locked --no-hashes --no-dev" in workflow
    assert "requirements-locked.generated.txt" in workflow


def test_unix_launcher_is_tracked_as_executable():
    """A shebang launcher must remain executable after a POSIX Git checkout."""
    result = subprocess.run(
        ["git", "ls-files", "--stage", "--", "run_app.sh"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    entries = result.stdout.splitlines()
    assert len(entries) == 1, result.stdout
    assert entries[0].split(maxsplit=1)[0] == "100755", entries[0]
