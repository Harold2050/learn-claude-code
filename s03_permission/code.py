#!/usr/bin/env python3
"""
s03_permission.py - Permission System

Three gates inserted before tool execution:

    Gate 1: Hard deny list (rm -rf /, sudo, ...)
    Gate 2: Rule matching (write outside workspace? destructive cmd?)
    Gate 3: User approval (pause and wait for confirmation)

    +-------+    +--------+    +--------+    +--------+    +------+
    | Tool  | -> | Gate 1 | -> | Gate 2 | -> | Gate 3 | -> | Exec |
    | call  |    | deny?  |    | match? |    | allow? |    |      |
    +-------+    +--------+    +--------+    +--------+    +------+
         |            |             |             |
         v            v             v             v
      (normal)     (blocked)    (ask user)   (user says no?)

Only one line added to the agent loop:

    if not check_permission(block):
        continue

Builds on s02 (multi-tool). Usage:

    python s03_permission/code.py
    Needs: pip install anthropic python-dotenv + ANTHROPIC_API_KEY in .env
"""

import os, subprocess
from pathlib import Path
# ↑ 导入模块，详见 s02 注释。os/subprocess/pathlib.Path 都是标准库。

try:
    import readline
    readline.parse_and_bind('set bind-tty-special-chars off')
    readline.parse_and_bind('set input-meta on')
    readline.parse_and_bind('set output-meta on')
    readline.parse_and_bind('set convert-meta off')
except ImportError:
    pass
# ↑ readline 中文输入修复，详见 s01 注释。

from anthropic import Anthropic
from dotenv import load_dotenv
# ↑ SDK 和 dotenv，详见 s01 注释。

load_dotenv(override=True)
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
# ↑ 加载 .env、兼容第三方端点，详见 s01 注释。

WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]
# ↑ 工作目录、客户端、模型 ID，详见 s02 注释。

SYSTEM = f"You are a coding agent at {WORKDIR}. All destructive operations require user approval."
# ↑ 系统提示词。s03 版强调"破坏性操作需要用户批准"，配合权限系统。


# ═══════════════════════════════════════════════════════════
#  FROM s02 (unchanged): Tool Implementations
# ═══════════════════════════════════════════════════════════

def safe_path(p: str) -> Path:
    # ↑ 继承自 s02，路径安全校验。逐行注释见 s02 的 safe_path。
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_bash(command: str) -> str:
    # ↑ 继承自 s01/s02，执行 shell 命令。逐行注释见 s02 的 run_bash。
    #   注意: s03 的 run_bash 把"危险命令黑名单"移到了权限层(check_deny_list)，
    #   这里只负责执行，不再内置黑名单(权限统一在 check_permission 管)。
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"


def run_read(path: str, limit: int | None = None) -> str:
    # ↑ 继承自 s02，读文件。逐行注释见 s02 的 run_read。
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str) -> str:
    # ↑ 继承自 s02，写文件。逐行注释见 s02 的 run_write。
    try:
        file_path = safe_path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    # ↑ 继承自 s02，编辑文件。逐行注释见 s02 的 run_edit。
    try:
        file_path = safe_path(path)
        text = file_path.read_text()
        if old_text not in text:
            return f"Error: text not found in {path}"
        file_path.write_text(text.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


def run_glob(pattern: str) -> str:
    # ↑ 继承自 s02，通配符查文件。逐行注释见 s02 的 run_glob。
    import glob as g
    try:
        results = []
        for match in g.glob(pattern, root_dir=WORKDIR):
            if (WORKDIR / match).resolve().is_relative_to(WORKDIR):
                results.append(match)
        return "\n".join(results) if results else "(no matches)"
    except Exception as e:
        return f"Error: {e}"


# ═══════════════════════════════════════════════════════════
#  FROM s02 (unchanged): Tool Definitions & Dispatch
# ═══════════════════════════════════════════════════════════

TOOLS = [
    # ↑ 5 个工具的声明(同 s02)，逐行注释见 s02 的 TOOLS。
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to a file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in a file once.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "glob", "description": "Find files matching a glob pattern.",
     "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}},
]

TOOL_HANDLERS = {
    # ↑ 工具分发映射(同 s02)，逐行注释见 s02 的 TOOL_HANDLERS。
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob,
}


# ═══════════════════════════════════════════════════════════
#  NEW in s03: Three-Gate Permission Pipeline
# ═══════════════════════════════════════════════════════════

# Gate 1: Hard deny list — always forbidden
# ↓ ★ 闸门1: 硬黑名单。无论何时都禁止的命令(命中就直接拒绝，不询问)。
DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if=", "> /dev/sda"]
# ↑ 一个字符串列表。每条都是"绝对禁止"的危险操作模式。

def check_deny_list(command: str) -> str | None:
    # ↑ 返回类型 str | None: 要么返回字符串(原因)，要么返回 None(表示没命中)。
    #   这是 Python 的"联合类型"，表示返回值可能是两种类型之一。
    for pattern in DENY_LIST:
        # ↑ 遍历黑名单里每一条危险模式。
        if pattern in command:
            # ↑ 子串匹配: 如果危险模式出现在命令里。
            return f"Blocked: '{pattern}' is on the deny list"
            # ↑ 命中，返回拦截原因(字符串)。
    return None
    # ↑ 全部检查完没命中，返回 None(放行)。


# Gate 2: Rule matching — context-dependent checks
# ↓ ★ 闸门2: 规则匹配。根据"工具+参数"做上下文相关的检查。
#   和黑名单的区别: 黑名单是"绝对禁止"；规则是"可疑→询问用户"。
PERMISSION_RULES = [
    # ↑ 一个"规则字典"列表。每条规则描述一种需要警惕的情况。
    {"tools": ["write_file", "edit_file"],
     # ↑ 这条规则作用于 write_file 和 edit_file 这两个工具。
     "check": lambda args: not (WORKDIR / args.get("path", "")).resolve().is_relative_to(WORKDIR),
     # ↑ lambda 是"匿名函数"(一行函数)。语法: lambda 参数: 表达式。
     #   这里 lambda args: ... 定义一个接收 args 参数的函数。
     #   args.get("path", ""): 从参数字典取 path，不存在则用空串。
     #   整体含义: 检查"写入路径是否在工作区外"。not 在前: 在外面就返回 True(可疑)。
     "message": "Writing outside workspace"},
     # ↑ 如果 check 返回 True，显示这条提示给用户。
    {"tools": ["bash"],
     # ↑ 这条规则作用于 bash 工具。
     "check": lambda args: any(kw in args.get("command", "") for kw in ["rm ", "> /etc/", "chmod 777"]),
     # ↑ 检查命令里是否含危险关键词(rm 删除、> /etc/ 改系统文件、chmod 777 全权限)。
     #   any(...) 见 s01 注释: 只要有任何一个匹配就返回 True(可疑)。
     "message": "Potentially destructive command"},
]

def check_rules(tool_name: str, args: dict) -> str | None:
    # ↑ 检查工具调用是否触发某条规则。
    for rule in PERMISSION_RULES:
        # ↑ 遍历每条规则。
        if tool_name in rule["tools"] and rule["check"](args):
            # ↑ 两个条件都满足:
            #   1) tool_name in rule["tools"]: 当前工具在这条规则的作用范围里。
            #   2) rule["check"](args): 调用规则的 lambda 函数，传 args 参数。
            #      返回 True 表示触发了可疑情况。
            #   and: 逻辑与，两边都 True 才进 if。
            return rule["message"]
            # ↑ 触发了，返回规则的提示语(交给闸门3去问用户)。
    return None
    # ↑ 没触发任何规则，返回 None(放行)。


# Gate 3: User approval — wait for confirmation after rule match
# ↓ ★ 闸门3: 用户审批。规则触发后，暂停问用户"允许吗?"。
def ask_user(tool_name: str, args: dict, reason: str) -> str:
    # ↑ 返回 "allow" 或 "deny" 字符串。
    print(f"\n\033[33m⚠  {reason}\033[0m")
    # ↑ 打印警告(⚠ 图标 + 黄色)。reason 是闸门2返回的提示语。
    print(f"   Tool: {tool_name}({args})")
    # ↑ 显示是哪个工具、传了什么参数，方便用户判断。
    choice = input("   Allow? [y/N] ").strip().lower()
    # ↑ input: 等待用户输入。.strip().lower(): 去空格转小写。
    #   [y/N] 的惯例: 大写 N 表示默认(直接回车=N 拒绝)。
    return "allow" if choice in ("y", "yes") else "deny"
    # ↑ 输入 y 或 yes 返回 "allow"，其他(包括空/回车)都返回 "deny"。


# Pipeline: all three gates chained
# ↓ ★★★ 把三道闸门串起来: 这是 agent_loop 调用的总入口。
def check_permission(block) -> bool:
    # ↑ 接收一个 tool_use 块，返回 True(允许)或 False(拒绝)。
    #   block.name: 工具名；block.input: 参数字典。
    if block.name == "bash":
        # ↑ 闸门1 只对 bash 有意义(危险命令)。其他工具不查黑名单。
        reason = check_deny_list(block.input.get("command", ""))
        # ↑ 跑闸门1。block.input.get("command", ""): 取 command 参数，没有就空串。
        if reason:
            # ↑ 如果 reason 非 None(命中黑名单)。
            print(f"\n\033[31m⛔ {reason}\033[0m")
            # ↑ 红色打印拦截原因(⛔ 图标)。31m=红色。
            return False
            # ↑ 直接拒绝，不继续后面闸门。
    reason = check_rules(block.name, block.input)
    # ↑ 跑闸门2(规则匹配)。对所有工具都查。
    if reason:
        # ↑ 触发了某条规则(reason 非 None)。
        decision = ask_user(block.name, block.input, reason)
        # ↑ 跑闸门3(问用户)。
        if decision == "deny":
            # ↑ 用户拒绝。
            return False
    return True
    # ↑ 没命中黑名单、没触发规则、或用户允许——放行。


# ═══════════════════════════════════════════════════════════
#  agent_loop — same as s02, with check_permission() inserted
# ═══════════════════════════════════════════════════════════

def agent_loop(messages: list):
    # ↑ agent 循环。和 s02 几乎一样，唯一区别: 工具执行前加权限检查。
    while True:
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})
        # ↑ 调 API + 存历史，详见 s01 注释。

        if response.stop_reason != "tool_use":
            return
            # ↑ 不调工具就结束，详见 s01 注释。

        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
                # ↑ continue: 跳过本次循环剩余部分，直接进入下一次循环。
                #   这里跳过非工具块(如 text 块)。和 s02 的 if block.type == "tool_use" 等价，
                #   只是写法反过来(用 continue 排除)。

            print(f"\033[36m> {block.name}\033[0m")
            # ↑ 青色打印工具名。36m=青色。

            # s03 change: run through permission pipeline before executing
            if not check_permission(block):
                # ↑ ★★★ s03 核心改动: 工具执行前先过权限管线。
                #   not check_permission(block): check_permission 返回 False(拒绝)时，
                #   not False = True，进入 if 块。
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": "Permission denied."})
                # ↑ 拒绝的话，把"权限拒绝"作为工具结果喂回模型。
                #   模型看到后会知道这个操作不被允许，可能换方案。
                continue
                # ↑ 跳过下面的实际执行(continue 直接进下一次循环)。

            handler = TOOL_HANDLERS.get(block.name)
            output = handler(**block.input) if handler else f"Unknown: {block.name}"
            # ↑ 查表执行工具，** 解包参数，详见 s02 注释。
            print(str(output)[:200])
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})

        messages.append({"role": "user", "content": results})
        # ↑ 喂回结果，详见 s01 注释。


if __name__ == "__main__":
    print("s03: Permission")
    print("输入问题，回车发送。输入 q 退出。\n")

    history = []
    while True:
        try:
            query = input("\033[36ms03 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history.append({"role": "user", "content": query})
        agent_loop(history)
        for block in history[-1]["content"]:
            if getattr(block, "type", None) == "text":
                print(block.text)
        print()
# ↑ 入口主循环，逻辑与 s01/s02 一致，详见 s01 注释。
