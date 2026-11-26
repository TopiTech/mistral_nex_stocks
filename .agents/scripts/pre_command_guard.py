import json
import re
import sys

_VALUE_FLAGS = frozenset({
    "-e",
    "--regexp",
    "-f",
    "--file",
    "-t",
    "--type",
    "-T",
    "--type-not",
    "--type-add",
    "--type-clear",
    "-g",
    "--glob",
    "--iglob",
    "-m",
    "--max-count",
    "--max-filesize",
    "-C",
    "-B",
    "-A",
    "--context",
    "--after-context",
    "--before-context",
    "--encoding",
    "-E",
    "--threads",
    "-j",
    "--max-depth",
    "-d",
    "--sort",
    "--sortr",
    "--color",
    "--colors",
    "--replace",
    "-r",
    "--context-separator",
    "--field-context-separator",
    "--field-match-separator",
    "--path-separator",
    "--pre",
    "--pre-glob",
    "-M",
    "--max-columns",
    "--engine",
})

_NON_SEARCH_FLAGS = frozenset({
    "--type-list",
    "--version",
    "-V",
    "--help",
    "-h",
    "--files",
})


def _tokenize_args(arg_str: str) -> list[str]:
    """Split shell arguments respecting single/double quotes and backslash escapes."""
    tokens: list[str] = []
    buf: list[str] = []
    in_quote: str | None = None
    i = 0
    n = len(arg_str)
    while i < n:
        ch = arg_str[i]
        if in_quote == "'":
            if ch == "'":
                in_quote = None
            else:
                buf.append(ch)
            i += 1
            continue
        if in_quote == '"':
            if ch == "\\" and i + 1 < n and arg_str[i + 1] in ('"', "\\", "$", "`"):
                buf.append(arg_str[i + 1])
                i += 2
                continue
            if ch == '"':
                in_quote = None
            else:
                buf.append(ch)
            i += 1
            continue
        if ch in ('"', "'"):
            in_quote = ch
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            buf.append(arg_str[i + 1])
            i += 2
            continue
        if ch.isspace():
            if buf:
                tokens.append("".join(buf))
                buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    if buf:
        tokens.append("".join(buf))
    return tokens


def _split_shell_segments(cmd_line: str) -> list[tuple[str, str]]:
    """Split command line on unquoted shell operators, returning (segment, sep)."""
    segments: list[tuple[str, str]] = []
    in_quote: str | None = None
    start = 0
    i = 0
    n = len(cmd_line)
    while i < n:
        ch = cmd_line[i]
        if in_quote == "'":
            if ch == "'":
                in_quote = None
            i += 1
            continue
        if in_quote == '"':
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == '"':
                in_quote = None
            i += 1
            continue
        if ch in ('"', "'"):
            in_quote = ch
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if ch in ("|", ";", "&", "\n"):
            sep = ch
            if ch in ("|", "&") and i + 1 < n and cmd_line[i + 1] == ch:
                sep = cmd_line[i : i + 2]
                i += 2
            else:
                i += 1
            segments.append((cmd_line[start : i - len(sep)], sep))
            start = i
            continue
        i += 1
    segments.append((cmd_line[start:], ""))
    return segments


def _rewrite_rg_segment(segment: str) -> str:
    match = re.match(r'^(\s*)(rg(?:\.exe)?)(?=\s|$)(.*)$', segment, re.IGNORECASE | re.DOTALL)
    if not match:
        return segment
    leading, rg_bin, rg_args = match.groups()
    raw_tokens = _tokenize_args(rg_args)

    positional_args: list[str] = []
    i = 0
    has_regexp_flag = False
    has_globs = False
    has_max_filesize = False
    has_non_search = False
    end_of_options = False

    while i < len(raw_tokens):
        t = raw_tokens[i]
        if end_of_options:
            positional_args.append(t)
            i += 1
            continue
        if t == "--":
            end_of_options = True
            i += 1
            continue
        if t.startswith("-") and t != "-":
            if t in _NON_SEARCH_FLAGS:
                has_non_search = True
                i += 1
                continue
            if t in ("-e", "--regexp", "-f", "--file"):
                has_regexp_flag = True
                i += 2
                continue
            if t.startswith(("--regexp=", "-e=", "--file=", "-f=")):
                has_regexp_flag = True
                i += 1
                continue
            if t in ("-g", "--glob", "--iglob"):
                has_globs = True
                i += 2
                continue
            if t.startswith(("--glob=", "--iglob=", "-g=")):
                has_globs = True
                i += 1
                continue
            if t == "--max-filesize":
                has_max_filesize = True
                i += 2
                continue
            if t.startswith("--max-filesize="):
                has_max_filesize = True
                i += 1
                continue
            if t in _VALUE_FLAGS:
                i += 2
                continue
            i += 1
        else:
            positional_args.append(t)
            i += 1

    if has_non_search:
        return segment

    options_to_inject = []
    if not has_max_filesize:
        options_to_inject.append("--max-filesize 2M")
    if not has_globs:
        options_to_inject.append(
            '--glob "!.venv/**" --glob "!node_modules/**" --glob "!.git/**" --glob "!__pycache__/**"'
        )

    needs_path = False
    if (len(positional_args) == 1 and not has_regexp_flag) or len(positional_args) == 0:
        needs_path = True

    opt_str = (" " + " ".join(options_to_inject)) if options_to_inject else ""
    path_suffix = " ." if needs_path else ""
    return f"{leading}{rg_bin}{opt_str}{rg_args}{path_suffix}"


def process_command(cmd_line: str) -> str:
    if not cmd_line or not isinstance(cmd_line, str):
        return cmd_line

    parts: list[str] = []
    for segment, sep in _split_shell_segments(cmd_line):
        parts.append(_rewrite_rg_segment(segment))
        if sep:
            parts.append(sep)
    return "".join(parts)

def main():
    try:
        raw_input = sys.stdin.read()
        if not raw_input.strip():
            print(json.dumps({"decision": "allow"}))
            return

        payload = json.loads(raw_input)
        tool_call = payload.get("toolCall", {})
        tool_name = tool_call.get("name", "")
        args = tool_call.get("args", {})

        if tool_name == "run_command" and "CommandLine" in args:
            orig_cmd = args["CommandLine"]
            modified_cmd = process_command(orig_cmd)
            
            if modified_cmd != orig_cmd:
                response = {
                    "decision": "allow",
                    "overwrite": {
                        "CommandLine": modified_cmd
                    }
                }
                print(json.dumps(response, ensure_ascii=False))
                return

        print(json.dumps({"decision": "allow"}))
    except Exception:
        print(json.dumps({"decision": "allow"}))

if __name__ == "__main__":
    main()
