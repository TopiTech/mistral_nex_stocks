"""Single-process deployment validation shared by WSGI entry points.

The application owns in-memory caches, background workers, and SSE listener
state.  Those objects are not shared across OS processes, so every supported
WSGI server must run exactly one application process.
"""

from __future__ import annotations

import importlib
import os
import shlex
import sys
from collections.abc import Mapping, Sequence
from typing import Any

_WORKER_ENV_NAMES = (
    "WEB_CONCURRENCY",
    "GUNICORN_WORKERS",
    "UWSGI_PROCESSES",
    "UWSGI_WORKERS",
    "UWSGI_CHEAPER_INITIAL",
)
_GUNICORN_WORKER_OPTIONS = ("--workers",)
_UWSGI_PROCESS_OPTIONS = ("--processes", "--workers", "--cheaper-initial")


class MultiWorkerConfigurationError(RuntimeError):
    """Raised when an execution configuration requests more than one process."""


def worker_validation_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Return whether the fail-closed single-worker guard is enabled."""
    source = os.environ if environ is None else environ
    return source.get("MNS_WORKER_VALIDATION", "1").strip().lower() not in (
        "0",
        "false",
        "no",
    )


def _positive_int(value: Any) -> int | None:
    """Parse a positive process count without treating booleans as integers."""
    if isinstance(value, bool):
        return None
    if isinstance(value, bytes):
        try:
            value = value.decode("ascii")
        except UnicodeDecodeError:
            return None
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _tokens_from_env(environ: Mapping[str, str], env_name: str) -> list[str]:
    """Split one server-specific command environment value defensively."""
    raw = environ.get(env_name, "")
    if not raw:
        return []
    try:
        return shlex.split(raw)
    except ValueError:
        # A malformed optional argument string must not prevent startup; a
        # separate environment/native-module setting can still be checked.
        return []


def _counts_from_command_tokens(tokens: Sequence[str], *, server: str) -> list[int]:
    """Return positive process counts for one concrete server's option syntax.

    Gunicorn's ``-p`` is its pidfile option, while uWSGI uses ``-p`` as a
    compact process-count option.  Do not interpret the latter outside an
    explicit uWSGI command context.
    """
    long_options: tuple[str, ...]
    short_options: tuple[str, ...]
    if server == "gunicorn":
        long_options = _GUNICORN_WORKER_OPTIONS
        short_options = ("-w",)
    elif server == "uwsgi":
        long_options = _UWSGI_PROCESS_OPTIONS
        short_options = ("-p",)
    else:
        return []

    counts: list[int] = []
    for index, token in enumerate(tokens):
        value: str | None = None
        if token in long_options or token in short_options:
            if index + 1 < len(tokens):
                value = tokens[index + 1]
        else:
            for option in long_options:
                prefix = f"{option}="
                if token.startswith(prefix):
                    value = token[len(prefix) :]
                    break
            if server == "gunicorn" and value is None and token.startswith("-w") and len(token) > 2:
                value = token[2:]
            if server == "uwsgi" and value is None and token.startswith("-p") and len(token) > 2:
                # uWSGI's compact -pN form configures its process count.
                value = token[2:]

        parsed = _positive_int(value)
        if parsed is not None:
            counts.append(parsed)
    return counts


def _argv_is_uwsgi(argv: Sequence[str], uwsgi_module: Any) -> bool:
    """Identify an actual uWSGI command without guessing from generic flags."""
    if argv:
        executable = os.path.basename(argv[0]).lower()
        # An explicit server executable is stronger evidence than an imported
        # module (which may merely be installed in the environment).  In
        # particular, do not let an available ``uwsgi`` module reinterpret
        # Gunicorn's ``-p`` pidfile flag as a process-count option.
        if executable.startswith("gunicorn"):
            return False
        if executable.startswith("uwsgi"):
            return True
    return uwsgi_module is not None


def _resolve_uwsgi_module(uwsgi_module: Any) -> Any:
    """Load uWSGI's native module only when a caller did not supply one."""
    if uwsgi_module is not None:
        return uwsgi_module
    try:
        return importlib.import_module("uwsgi")
    except ImportError:
        return None


def _is_enabled_uwsgi_value(value: Any) -> bool:
    """Whether a uWSGI option value enables a feature such as cheaper mode."""
    if isinstance(value, bytes):
        try:
            value = value.decode("ascii")
        except UnicodeDecodeError:
            return True
    normalized = str(value or "").strip().lower()
    return bool(normalized) and normalized not in ("0", "false", "no", "off")


def _uwsgi_cheaper_is_configured(
    environ: Mapping[str, str], argv: Sequence[str], uwsgi_module: Any
) -> bool:
    """Return whether uWSGI's dynamic ``cheaper`` worker mode is enabled.

    Cheaper may spawn additional processes after startup, so it is incompatible
    with the application's process-local state even when ``cheaper-initial``
    happens to be one.  Reject the mode outright instead of trying to infer its
    future scaling ceiling from a mix of uWSGI settings.
    """
    if _is_enabled_uwsgi_value(environ.get("UWSGI_CHEAPER")):
        return True

    token_sets = [_tokens_from_env(environ, "UWSGI_CMD_ARGS")]
    if _argv_is_uwsgi(argv, uwsgi_module):
        token_sets.append(list(argv[1:]))
    for tokens in token_sets:
        for token in tokens:
            if token == "--cheaper" or token.startswith("--cheaper="):
                return True

    try:
        options = uwsgi_module.opt
    except AttributeError:
        options = None
    if isinstance(options, Mapping):
        for key in ("cheaper", b"cheaper"):
            if key in options and _is_enabled_uwsgi_value(options[key]):
                return True
    return False


def _counts_from_uwsgi_module(uwsgi_module: Any) -> list[int]:
    """Read the active uWSGI process count when embedded by uWSGI itself.

    ``uwsgi --ini`` keeps its settings in the native module rather than in
    ``sys.argv``.  Checking the module closes that configuration-file bypass.
    The public values differ slightly between uWSGI versions, so inspect both
    attributes and the option mapping defensively.
    """
    if uwsgi_module is None:
        return []

    counts: list[int] = []
    for attr_name in ("numproc", "processes", "workers"):
        try:
            value = getattr(uwsgi_module, attr_name)
            if callable(value):
                value = value()
        except (AttributeError, TypeError, ValueError):
            continue
        parsed = _positive_int(value)
        if parsed is not None:
            counts.append(parsed)

    try:
        options = uwsgi_module.opt
    except AttributeError:
        options = None
    if isinstance(options, Mapping):
        for option_name in ("processes", "workers", "cheaper-initial"):
            for key in (option_name, option_name.encode("ascii")):
                value = options.get(key)
                if isinstance(value, (list, tuple)):
                    value = value[-1] if value else None
                parsed = _positive_int(value)
                if parsed is not None:
                    counts.append(parsed)
    return counts


def detect_configured_worker_count(
    *,
    environ: Mapping[str, str] | None = None,
    argv: Sequence[str] | None = None,
    uwsgi_module: Any = None,
) -> int:
    """Return the highest explicitly configured process count, or one.

    Taking the maximum is intentional: conflicting deployment inputs must not
    allow a multi-worker setting to be masked by an unrelated ``=1`` value.
    """
    source = os.environ if environ is None else environ
    command_argv = sys.argv if argv is None else argv
    counts = [
        parsed
        for env_name in _WORKER_ENV_NAMES
        if (parsed := _positive_int(source.get(env_name))) is not None
    ]
    uwsgi_module = _resolve_uwsgi_module(uwsgi_module)

    # Command-environment variables are server-specific, so never feed a
    # Gunicorn pidfile value through uWSGI's ``-p`` parser (or vice versa).
    counts.extend(
        _counts_from_command_tokens(
            _tokens_from_env(source, "GUNICORN_CMD_ARGS"), server="gunicorn"
        )
    )
    counts.extend(
        _counts_from_command_tokens(_tokens_from_env(source, "UWSGI_CMD_ARGS"), server="uwsgi")
    )
    argv_server = "uwsgi" if _argv_is_uwsgi(command_argv, uwsgi_module) else "gunicorn"
    counts.extend(_counts_from_command_tokens(command_argv[1:], server=argv_server))
    counts.extend(_counts_from_uwsgi_module(uwsgi_module))
    return max(counts, default=1)


def enforce_single_worker(
    *,
    environ: Mapping[str, str] | None = None,
    argv: Sequence[str] | None = None,
    uwsgi_module: Any = None,
) -> int:
    """Raise when enabled validation finds an unsupported process count."""
    source = os.environ if environ is None else environ
    if not worker_validation_enabled(source):
        return 1
    command_argv = sys.argv if argv is None else argv
    resolved_uwsgi_module = _resolve_uwsgi_module(uwsgi_module)
    if _uwsgi_cheaper_is_configured(source, command_argv, resolved_uwsgi_module):
        raise MultiWorkerConfigurationError(
            "uWSGI cheaper mode is not supported because it can spawn additional "
            "processes after startup."
        )
    worker_count = detect_configured_worker_count(
        environ=source,
        argv=command_argv,
        uwsgi_module=resolved_uwsgi_module,
    )
    if worker_count > 1:
        raise MultiWorkerConfigurationError(
            f"Multi-worker mode detected (workers={worker_count}). "
            "This application uses in-memory singleton state and is only supported "
            "with a single worker process."
        )
    return worker_count
