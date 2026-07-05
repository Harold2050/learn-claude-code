#!/usr/bin/env python3
# ↑ shebang 行，详见 s01 注释。
"""
s06: Subagent — spawn sub-agents with fresh messages[] for context isolation.

  Parent Agent                           Subagent
  +------------------+                  +------------------+
  | messages=[...]   |                  | messages=[task]  | <-- fresh
  |                  |   dispatch       |                  |
  | tool: task       | ---------------> | own while loop   |
  |   prompt="..."   |                  |   bash/read/...  |
  |                  |   summary only   |   (max 30 turns) |
  | result = "..."   | <--------------- | return last text |
  +------------------+                  +------------------+
        ^                                      |
        |       intermediate results DISCARDED  |
        +--------------------------------------+

  Subagent tools: bash, read, write, edit, glob (NO task — no recursion)

Changes from s05:
  + task tool + spawn_subagent() with fresh messages[]
  + Safety limit: max 30 turns per subagent
  + extract_text() helper
  Subagent cannot spawn sub-subagents (no task tool in sub_tools).
  Main loop unchanged: task auto-dispatches via TOOL_HANDLERS.

Run: python s06_subagent/code.py
Needs: pip install anthropic python-dotenv + ANTHROPIC_API_KEY in .env
"""
# ↑ 模块 docstring，详见 s01 注释（docstring 内部不能加注释，解释放外面）。
#   本文件核心: 子代理(subagent)。主代理把复杂子任务交给一个"全新上下文"的子代理，
#   子代理跑完后只返回最终摘要，中间过程全部丢弃——节省主代理的上下文空间。

import ast, json, os, subprocess
from pathlib import Path
# ↑ 标准库导入，详见 s05/s02 注释。ast/json 见 s05，os/subprocess 见 s01，Path 见 s02。

try:
    import readline
    readline.parse_and_bind('set bind-tty-special-chars off')
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
CURRENT_TODOS: list[dict] = []
# ↑ 工作目录、客户端、模型、todo 状态，详见 s05 注释。

SYSTEM = (
    # ↑ 系统提示词，详见 s05 注释（括号包裹的多行字符串拼接）。
    f"You are a coding agent at {WORKDIR}. "
    "For complex sub-problems, use the task tool to spawn a subagent."
    # ↑ s06 提示词新增: 引导模型遇到复杂子问题时用 task 工具派子代理。
)

# s06: subagent gets its own system prompt — no task, no recursion
SUB_SYSTEM = (
    # ↑ ★ s06 新增: 子代理专用的系统提示词。和主代理 SYSTEM 分开。
    #   为什么分开? 因为子代理的角色不同: 它只管"完成派来的任务"，不能再派子代理。
    #   "Do not delegate further" = 不要再往下委托(防止无限递归)。
    f"You are a coding agent at {WORKDIR}. "
    "Complete the task you were given, then return a concise summary. "
    "Do not delegate further."
)


# ═══════════════════════════════════════════════════════════
#  FROM s02-s05 (unchanged): Tool Implementations
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

def _normalize_todos(todos):
    # ↑ 继承自 s05，规范化 todo 列表。逐行注释见 s05 的 _normalize_todos。
    if isinstance(todos, str):
        try:
            todos = json.loads(todos)
        except json.JSONDecodeError:
            try:
                todos = ast.literal_eval(todos)
            except (SyntaxError, ValueError):
                return None, "Error: todos must be a list or JSON array string"
    if not isinstance(todos, list):
        return None, "Error: todos must be a list"
    for i, t in enumerate(todos):
        if not isinstance(t, dict):
            return None, f"Error: todos[{i}] must be an object"
        if "content" not in t or "status" not in t:
            return None, f"Error: todos[{i}] missing 'content' or 'status'"
        if t["status"] not in ("pending", "in_progress", "completed"):
            return None, f"Error: todos[{i}] has invalid status '{t['status']}'"
    return todos, None

def run_todo_write(todos: list) -> str:
    # ↑ 继承自 s05，更新 todo 列表。逐行注释见 s05 的 run_todo_write。
    global CURRENT_TODOS
    todos, error = _normalize_todos(todos)
    if error:
        return error
    CURRENT_TODOS = todos
    lines = ["\n\033[33m## Current Tasks\033[0m"]
    for t in CURRENT_TODOS:
        icon = {"pending": " ", "in_progress": "\033[36m▸\033[0m", "completed": "\033[32m✓\033[0m"}[t["status"]]
        lines.append(f"  [{icon}] {t['content']}")
    print("\n".join(lines))
    return f"Updated {len(CURRENT_TODOS)} tasks"

TOOLS = [
    # ↑ 主代理的工具声明列表(继承 s05 的 6 个工具)。逐项注释见 s05 的 TOOLS。
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
    {"name": "todo_write", "description": "Create and manage a task list for your current coding session.",
     "input_schema": {"type": "object", "properties": {"todos": {"type": "array", "items": {"type": "object", "properties": {"content": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}}, "required": ["content", "status"]}}}, "required": ["todos"]}},
]

TOOL_HANDLERS = {
    # ↑ 主代理工具分发映射(继承 s05)。逐行注释见 s05 的 TOOL_HANDLERS。
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob, "todo_write": run_todo_write,
}


# ═══════════════════════════════════════════════════════════
#  NEW in s06: Subagent — fresh messages[], summary only
# ═══════════════════════════════════════════════════════════

SUB_TOOLS = [
    # ★★★ s06 新增: 子代理专属的工具声明列表。
    #   和主代理 TOOLS 的关键区别: 这里【没有 task 工具】。
    #   为什么? 防止子代理再派子子代理 → 无限递归。子代理只能用基础工具干活。
    #   也【没有 todo_write】——子代理是"一次性任务执行者"，不需要规划。
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to a file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in a file once.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "glob", "description": "Find files matching a glob pattern.",
     "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}},
]
# NO "task" tool — prevent recursive spawning
# ↑ 注释强调: 没有 task 工具，防递归。

SUB_HANDLERS = {
    # ↑ 子代理的工具分发映射。和主代理 TOOL_HANDLERS 的区别: 没有 todo_write 和 task。
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob,
}

def extract_text(content) -> str:
    # ★ s06 新增: 从消息内容块里提取纯文字。
    #   模型回复的 content 可能是"块列表"(含 text 块和 tool_use 块)，
    #   这个函数只把 text 块的文字拼出来，忽略 tool_use 块。
    """Extract text from message content blocks."""
    if not isinstance(content, list):
        # ↑ 如果 content 不是列表(比如是纯字符串)，直接转字符串返回。
        return str(content)
    return "\n".join(getattr(b, "text", "") for b in content if getattr(b, "type", None) == "text")
    # ↑ 这行是个"生成器表达式"，拆开理解:
    #   for b in content: 遍历每个内容块 b。
    #   if getattr(b, "type", None) == "text": 只保留 text 类型的块。
    #     getattr(b, "text", ""): 取块的 text 属性，没有就返回空串(不报错)。
    #   "\n".join(...): 用换行把所有文字块拼成一个字符串。
    #   整体: 把所有 text 块的文字提取并拼接。

def spawn_subagent(description: str) -> str:
    # ★★★ s06 核心新增: 派生一个子代理执行任务。
    #   这是 task 工具的处理函数。主代理调 task(description="...") 时执行它。
    #   核心思想:
    #   1. 开一个全新的 messages[](上下文隔离)——子代理看不到主代理的历史
    #   2. 子代理跑自己的 while 循环(最多 30 轮)
    #   3. 只把最终文字摘要返回给主代理，中间历史全部丢弃
    #   好处: 子代理干的脏活累活不污染主代理的上下文。
    """Spawn a subagent with fresh messages[], return summary only."""
    print(f"\n\033[35m[Subagent spawned]\033[0m")
    # ↑ 35m=紫色。打印"子代理已派生"提示。
    messages = [{"role": "user", "content": description}]  # fresh context
    # ↑ ★ 全新的 messages 列表，只有一条消息: 主代理派来的任务描述。
    #   注意: 这是【局部变量】，和主代理的 messages 完全隔离。
    #   子代理看不到主代理之前的对话——这叫"上下文隔离"。

    for _ in range(30):  # safety limit
        # ↑ 最多循环 30 次。_ 是约定俗成的"用不到的循环变量"名。
        #   range(30) 生成 0..29 共 30 个数。30 是安全上限，防子代理无限循环。
        response = client.messages.create(
            model=MODEL, system=SUB_SYSTEM,
            # ↑ 用子代理专属的系统提示词(不是主代理的 SYSTEM)。
            messages=messages, tools=SUB_TOOLS, max_tokens=8000,
            # ↑ 用子代理专属的工具列表(没有 task)。
        )
        messages.append({"role": "assistant", "content": response.content})
        # ↑ 把子代理的回复存进它自己的历史。
        if response.stop_reason != "tool_use":
            break
            # ↑ 子代理不再调工具(任务完成或答完)，跳出循环。
        results = []
        for block in response.content:
            if block.type == "tool_use":
                # Issue 1: subagent also runs hooks (permissions apply)
                blocked = trigger_hooks("PreToolUse", block)
                # ↑ ★ 子代理也跑 hooks! 这意味着权限(黑名单等)对子代理同样生效。
                #   trigger_hooks 详见 s04 注释。
                if blocked:
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": str(blocked)})
                    continue
                handler = SUB_HANDLERS.get(block.name)
                # ↑ 从子代理的 handlers 查(不是主代理的 TOOL_HANDLERS)。
                output = handler(**block.input) if handler else f"Unknown: {block.name}"
                trigger_hooks("PostToolUse", block, output)
                print(f"  \033[90m[sub] {block.name}: {str(output)[:100]}\033[0m")
                # ↑ 灰色打印子代理的工具调用(缩进2格，前面带 [sub] 标识)。
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": output})
        messages.append({"role": "user", "content": results})

    # Issue 5: fallback if safety limit hit during tool_use
    result = extract_text(messages[-1]["content"])
    # ↑ 取子代理最后一条消息的文字(理想情况: 它说完了，最后一条是 text)。
    if not result:
        # ↑ 如果最后一条没有文字(可能停在 tool_result)，往回找最近的 assistant 文字。
        for msg in reversed(messages):
            # ↑ reversed(列表): 反向遍历(从后往前找)。
            if msg["role"] == "assistant":
                result = extract_text(msg["content"])
                if result:
                    break
                    # ↑ 找到有文字的 assistant 消息就停。
        if not result:
            result = "Subagent stopped after 30 turns without final answer."
            # ↑ 实在找不到文字，返回兜底说明(跑了 30 轮没结论)。
    print(f"\033[35m[Subagent done]\033[0m")
    return result  # only summary, entire message history discarded
    # ↑ ★ 只返回最终文字摘要。子代理的整个 messages 历史(局部变量)随函数结束被丢弃。
    #   这就是"只返回摘要，中间历史全丢弃"——省主代理的上下文空间。

# Add task tool to parent's tools
TOOLS.append({
    # ↑ ★ 把 task 工具加到主代理的工具列表(append 末尾添加)。
    #   task 是主代理独有的——子代理的 SUB_TOOLS 里没有它。
    "name": "task",
    "description": "Launch a subagent to handle a complex subtask. Returns only the final conclusion.",
    "input_schema": {"type": "object", "properties": {"description": {"type": "string"}}, "required": ["description"]},
})
TOOL_HANDLERS["task"] = spawn_subagent
# ↑ 在主代理的分发表里登记: task 工具 → spawn_subagent 函数。
#   dict["key"] = value 既能新增也能修改键值。


# ═══════════════════════════════════════════════════════════
#  FROM s04 (unchanged): Hook System
# ═══════════════════════════════════════════════════════════

HOOKS = {"UserPromptSubmit": [], "PreToolUse": [], "PostToolUse": [], "Stop": []}
# ↑ 钩子注册表(继承 s04)，详见 s04 注释。

def register_hook(event: str, callback):
    HOOKS[event].append(callback)
# ↑ 注册钩子(继承 s04)，详见 s04 注释。

def trigger_hooks(event: str, *args):
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:
            return result
    return None
# ↑ 触发钩子(继承 s04)，详见 s04 注释。

DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if="]
# ↑ 黑名单(继承 s05/s04)，详见 s05 注释。

def permission_hook(block):
    # ↑ PreToolUse 钩子: 黑名单检查(继承 s05)。逐行注释见 s05 的 permission_hook。
    """PreToolUse: deny list check."""
    if block.name == "bash":
        for p in DENY_LIST:
            if p in block.input.get("command", ""):
                print(f"\n\033[31m⛔ Blocked: '{p}'\033[0m")
                return "Permission denied"
    return None

def log_hook(block):
    # ↑ PreToolUse 钩子: 记录工具调用(继承 s05)。逐行注释见 s05 的 log_hook。
    """PreToolUse: log tool calls."""
    print(f"\033[90m[HOOK] {block.name}\033[0m")
    return None

def context_inject_hook(query: str):
    # ↑ UserPromptSubmit 钩子(继承 s05)。逐行注释见 s05 的 context_inject_hook。
    """UserPromptSubmit: log working directory."""
    print(f"\033[90m[HOOK] UserPromptSubmit: working in {WORKDIR}\033[0m")
    return None

def summary_hook(messages: list):
    # ↑ Stop 钩子: 统计工具调用次数(继承 s05)。逐行注释见 s05 的 summary_hook。
    """Stop: print tool call count."""
    tool_count = sum(1 for m in messages
                     for b in (m.get("content") if isinstance(m.get("content"), list) else [])
                     if isinstance(b, dict) and b.get("type") == "tool_result")
    print(f"\033[90m[HOOK] Stop: session used {tool_count} tool calls\033[0m")
    return None

register_hook("UserPromptSubmit", context_inject_hook)
register_hook("PreToolUse", permission_hook)
register_hook("PreToolUse", log_hook)
register_hook("Stop", summary_hook)
# ↑ 注册 4 个钩子(继承 s05)。


# ═══════════════════════════════════════════════════════════
#  agent_loop — same as s05 + nag reminder, task auto-dispatches
# ═══════════════════════════════════════════════════════════

rounds_since_todo = 0
# ↑ todo nag 计数器(继承 s05)，详见 s05 注释。

def agent_loop(messages: list):
    # ↑ 主代理的 agent loop。和 s05 几乎一样——task 工具通过 TOOL_HANDLERS 自动派发，
    #   循环主体不需要专门处理子代理(查表执行那行 handler(**block.input) 会调到 spawn_subagent)。
    global rounds_since_todo
    while True:
        # s05: nag reminder
        if rounds_since_todo >= 3 and messages:
            messages.append({"role": "user",
                             "content": "<reminder>Update your todos.</reminder>"})
            rounds_since_todo = 0
        # ↑ todo nag 提醒(继承 s05)，详见 s05 注释。

        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})
        # ↑ 调 API + 存历史，详见 s01 注释。

        if response.stop_reason != "tool_use":
            force = trigger_hooks("Stop", messages)
            if force:
                messages.append({"role": "user", "content": force})
                continue
            return
            # ↑ Stop 钩子 + 退出逻辑(继承 s04/s05)，详见 s04 注释。

        rounds_since_todo += 1
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            blocked = trigger_hooks("PreToolUse", block)
            if blocked:
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": str(blocked)})
                continue

            handler = TOOL_HANDLERS.get(block.name)
            # ↑ ★ 这里 block.name 可能是 "task"。TOOL_HANDLERS["task"] = spawn_subagent。
            output = handler(**block.input) if handler else f"Unknown: {block.name}"
            # ↑ 如果是 task，这里调 spawn_subagent(description=...)，返回子代理的摘要字符串。
            #   摘要作为 tool_result 喂回主代理——主代理只看到结论，看不到子代理的过程。

            trigger_hooks("PostToolUse", block, output)

            if block.name == "todo_write":
                rounds_since_todo = 0
                # ↑ todo 更新了重置计数器(继承 s05)。

            results.append({"type": "tool_result", "tool_use_id": block.id,
                            "content": output})

        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    print("s06: Subagent — spawn sub-agents with fresh context, summary only")
    print("Type a question, press Enter. Type q to quit.\n")

    history = []
    while True:
        try:
            query = input("\033[36ms06 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        trigger_hooks("UserPromptSubmit", query)
        history.append({"role": "user", "content": query})
        agent_loop(history)
        for block in history[-1]["content"]:
            if getattr(block, "type", None) == "text":
                print(block.text)
        print()
# ↑ 入口主循环(继承 s05)，详见 s01/s05 注释。
