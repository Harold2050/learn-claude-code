#!/usr/bin/env python3
"""
s05: TodoWrite — add a planning tool on top of s04 hooks.

  +---------+      +-------+      +------------------+
  |  User   | ---> |  LLM  | ---> | TOOL_HANDLERS    |
  | prompt  |      |       |      |  bash            |
  +---------+      +---+---+      |  read_file       |
                        ^         |  write_file      |
                        | result  |  edit_file       |
                        +---------+  glob            |
                                       todo_write ← NEW
                                    +------------------+
                                         |
                          in-memory current_todos
                                         |
                         if rounds_since_todo >= 3:
                           inject <reminder>

Changes from s04:
  + todo_write tool + run_todo_write() implementation
  + Nag reminder (inject reminder after 3 rounds without todo update)
  + SYSTEM prompt includes "plan before execute" guidance
  + rounds_since_todo counter in agent_loop
  Loop unchanged: new tool auto-dispatches via TOOL_HANDLERS.

Run: python s05_todo_write/code.py
Needs: pip install anthropic python-dotenv + ANTHROPIC_API_KEY in .env
"""

import ast, json, os, subprocess
# ↑ 导入标准库。一行导入多个，逗号分隔。
#   ast:  Abstract Syntax Tree(抽象语法树)。这里用来安全解析 Python 字面量。
#   json: JSON 编解码。把字符串解析成 Python 对象。
#   os, subprocess: 详见 s01/s02 注释。
#   ★ ast 和 json 是 s05 新增(s01-s04 没有)，用于解析模型传来的 todos 参数。
from pathlib import Path
# ↑ pathlib.Path，详见 s02 注释。

try:
    import readline
    readline.parse_and_bind('set bind-tty-special-chars off')
except ImportError:
    pass
# ↑ readline 中文输入修复(s05 只保留了最关键的一行)，详见 s01 注释。

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
CURRENT_TODOS: list[dict] = []
# ↑ ★ s05 新增: 全局变量，存"当前任务列表"。
#   list[dict] 是"类型注解": 表示这是一个"字典列表"(列表里每个元素是字典)。
#   初始为空列表 []。模型调 todo_write 时会更新它。
#   注意: 这是"全局可变状态"——函数里改它要用 global 声明(见 run_todo_write)。

# s05 change: SYSTEM prompt adds planning guidance
SYSTEM = (
    # ↑ 用括号包裹的字符串，会自动拼接成一行(隐式字符串连接)。
    #   作用: 把长字符串拆成多行写，更易读。等价于一行写完。
    f"You are a coding agent at {WORKDIR}. "
    "Before starting any multi-step task, use todo_write to plan your steps. "
    "Update status as you go."
)
# ↑ ★ s05 系统提示词改动: 加入"计划先行"引导。
#   告诉模型: 多步任务先用 todo_write 规划，边做边更新状态。
#   这是"软约束"——不是代码强制，而是靠系统提示引导模型行为。


# ═══════════════════════════════════════════════════════════
#  FROM s02-s04 (unchanged): Tool Implementations
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


# ═══════════════════════════════════════════════════════════
#  NEW in s05: todo_write tool — plan only, no execution
# ═══════════════════════════════════════════════════════════

def _normalize_todos(todos):
    # ↑ ★ s05 新增: 把模型传来的 todos "规范化"成标准格式。
    #   函数名前的下划线 _ 是 Python 惯例: 表示"内部函数"(不希望外部调用)。
    #   模型可能传 JSON 字符串、Python 字面量字符串、或直接列表，这里统一处理。
    #   返回 (规范化的todos, 错误信息)。错误信息为 None 表示成功。
    if isinstance(todos, str):
        # ↑ 如果模型传的是字符串(JSON 文本)，需要先解析成 Python 对象。
        #   (模型有时把列表当成字符串传，要兼容这种情况。)
        try:
            todos = json.loads(todos)
            # ↑ json.loads(字符串): 把 JSON 文本解析成 Python 对象。
            #   如 '[{"content":"x","status":"pending"}]' → [{'content':'x','status':'pending'}]
            #   loads = load string。
        except json.JSONDecodeError:
            # ↑ 如果不是合法 JSON，尝试用 ast 解析。
            try:
                todos = ast.literal_eval(todos)
                # ↑ ast.literal_eval(字符串): 安全地解析 Python 字面量。
                #   能解析 字符串/数字/列表/字典/元组/布尔/None。
                #   比 eval 安全(不会执行任意代码，只解析数据)。
                #   适合模型有时用 Python 风格传参(如单引号)。
            except (SyntaxError, ValueError):
                # ↑ 两种解析都失败，返回错误。
                return None, "Error: todos must be a list or JSON array string"
    if not isinstance(todos, list):
        # ↑ 检查解析后是不是列表。不是就报错。
        return None, "Error: todos must be a list"
    for i, t in enumerate(todos):
        # ↑ enumerate(列表): 同时获得"索引 i"和"元素 t"。如 [(0, todos[0]), (1, todos[1]), ...]
        if not isinstance(t, dict):
            return None, f"Error: todos[{i}] must be an object"
            # ↑ 每个任务必须是字典(对象)。
        if "content" not in t or "status" not in t:
            # ↑ 每个任务必须有 content(内容)和 status(状态)两个键。
            #   or: 逻辑或，任一缺失就报错。
            return None, f"Error: todos[{i}] missing 'content' or 'status'"
        if t["status"] not in ("pending", "in_progress", "completed"):
            # ↑ status 只能是这三种合法值。
            #   pending=待办、in_progress=进行中、completed=已完成。
            return None, f"Error: todos[{i}] has invalid status '{t['status']}'"
    return todos, None
    # ↑ 全部校验通过，返回 (规范化的todos, None表示无错误)。

def run_todo_write(todos: list) -> str:
    # ↑ ★ s05 核心新增: todo_write 工具的实现。
    #   注意: 这个工具"只规划不执行"——它只更新任务列表，不真的跑任务。
    #   任务的实际执行靠模型后续调 bash/read_file 等工具。
    global CURRENT_TODOS
    # ↑ global 关键字: 声明我要修改全局变量 CURRENT_TODOS(不是创建局部变量)。
    #   没有 global 的话，赋值会创建一个局部变量，全局变量不变。
    todos, error = _normalize_todos(todos)
    # ↑ 规范化校验。返回 (todos, error) 元组，解包赋值。
    if error:
        return error
        # ↑ 有错误直接返回，不更新。
    CURRENT_TODOS = todos
    # ↑ 用新列表替换全局任务列表。
    lines = ["\n\033[33m## Current Tasks\033[0m"]
    # ↑ 准备打印的任务清单。开头一个黄色标题。列表里先放标题这一行。
    for t in CURRENT_TODOS:
        # ↑ 遍历每个任务，生成一行显示。
        icon = {"pending": " ", "in_progress": "\033[36m▸\033[0m", "completed": "\033[32m✓\033[0m"}[t["status"]]
        # ↑ 根据状态选图标(查字典):
        #   pending(待办): 空格
        #   in_progress(进行中): ▸ 青色
        #   completed(完成): ✓ 绿色
        #   字典[键] 取值，这里 t["status"] 是键。
        lines.append(f"  [{icon}] {t['content']}")
        # ↑ 每个任务一行: 缩进2空格 + [图标] + 内容。
    print("\n".join(lines))
    # ↑ 把所有行用换行拼起来打印。
    return f"Updated {len(CURRENT_TODOS)} tasks"
    # ↑ 返回成功信息(告诉模型更新了几个任务)。

TOOLS = [
    # ↑ 工具声明列表。前 5 个同 s02，第 6 个是 s05 新增。
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
    # s05: new tool
    {"name": "todo_write", "description": "Create and manage a task list for your current coding session.",
     # ↑ ★ s05 新工具: todo_write。描述告诉模型"用来管理任务列表"。
     "input_schema": {"type": "object", "properties": {"todos": {"type": "array",
     # ↑ todos 参数是数组(array = JSON 的数组 = Python 的列表)。
        "items": {"type": "object", "properties": {"content": {"type": "string"},
     # ↑ items 描述数组每个元素的结构: 每个是个对象，含 content(字符串)...
        "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}},
     # ↑ ...和 status。status 用 enum(枚举)限定只能是这三种值。
        "required": ["content", "status"]}}}, "required": ["todos"]}},
     # ↑ content 和 status 都是必填。todos 本身必填。
]

TOOL_HANDLERS = {
    # ↑ 工具分发映射(同 s02 模式)，新增 todo_write 一行。
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob, "todo_write": run_todo_write,
}


# ═══════════════════════════════════════════════════════════
#  FROM s04 (unchanged): Hook System
# ═══════════════════════════════════════════════════════════

HOOKS = {"UserPromptSubmit": [], "PreToolUse": [], "PostToolUse": [], "Stop": []}
# ↑ 钩子注册表(同 s04)，逐行注释见 s04 的 HOOKS。

def register_hook(event: str, callback):
    HOOKS[event].append(callback)
# ↑ 注册钩子(同 s04)，逐行注释见 s04。

def trigger_hooks(event: str, *args):
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:
            return result
    return None
# ↑ 触发钩子(同 s04)，逐行注释见 s04。

# s04 hooks preserved
DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if="]
# ↑ 黑名单(同 s04)。

def permission_hook(block):
    # ↑ PreToolUse 钩子: 黑名单检查(简化版，只保留 deny list，去掉了 s04 的破坏性询问)。
    """PreToolUse: deny list check."""
    if block.name == "bash":
        for p in DENY_LIST:
            if p in block.input.get("command", ""):
                print(f"\n\033[31m⛔ Blocked: '{p}'\033[0m")
                return "Permission denied"
    return None

def log_hook(block):
    # ↑ PreToolUse 钩子: 记录工具调用(简化版)。逐行注释见 s04 的 log_hook。
    """PreToolUse: log tool calls."""
    print(f"\033[90m[HOOK] {block.name}\033[0m")
    return None

def context_inject_hook(query: str):
    # ↑ UserPromptSubmit 钩子(同 s04)。逐行注释见 s04。
    """UserPromptSubmit: log working directory."""
    print(f"\033[90m[HOOK] UserPromptSubmit: working in {WORKDIR}\033[0m")
    return None

def summary_hook(messages: list):
    # ↑ Stop 钩子: 统计工具调用次数(同 s04)。逐行注释见 s04 的 summary_hook。
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
# ↑ 注册 4 个钩子(同 s04，去掉了 PostToolUse 的 large_output，简化教学)。


# ═══════════════════════════════════════════════════════════
#  agent_loop — same as s04 + nag reminder counter
# ═══════════════════════════════════════════════════════════

rounds_since_todo = 0
# ↑ ★ s05 新增: "距上次更新 todo 的轮数"计数器(全局变量)。
#   每轮 +1，调了 todo_write 就清零。达到 3 就注入提醒。

def agent_loop(messages: list):
    global rounds_since_todo
    # ↑ 声明修改全局变量 rounds_since_todo。
    while True:
        # s05: nag reminder — inject if model hasn't updated todos for 3 rounds
        if rounds_since_todo >= 3 and messages:
            # ↑ ★ s05 核心: 如果 3 轮没更新 todo，就注入提醒。
            #   >= 3: 大于等于 3。and messages: 且历史非空(防止首轮就触发)。
            messages.append({"role": "user",
                             "content": "<reminder>Update your todos.</reminder>"})
            # ↑ 把提醒作为 user 消息注入历史。模型下一轮会看到它。
            #   <reminder>标签让模型更容易识别这是系统提醒(而非真用户)。
            rounds_since_todo = 0
            # ↑ 注入后清零计数(给模型新一轮的"宽限期")。

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
            # ↑ Stop 钩子 + 退出逻辑(同 s04)，详见 s04 注释。

        rounds_since_todo += 1
        # ↑ ★ s05: 每轮工具调用后，计数器 +1。
        #   += 是"加赋值": rounds_since_todo = rounds_since_todo + 1 的简写。
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
            output = handler(**block.input) if handler else f"Unknown: {block.name}"
            # ↑ PreToolUse 钩子 + 查表执行工具(同 s04)，详见 s04/s02 注释。

            trigger_hooks("PostToolUse", block, output)

            # s05: reset nag counter when todo_write is called
            if block.name == "todo_write":
                rounds_since_todo = 0
                # ↑ ★ s05: 模型调了 todo_write，计数器清零(它有在规划，不用 nag 了)。

            results.append({"type": "tool_result", "tool_use_id": block.id,
                            "content": output})

        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    print("s05: TodoWrite — plan before execute, nag if you forget")
    print("Type a question, press Enter. Type q to quit.\n")

    history = []
    while True:
        try:
            query = input("\033[36ms05 >> \033[0m")
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
# ↑ 入口主循环(同 s04)，详见 s01/s04 注释。
