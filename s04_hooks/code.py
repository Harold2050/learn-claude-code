#!/usr/bin/env python3
"""
s04: Hooks — move extension logic out of the loop, onto hooks.

  User types query
       │
       ▼
  ┌──────────────────┐
  │ UserPromptSubmit │ ── trigger_hooks() before LLM
  └────────┬─────────┘
           ▼
  ┌────────────┐     ┌─────────────────────────────┐
  │  messages  │────▶│  LLM (stop_reason=tool_use?)│
  └────────────┘     │   No ──▶ Stop hooks ──▶ exit │
                     │   Yes ──▶ tool_use block ──┐ │
                     └────────────────────────────┘ │
                                                    ▼
                                          ┌──────────────────┐
                                          │ trigger_hooks()   │
                                          │  PreToolUse:      │
                                          │   permission_hook │
                                          │   log_hook        │
                                          └───────┬──────────┘
                                                  │ (not blocked)
                                          ┌───────▼──────────┐
                                          │ TOOL_HANDLERS[x]  │
                                          └───────┬──────────┘
                                                  │
                                          ┌───────▼──────────┐
                                          │ trigger_hooks()   │
                                          │  PostToolUse:     │
                                          │   large_output    │
                                          └───────┬──────────┘
                                                  │
                                          results ──▶ back to messages

Changes from s03:
  + HOOKS registry (event -> list of callbacks)
  + register_hook() / trigger_hooks()
  + context_inject_hook (UserPromptSubmit)
  + permission_hook, log_hook (PreToolUse)
  + large_output_hook (PostToolUse)
  + summary_hook (Stop)
  - check_permission() removed from loop body
    (logic moved into permission_hook, triggered via PreToolUse)

Run: python s04_hooks/code.py
Needs: pip install anthropic python-dotenv + ANTHROPIC_API_KEY in .env
"""

import os, subprocess
from pathlib import Path
# ↑ 标准库导入，详见 s02 注释。

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

SYSTEM = f"You are a coding agent at {WORKDIR}. Use tools to solve tasks. Act, don't explain."
# ↑ 系统提示词(同 s02 风格)。


# ═══════════════════════════════════════════════════════════
#  FROM s02-s03 (unchanged): Tool Implementations
# ═══════════════════════════════════════════════════════════

def safe_path(p: str) -> Path:
    # ↑ 继承自 s02，路径安全校验。逐行注释见 s02 的 safe_path。
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path

def run_bash(command: str) -> str:
    # ↑ 继承自 s01/s02，执行 shell 命令。逐行注释见 s02 的 run_bash。
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

TOOLS = [
    # ↑ 5 个工具声明(同 s02)，逐行注释见 s02 的 TOOLS。
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
#  NEW in s04: Hook System (s03 permission logic now via hooks)
# ═══════════════════════════════════════════════════════════

HOOKS = {"UserPromptSubmit": [], "PreToolUse": [], "PostToolUse": [], "Stop": []}
# ★★★ s04 灵魂: "钩子注册表"。
#   一个字典，key 是"事件名"，value 是"回调函数列表"(初始为空)。
#   四个事件点:
#     UserPromptSubmit: 用户提交输入后、调 LLM 前
#     PreToolUse: 工具执行前
#     PostToolUse: 工具执行后
#     Stop: 循环即将退出时
#   思想: 把扩展逻辑挂在事件点上，循环主体保持干净(只调 trigger_hooks)。
#   这就是"钩子模式"(hook pattern)，类似 git hooks、webpack plugins。

def register_hook(event: str, callback):
    # ↑ 注册一个钩子: 把 callback 函数加到 HOOKS[event] 列表里。
    #   event: 事件名(如 "PreToolUse")。
    #   callback: 要执行的函数。
    HOOKS[event].append(callback)
    # ↑ 列表的 append 方法: 末尾添加元素。

def trigger_hooks(event: str, *args):
    # ↑ 触发某事件的所有钩子: 按注册顺序逐个调用。
    #   *args: 可变参数。把所有传进来的参数"打包"成元组 args。
    #   这样不同事件的钩子可以接收不同参数(PreToolUse 传 block；PostToolUse 传 block+output)。
    for callback in HOOKS[event]:
        # ↑ 遍历该事件的所有回调函数。
        result = callback(*args)
        # ↑ callback(*args): 调用回调函数。*args 是"解包": 把元组展开成位置参数。
        #   * 在函数定义时是"打包"，在调用时是"解包"，符号一样但作用相反。
        if result is not None:  # teaching shortcut: block this tool call
            # ↑ ★ 教学版约定: 回调返回非 None 表示"拦截"。
            #   如果钩子返回了值(字符串)，就立即停止后续钩子，返回这个值。
            #   这让 PreToolUse 钩子能拦截工具执行(返回拦截原因)。
            return result
    return None
    # ↑ 所有钩子都返回 None(没拦截)，返回 None(放行)。


# s03 permission check logic, now wrapped as a hook
DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if="]
DESTRUCTIVE = ["rm ", "> /etc/", "chmod 777"]
# ↑ 两个列表: 绝对禁止(黑名单)、需要询问(破坏性)。

def permission_hook(block):
    # ↑ PreToolUse 钩子: 把 s03 的 check_permission 逻辑搬到这里。
    #   接收一个 block 参数(trigger_hooks("PreToolUse", block) 传进来的)。
    """PreToolUse: s03 check_permission() logic moved here."""
    if block.name == "bash":
        # ↑ 对 bash 工具: 先查黑名单，再查破坏性命令。
        for pattern in DENY_LIST:
            if pattern in block.input.get("command", ""):
                # ↑ 黑名单命中。block.input.get("command", "") 见 s03 注释。
                print(f"\n\033[31m⛔ Blocked: '{pattern}'\033[0m")
                return "Permission denied by deny list"
                # ↑ ★ 返回非 None 字符串 → trigger_hooks 拦截工具。
        for kw in DESTRUCTIVE:
            if kw in block.input.get("command", ""):
                # ↑ 破坏性命令命中 → 询问用户。
                print(f"\n\033[33m⚠  Potentially destructive command\033[0m")
                print(f"   Tool: {block.name}({block.input})")
                choice = input("   Allow? [y/N] ").strip().lower()
                if choice not in ("y", "yes"):
                    # ↑ 用户不答应(not in)，拒绝。
                    return "Permission denied by user"
    if block.name in ("write_file", "edit_file"):
        # ↑ 对写工具: 检查是否写工作区外。
        path = block.input.get("path", "")
        if not (WORKDIR / path).resolve().is_relative_to(WORKDIR):
            # ↑ 路径逃出工作区 → 询问用户。逻辑同 s03 的规则，详见 s03 注释。
            print(f"\n\033[33m⚠  Writing outside workspace\033[0m")
            print(f"   Tool: {block.name}({block.input})")
            choice = input("   Allow? [y/N] ").strip().lower()
            if choice not in ("y", "yes"):
                return "Permission denied by user"
    return None
    # ↑ 返回 None = 不拦截(放行)。trigger_hooks 约定见上方。

def log_hook(block):
    # ↑ PreToolUse 钩子: 记录每次工具调用(日志)。
    """PreToolUse: log every tool call."""
    args_preview = str(list(block.input.values())[:2])[:60]
    # ↑ 这行有点复杂，拆开看:
    #   block.input.values(): 参数字典的所有值(如 ["ls -la"] 或 ["a.txt", "hi"])。
    #   list(...): 转成列表。
    #   [:2]: 切片，只取前 2 个(防止参数太多)。
    #   str(...): 转字符串。
    #   [:60]: 再切片，只取前 60 个字符(防止太长)。
    #   最终得到参数的简短预览。
    print(f"\033[90m[HOOK] {block.name}({args_preview})\033[0m")
    # ↑ 90m=灰色。灰色打印日志(不抢眼)。
    return None
    # ↑ 返回 None = 不拦截(只记录，不影响执行)。

def large_output_hook(block, output):
    # ↑ PostToolUse 钩子: 工具执行后检查输出大小。
    #   接收两个参数: block 和 output(trigger_hooks 传两个)。
    """PostToolUse: warn on large output."""
    if len(str(output)) > 100000:
        # ↑ 输出超过 10 万字符 → 警告(可能撑爆上下文)。
        print(f"\033[33m[HOOK] ⚠ Large output from {block.name}: {len(str(output))} chars\033[0m")
    return None

# UserPromptSubmit hook: log user input before it reaches the LLM
def context_inject_hook(query: str):
    # ↑ UserPromptSubmit 钩子: 用户输入后、调 LLM 前触发。
    #   接收 query(用户输入的字符串)。
    print(f"\033[90m[HOOK] UserPromptSubmit: working in {WORKDIR}\033[0m")
    return None

# Stop hook: print summary when loop is about to exit
def summary_hook(messages: list):
    # ↑ Stop 钩子: 循环退出前触发。接收 messages(整个对话历史)。
    """Stop: print summary when loop is about to exit"""
    tool_count = sum(1 for m in messages
                     for b in (m.get("content") if isinstance(m.get("content"), list) else [])
                     if isinstance(b, dict) and b.get("type") == "tool_result")
    # ↑ ★ 这行用了"嵌套生成器表达式"统计工具调用次数，拆开理解:
    #   sum(1 for ... ): 数有多少个满足条件的元素(每满足一次加 1)。
    #   for m in messages: 第一层循环，遍历每条消息 m。
    #   for b in (...): 第二层循环，遍历消息里的每个内容块 b。
    #     m.get("content"): 取消息内容。
    #     if isinstance(..., list): 内容是列表才遍历(字符串跳过)。
    #     else []: 不是列表就用空列表(不遍历)。
    #   if isinstance(b, dict) and b.get("type") == "tool_result":
    #     只数 tool_result 类型的块。
    #   整体: 统计所有消息里 tool_result 块的总数 = 工具调用总次数。
    print(f"\033[90m[HOOK] Stop: session used {tool_count} tool calls\033[0m")
    return None

register_hook("UserPromptSubmit", context_inject_hook)
register_hook("PreToolUse", permission_hook)
register_hook("PreToolUse", log_hook)
# ↑ 注意: 同一事件可以注册多个钩子。PreToolUse 注册了 permission 和 log 两个。
#   执行顺序 = 注册顺序(先 permission 后 log)。permission 返回非 None 的话，
#   trigger_hooks 会跳过后续钩子(log 不执行)。
register_hook("PostToolUse", large_output_hook)
register_hook("Stop", summary_hook)
# ↑ 把 5 个钩子注册到 4 个事件点上。从此循环只需调 trigger_hooks，不用关心具体逻辑。


# ═══════════════════════════════════════════════════════════
#  agent_loop — same structure as s03, but no hard-coded check
#  s03: if not check_permission(block): ...
#  s04: if trigger_hooks("PreToolUse", block): ...
# ═══════════════════════════════════════════════════════════

def agent_loop(messages: list):
    # ↑ agent 循环。和 s03 结构一样，但权限检查改用钩子触发。
    while True:
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})
        # ↑ 调 API + 存历史，详见 s01 注释。

        if response.stop_reason != "tool_use":
            # ↑ 模型不调工具(答完了)。
            force = trigger_hooks("Stop", messages)
            # ↑ ★ s04: 退出前触发 Stop 钩子(打印统计等)。
            #   force 接收钩子返回值。返回非 None 表示"强制继续"(教学版特性)。
            if force:
                messages.append({"role": "user", "content": force})
                continue
                # ↑ 如果 Stop 钩子返回了内容，把它作为 user 消息塞进去，继续循环。
                #   (这是教学演示，实际 Stop 钩子通常返回 None。)
            return
            # ↑ 正常退出。

        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            # s04 change: hook replaces hard-coded check_permission()
            blocked = trigger_hooks("PreToolUse", block)
            # ↑ ★★★ s04 核心改动: 工具执行前触发 PreToolUse 钩子。
            #   s03 写 if not check_permission(block)，s04 改成 trigger_hooks。
            #   好处: 循环不关心具体权限逻辑，全由钩子决定(可扩展)。
            if blocked:
                # ↑ blocked 非 None 表示某钩子拦截了。
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": str(blocked)})
                # ↑ 把拦截原因作为工具结果喂回模型。
                continue
                # ↑ 跳过实际执行。

            handler = TOOL_HANDLERS.get(block.name)
            output = handler(**block.input) if handler else f"Unknown: {block.name}"
            # ↑ 查表执行工具，详见 s02 注释。

            trigger_hooks("PostToolUse", block, output)  # s04: post hook
            # ↑ ★ s04: 工具执行后触发 PostToolUse 钩子(如大输出警告)。
            #   传两个参数: block 和 output。

            results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})

        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    print("s04: Hooks — extension logic on hooks, loop stays clean")
    print("Type a question, press Enter. Type q to quit.\n")

    history = []
    while True:
        try:
            query = input("\033[36ms04 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        trigger_hooks("UserPromptSubmit", query)
        # ↑ ★ s04: 用户输入后、调 agent_loop 前，触发 UserPromptSubmit 钩子。
        history.append({"role": "user", "content": query})
        agent_loop(history)
        for block in history[-1]["content"]:
            if getattr(block, "type", None) == "text":
                print(block.text)
        print()
# ↑ 入口主循环，逻辑与 s01-s03 一致，详见 s01 注释。
