#!/usr/bin/env python3
# ↑ shebang 行，详见 s01 注释。
"""
s12: Task System — file-persisted task graph with blockedBy dependencies.

Run:  python s12_task_system/code.py
Need: pip install anthropic python-dotenv + .env with ANTHROPIC_API_KEY

Changes from s11:
  - Task dataclass (id, subject, description, status, owner, blockedBy)
  - TASKS_DIR = .tasks/ for persistent JSON storage
  - create_task / save_task / load_task / list_tasks / get_task
  - can_start: checks blockedBy all completed (missing deps = blocked)
  - claim_task: set owner + pending -> in_progress
  - complete_task: set completed + report unblocked downstream
  - 5 new tools: create_task, list_tasks, get_task, claim_task, complete_task

Note: Teaching code keeps a basic agent loop to stay focused on the task
system. S11's full error recovery (RecoveryState, backoff, escalation,
reactive compact, fallback model) is omitted — in real CC, tasks.ts and
withRetry are independent layers that compose naturally.
"""
# ↑ 模块 docstring，详见 s01 注释。
#   本文件核心: 文件持久化的任务图 + blockedBy 依赖。
#   前面的 todo_write(s05)只在内存里存任务列表(重启就丢)。
#   s12 把每个任务存成独立 JSON 文件(.tasks/task_xxx.json)，重启还在。
#   关键: blockedBy 依赖——任务B依赖任务A，A没完成B就不能开始。

import os, subprocess, json, time, random
# ↑ 标准库导入。time/random 见 s11；json 见 s09。
from pathlib import Path
# ↑ pathlib.Path，详见 s02 注释。
from dataclasses import dataclass, asdict
# ↑ ★ s12 新增: dataclasses 模块。
#   @dataclass: 装饰器，自动生成 __init__/__repr__ 等方法(省去手写样板代码)。
#   asdict(对象): 把 dataclass 对象转成字典(便于 JSON 序列化)。

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
MEMORY_DIR = WORKDIR / ".memory"
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]
# ↑ 工作目录、记忆、客户端、模型(继承 s05/s10)。详见对应注释。

# ── Task System ──

TASKS_DIR = WORKDIR / ".tasks"
# ↑ ★ s12 新增: 任务持久化目录 .tasks/。每个任务一个 JSON 文件。
TASKS_DIR.mkdir(exist_ok=True)
# ↑ 确保目录存在。mkdir(exist_ok=True) 详见 s09。


@dataclass
# ↑ ★★★ 装饰器: 给下面的类自动生成样板方法(__init__/__repr__/__eq__等)。
#   没有它，你得手写 def __init__(self, id, subject, ...): self.id = id ...
#   有它，只需声明字段，Python 自动生成构造函数。
class Task:
    # ★★★ s12 核心新增: 任务数据类。每个字段一行声明(带类型注解)。
    id: str
    # ↑ 任务唯一 ID(如 "task_1700000000_0042")。
    subject: str
    # ↑ 任务标题(一句话)。
    description: str
    # ↑ 任务详细描述。
    status: str          # pending | in_progress | completed
    # ↑ 任务状态: pending(待办)/in_progress(进行中)/completed(完成)。# 后是注释。
    owner: str | None    # Agent name (multi-agent scenarios)
    # ↑ 认领者(多 agent 场景谁在做这个)。None 表示无人认领。
    blockedBy: list[str] # Dependency task IDs
    # ↑ ★ 依赖列表: 这个任务等哪些任务完成才能开始。list[str] = 字符串列表。


def _task_path(task_id: str) -> Path:
    # ↑ s12 辅助: 根据任务 ID 拼出 JSON 文件路径。下划线前缀表示内部函数。
    return TASKS_DIR / f"{task_id}.json"


def create_task(subject: str, description: str = "",
                blockedBy: list[str] | None = None) -> Task:
    # ★ s12 新增: 创建一个新任务(生成 ID + 存盘)。
    task = Task(
        id=f"task_{int(time.time())}_{random.randint(0, 9999):04d}",
        # ↑ ★ ID 生成: "task_" + 时间戳 + 随机数。
        #   int(time.time()): 当前时间戳转整数(秒)。详见 s08。
        #   random.randint(0, 9999): 0-9999 随机整数。
        #   :04d: 格式化为至少4位(不足补零)。如 42 → "0042"。这是 f-string 格式说明符。
        #   组合如 "task_1700000000_0042"。时间戳+随机数保证唯一性。
        subject=subject,
        description=description,
        status="pending",
        # ↑ 新任务默认 pending(待办)。
        owner=None,
        # ↑ 无人认领。
        blockedBy=blockedBy or [],
        # ↑ blockedBy or []: 如果 blockedBy 是 None(没传)，用空列表。
        #   or 短路: None 是假值，所以返回后面的 []。详见 s01 的三元表达式。
    )
    save_task(task)
    return task


def save_task(task: Task):
    # ★ s12 新增: 把任务存成 JSON 文件(持久化)。
    _task_path(task.id).write_text(json.dumps(asdict(task), indent=2))
    # ↑ asdict(task): dataclass 对象转字典。
    #   json.dumps(..., indent=2): 字典转 JSON 文本，缩进2空格(美观)。
    #   write_text(...): 写入文件。


def load_task(task_id: str) -> Task:
    # ★ s12 新增: 从 JSON 文件读出任务。
    return Task(**json.loads(_task_path(task_id).read_text()))
    # ↑ ★★★ 这行很精妙，拆解:
    #   read_text(): 读 JSON 文本。
    #   json.loads(...): JSON 文本解析成字典。
    #   Task(**字典): 字典解包构造。** 把字典展开成关键字参数。
    #     如 Task(**{"id":"t1","subject":"x",...}) 等价于 Task(id="t1", subject="x", ...)。
    #   整体: 读文件→解析字典→用字典构造 Task 对象。


def list_tasks() -> list[Task]:
    # ★ s12 新增: 列出所有任务。
    return [Task(**json.loads(p.read_text()))
            for p in sorted(TASKS_DIR.glob("task_*.json"))]
    # ↑ ★ 列表推导: 遍历 .tasks/ 下所有 task_*.json 文件，每个读出转 Task 对象。
    #   TASKS_DIR.glob("task_*.json"): glob 通配符匹配(*匹配任意)。详见 s09 的 Path.glob。
    #   sorted(...): 排序(按文件名，保证稳定顺序)。
    #   [Task(**json.loads(p.read_text())) for p in ...]: 每个文件 p 读出转 Task。


def get_task(task_id: str) -> str:
    # ★ s12 新增: 获取任务详情(返回 JSON 文本)。
    """Return full task details as JSON."""
    task = load_task(task_id)
    return json.dumps(asdict(task), indent=2)


def can_start(task_id: str) -> bool:
    # ★★★ s12 核心新增: 检查任务能否开始(所有依赖是否完成)。
    #   依赖系统的灵魂: 任务B的 blockedBy=[A]，A没完成B就不能开始。
    """Check if all blockedBy dependencies are completed.
    Missing dependencies are treated as blocked."""
    task = load_task(task_id)
    for dep_id in task.blockedBy:
        # ↑ 遍历每个依赖任务 ID。
        if not _task_path(dep_id).exists():
            return False
            # ↑ ★ 依赖任务文件不存在 → 当作阻塞(防御性: 宁可卡住也不能乱跑)。
        if load_task(dep_id).status != "completed":
            return False
            # ↑ 依赖任务未完成 → 阻塞。
    return True
    # ↑ 所有依赖都完成 → 可以开始。


def claim_task(task_id: str, owner: str = "agent") -> str:
    # ★★★ s12 核心新增: 认领任务(pending → in_progress，设 owner)。
    #   多 agent 场景下，agent 用这个"锁住"任务，防止其他 agent 重复做。
    task = load_task(task_id)
    if task.status != "pending":
        return f"Task {task_id} is {task.status}, cannot claim"
        # ↑ 只能认领 pending 状态的任务。
    if not can_start(task_id):
        deps = [d for d in task.blockedBy
                if not _task_path(d).exists() or load_task(d).status != "completed"]
        return f"Blocked by: {deps}"
        # ↑ 依赖没完成 → 列出阻塞的依赖，拒绝认领。
    task.owner = owner
    task.status = "in_progress"
    save_task(task)
    # ↑ 设 owner + 状态改 in_progress + 存盘。
    print(f"  \033[36m[claim] {task.subject} → in_progress (owner: {owner})\033[0m")
    return f"Claimed {task.id} ({task.subject})"


def complete_task(task_id: str) -> str:
    # ★★★ s12 核心新增: 完成任务(in_progress → completed，报告解锁的下游)。
    task = load_task(task_id)
    if task.status != "in_progress":
        return f"Task {task_id} is {task.status}, cannot complete"
        # ↑ 只能完成 in_progress 状态的任务。
    task.status = "completed"
    save_task(task)
    unblocked = [t.subject for t in list_tasks()
                 if t.status == "pending" and t.blockedBy and can_start(t.id)]
    # ↑ ★ 列表推导: 找出所有因这次完成而"解锁"的任务。
    #   遍历所有任务 t，条件:
    #   - t.status == "pending": 还是待办(可能被阻塞着)。
    #   - t.blockedBy: 有依赖(没依赖的不算"解锁"，本来就 能开始)。
    #   - can_start(t.id): 现在依赖全完成了(刚完成的任务让它们解锁了)。
    print(f"  \033[32m[complete] {task.subject} ✓\033[0m")
    msg = f"Completed {task.id} ({task.subject})"
    if unblocked:
        msg += f"\nUnblocked: {', '.join(unblocked)}"
        # ★ 报告哪些下游任务解锁了(让模型知道可以继续做它们)。
        print(f"  \033[33m[unblocked] {', '.join(unblocked)}\033[0m")
    return msg


# ── Prompt Assembly (from s10, synced) ──

PROMPT_SECTIONS = {
    # ↑ 提示片段字典(继承 s10)。tools 段加了任务工具列表。
    "identity": "You are a coding agent. Act, don't explain.",
    "tools": "Available tools: bash, read_file, write_file, "
             "create_task, list_tasks, get_task, claim_task, complete_task.",
    "workspace": f"Working directory: {WORKDIR}",
    "memory": "Relevant memories are injected below when available.",
}


def assemble_system_prompt(context: dict) -> str:
    # ↑ 组装提示(继承 s10)。逐行注释见 s10。
    sections = [PROMPT_SECTIONS["identity"],
                PROMPT_SECTIONS["tools"],
                PROMPT_SECTIONS["workspace"]]
    memories = context.get("memories", "")
    if memories:
        sections.append(f"Relevant memories:\n{memories}")
    return "\n\n".join(sections)


_last_context_key, _last_prompt = None, None


def get_system_prompt(context: dict) -> str:
    # ↑ 带缓存的提示组装(继承 s10)。逐行注释见 s10。
    global _last_context_key, _last_prompt
    key = json.dumps(context, sort_keys=True, ensure_ascii=False, default=str)
    if key == _last_context_key and _last_prompt:
        return _last_prompt
    _last_context_key = key
    _last_prompt = assemble_system_prompt(context)
    return _last_prompt


# ── Tools ──

def safe_path(p: str) -> Path:
    # ↑ 继承自 s02。逐行注释见 s02 的 safe_path。
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_bash(command: str) -> str:
    # ↑ 继承自 s01/s02。逐行注释见 s02 的 run_bash。
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"


def run_read(path: str, limit: int | None = None) -> str:
    # ↑ 继承自 s02。逐行注释见 s02 的 run_read。
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str) -> str:
    # ↑ 继承自 s02。逐行注释见 s02 的 run_write。
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


# Task tools
# ↓ 5 个任务工具的处理函数(包装上面的核心函数，加打印)。

def run_create_task(subject: str, description: str = "",
                    blockedBy: list[str] | None = None) -> str:
    # ↑ 创建任务工具的处理函数。
    task = create_task(subject, description, blockedBy)
    deps = f" (blockedBy: {', '.join(blockedBy)})" if blockedBy else ""
    print(f"  \033[34m[create] {task.subject}{deps}\033[0m")
    return f"Created {task.id}: {task.subject}{deps}"


def run_list_tasks() -> str:
    # ↑ 列任务工具。返回格式化的任务清单文本。
    tasks = list_tasks()
    if not tasks:
        return "No tasks. Use create_task to add some."
    lines = []
    for t in tasks:
        icon = {"pending": "○", "in_progress": "●",
                "completed": "✓"}.get(t.status, "?")
        # ↑ 按状态选图标。dict.get(key, "?"): 没匹配返回 "?"。详见 s02。
        deps = f" (blockedBy: {', '.join(t.blockedBy)})" if t.blockedBy else ""
        owner = f" [{t.owner}]" if t.owner else ""
        lines.append(f"  {icon} {t.id}: {t.subject} "
                     f"[{t.status}]{owner}{deps}")
    return "\n".join(lines)


def run_get_task(task_id: str) -> str:
    # ↑ 查任务详情工具。
    try:
        return get_task(task_id)
    except FileNotFoundError:
        return f"Error: Task {task_id} not found"
        # ↑ 任务文件不存在 → 返回错误(而非崩溃)。


def run_claim_task(task_id: str) -> str:
    # ↑ 认领任务工具。
    return claim_task(task_id, owner="agent")


def run_complete_task(task_id: str) -> str:
    # ↑ 完成任务工具。
    return complete_task(task_id)


TOOLS = [
    # ↑ 工具声明列表。前3个继承 s10，后5个是 s12 新增的任务工具。
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object",
                      "properties": {"command": {"type": "string"}},
                      "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "limit": {"type": "integer"}},
                      "required": ["path"]}},
    {"name": "write_file", "description": "Write content to a file.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "content": {"type": "string"}},
                      "required": ["path", "content"]}},
    {"name": "create_task",
     # ↑ ★ s12 新工具: 创建任务。
     "description": "Create a new task with optional blockedBy dependencies.",
     "input_schema": {"type": "object",
                      "properties": {
                          "subject": {"type": "string"},
                          "description": {"type": "string"},
                          "blockedBy": {"type": "array",
                                        "items": {"type": "string"}}},
                      "required": ["subject"]}},
    {"name": "list_tasks",
     "description": "List all tasks with status, owner, and dependencies.",
     "input_schema": {"type": "object", "properties": {},
                      "required": []}},
    {"name": "get_task",
     "description": "Get full details of a specific task by ID.",
     "input_schema": {"type": "object",
                      "properties": {"task_id": {"type": "string"}},
                      "required": ["task_id"]}},
    {"name": "claim_task",
     "description": "Claim a pending task. Sets owner, changes status to in_progress.",
     "input_schema": {"type": "object",
                      "properties": {"task_id": {"type": "string"}},
                      "required": ["task_id"]}},
    {"name": "complete_task",
     "description": "Complete an in-progress task. Reports unblocked downstream tasks.",
     "input_schema": {"type": "object",
                      "properties": {"task_id": {"type": "string"}},
                      "required": ["task_id"]}},
]

TOOL_HANDLERS = {
    # ↑ 工具分发映射。新增 5 个任务工具的映射。
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "create_task": run_create_task, "list_tasks": run_list_tasks,
    "get_task": run_get_task, "claim_task": run_claim_task,
    "complete_task": run_complete_task,
}


# ── Context ──

def update_context(context: dict, messages: list) -> dict:
    # ↑ 更新上下文(继承 s10)。逐行注释见 s10。
    """Derive context from real state."""
    memories = ""
    if MEMORY_INDEX.exists():
        content = MEMORY_INDEX.read_text().strip()
        if content:
            memories = content
    return {
        "enabled_tools": list(TOOL_HANDLERS.keys()),
        "workspace": str(WORKDIR),
        "memories": memories,
    }


# ── Agent Loop (simplified, focused on task system) ──

def agent_loop(messages: list, context: dict):
    # ★ s12 agent loop: 简化版(聚焦任务系统)。
    #   ★ 注意: docstring 已声明——本章【故意省略】s11的完整错误恢复，
    #   只保留最简 try/except。这是教学简化(详见 AGENTS.md "看似 bug 的简化")。
    system = get_system_prompt(context)
    while True:
        try:
            response = client.messages.create(
                model=MODEL, system=system, messages=messages,
                tools=TOOLS, max_tokens=8000)
        except Exception as e:
            messages.append({"role": "assistant", "content": [
                {"type": "text",
                 "text": f"[Error] {type(e).__name__}: {e}"}]})
            # ↑ type(e).__name__: 异常类名。详见 s11。
            return
            # ↑ 简化处理: 任何错误都记录退出(没有 s11 的退避/升级/压缩)。

        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            return

        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            print(f"\033[36m> {block.name}\033[0m")
            handler = TOOL_HANDLERS.get(block.name)
            # ↑ ★ 任务工具(create_task/claim_task 等)通过 TOOL_HANDLERS 自动派发。
            #   循环主体不变——加工具只需登记 TOOLS + TOOL_HANDLERS。这是 s02 查表模式的威力。
            output = handler(**block.input) if handler else f"Unknown: {block.name}"
            print(str(output)[:300])
            results.append({"type": "tool_result",
                            "tool_use_id": block.id, "content": output})
        messages.append({"role": "user", "content": results})
        context = update_context(context, messages)
        system = get_system_prompt(context)


if __name__ == "__main__":
    print("s12: task system")
    print("Enter a question, press Enter to send. Type q to quit.\n")
    history = []
    context = update_context({}, [])
    while True:
        try:
            query = input("\033[36ms12 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history.append({"role": "user", "content": query})
        agent_loop(history, context)
        context = update_context(context, history)
        for block in history[-1]["content"]:
            if getattr(block, "type", None) == "text":
                print(block.text)
            elif isinstance(block, dict) and block.get("type") == "text":
                print(block.get("text", ""))
                # ↑ 兼容两种 text 块: SDK 对象(getattr) 和字典(isinstance dict)。
        print()
# ↑ 入口主循环(继承 s10/s11)。详见 s10 注释。
