import sys
import json
import re

def process_command(cmd_line: str) -> str:
    if not cmd_line or not isinstance(cmd_line, str):
        return cmd_line

    # rg または rg.exe の呼び出し箇所を探す
    rg_regex = re.compile(r'(^|[|;&\n]\s*)(rg(?:\.exe)?)\b', re.IGNORECASE)
    match = rg_regex.search(cmd_line)
    if not match:
        return cmd_line

    rg_start = match.start(2)
    rg_end = match.end(2)

    before_rg = cmd_line[:rg_end]
    after_rg = cmd_line[rg_end:]

    # クォート外のコマンド区切り (| ; & \n) を探す
    in_quote = None
    cmd_boundary = len(after_rg)
    for idx, char in enumerate(after_rg):
        if char in ('"', "'"):
            if in_quote is None:
                in_quote = char
            elif in_quote == char:
                in_quote = None
        elif in_quote is None and char in ('|', ';', '&', '\n'):
            cmd_boundary = idx
            break

    rg_args = after_rg[:cmd_boundary]
    after_rg_cmd = after_rg[cmd_boundary:]

    # rg_args を解析
    has_globs = "--glob" in rg_args or "-g " in rg_args

    options_to_inject = []
    if "--max-filesize" not in rg_args:
        options_to_inject.append("--max-filesize 2M")
    if not has_globs:
        options_to_inject.append('--glob "!.venv/**" --glob "!node_modules/**" --glob "!.git/**" --glob "!__pycache__/**"')

    # パスが指定されているかチェック
    token_pattern = re.compile(r'("[^"]*"|\'[^\']*\'|\S+)')
    raw_tokens = token_pattern.findall(rg_args)

    positional_args = []
    i = 0
    has_regexp_flag = False
    while i < len(raw_tokens):
        t = raw_tokens[i]
        if t.startswith("-"):
            if t in ["-e", "--regexp"]:
                has_regexp_flag = True
                i += 2
                continue
            if t in ["-t", "--type", "-g", "--glob", "-m", "--max-count", "--max-filesize", "-C", "-B", "-A", "--context"]:
                i += 2
                continue
            i += 1
        else:
            positional_args.append(t)
            i += 1

    needs_path = False
    # 位置引数が1つ（パターンのみ）で、-e/--regexp がない場合 -> パス未指定のため stdin 待ちになる！
    if len(positional_args) == 1 and not has_regexp_flag:
        needs_path = True
    elif len(positional_args) == 0:
        # 位置引数が0個（-e "pattern" のみ、あるいは引数なし） -> パス未指定！
        needs_path = True

    opt_str = (" " + " ".join(options_to_inject)) if options_to_inject else ""
    path_suffix = " ." if needs_path else ""

    new_rg_args = rg_args + path_suffix
    new_cmd = f"{before_rg}{opt_str}{new_rg_args}{after_rg_cmd}"
    return new_cmd

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
