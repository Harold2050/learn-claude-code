#!/usr/bin/env python3
# ↑ shebang 行，详见 s01 注释。
"""
s13: Background Tasks — thread-based async execution + notification injection.

Run:  python s13_background_tasks/code.py
Need: pip install anthropic python-dotenv + .env with ANTHROPIC_API_KEY

Changes from s12:
  - threading.Thread for background execution
  - background_tasks dict for lifecycle tracking (bg_id, command, status)
  - background_results dict + threading.Lock for thread-safe storage
  - should_run_background: model explicit request via run_in_background param
  - is_slow_operation: fallback heuristic when model doesn't specify
  - start_background_task: dispatch to daemon thread, return bg task id
  - collect_background_results: gather completed, return as notifications
  - agent_loop: slow ops → background + placeholder, inject notifications
  - Notifications use <task_notification> format, not reused tool_use_id

Note: Teaching code keeps a basic agent loop to stay focused on background
tasks. S11's full error recovery (RecoveryState, backoff, escalation,
reactive compact, fallback model) is omitted.
"""
# ↑ 模块 docstring，详见 s01 注释。
#   本文件核心: 线程异步执行慢操作 + 通知注入。
#   有些工具(npm install/docker build)要跑几分钟。同步等会卡住 agent。
#   s13 把慢操作丢到后台线程执行，主循环立刻继续。
#   后台完成后，用 <task_notification> XML 注入下一轮对话告诉模型结果。

import os, subprocess, json, time, random, threading
# ↑ 标准库导入。
#   ★ threading: s13 新增。Python 的线程模块。
#     threading.Thread(target=函数): 创建线程对象，target 是线程要跑的函数。
#     threading.Lock(): 创建互斥锁(防止多线程同时改数据出错)。
from pathlib import Path
from dataclasses import dataclass, asdict
# ↑ pathlib/dataclasses 详见 s02/s12 注释。

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
# ↑ 工作目录、记忆、客户端、模型(继承 s05/s10)。

# ── Task System (from s12, synced) ──
# ↓ 任务系统(继承 s12 完整复制)。逐行注释见 s12。这里是为后台任务提供任务管理基础。

TASKS_DIR = WORKDIR / ".tasks"
TASKS_DIR.mkdir(exist_ok=True)


@dataclass
class Task:
    # ↑ Task 数据类(继承 s12)。逐行注释见 s12 的 Task。
    id: str
    subject: str
    description: str
    status: str          # pending | in_progress | completed
    owner: str | None
    blockedBy: list[str]


def _task_path(task_id: str) -> Path:
    return TASKS_DIR / f"{task_id}.json"


def create_task(subject: str, description: str = "",
                blockedBy: list[str] | None = None) -> Task:
    task = Task(
        id=f"task_{int(time.time())}_{random.randint(0, 9999):04d}",
        subject=subject, description=description,
        status="pending", owner=None,
        blockedBy=blockedBy or [],
    )
    save_task(task)
    return task


def save_task(task: Task):
    _task_path(task.id).write_text(json.dumps(asdict(task), indent=2))


def load_task(task_id: str) -> Task:
    return Task(**json.loads(_task_path(task_id).read_text()))


def list_tasks() -> list[Task]:
    return [Task(**json.loads(p.read_text()))
            for p in sorted(TASKS_DIR.glob("task_*.json"))]


def get_task(task_id: str) -> str:
    """Return full task details as JSON."""
    task = load_task(task_id)
    return json.dumps(asdict(task), indent=2)


def can_start(task_id: str) -> bool:
    """Check if all blockedBy dependencies are completed.
    Missing dependencies are treated as blocked."""
    task = load_task(task_id)
    for dep_id in task.blockedBy:
        if not _task_path(dep_id).exists():
            return False
        if load_task(dep_id).status != "completed":
            return False
    return True


def claim_task(task_id: str, owner: str = "agent") -> str:
    task = load_task(task_id)
    if task.status != "pending":
        return f"Task {task_id} is {task.status}, cannot claim"
    if not can_start(task_id):
        deps = [d for d in task.blockedBy
                if not _task_path(d).exists() or load_task(d).status != "completed"]
        return f"Blocked by: {deps}"
    task.owner = owner
    task.status = "in_progress"
    save_task(task)
    print(f"  \033[36m[claim] {task.subject} → in_progress (owner: {owner})\033[0m")
    return f"Claimed {task.id} ({task.subject})"


def complete_task(task_id: str) -> str:
    task = load_task(task_id)
    if task.status != "in_progress":
        return f"Task {task_id} is {task.status}, cannot complete"
    task.status = "completed"
    save_task(task)
    unblocked = [t.subject for t in list_tasks()
                 if t.status == "pending" and t.blockedBy and can_start(t.id)]
    print(f"  \033[32m[complete] {task.subject} ✓\033[0m")
    msg = f"Completed {task.id} ({task.subject})"
    if unblocked:
        msg += f"\nUnblocked: {', '.join(unblocked)}"
        print(f"  \033[33m[unblocked] {', '.join(unblocked)}\033[0m")
    return msg


# ── Prompt Assembly (from s10, synced) ──

PROMPT_SECTIONS = {
    # ↑ 提示片段字典(继承 s10)。逐行注释见 s10。
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


def run_bash(command: str, run_in_background: bool = False) -> str:
    # ↑ ★ s13 改动: run_bash 多了个 run_in_background 参数。
    #   但这个参数【不在这里处理】——它在 agent_loop 的分发逻辑里判断(见 should_run_background)。
    #   这里只是为了让函数签名能接收这个参数(模型可能传它)。
    # run_in_background is handled by agent_loop dispatch, not here
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
# ↓ 5 个任务工具(继承 s12)。逐行注释见 s12 的对应函数。

def run_create_task(subject: str, description: str = "",
                    blockedBy: list[str] | None = None) -> str:
    task = create_task(subject, description, blockedBy)
    deps = f" (blockedBy: {', '.join(blockedBy)})" if blockedBy else ""
    print(f"  \033[34m[create] {task.subject}{deps}\033[0m")
    return f"Created {task.id}: {task.subject}{deps}"


def run_list_tasks() -> str:
    tasks = list_tasks()
    if not tasks:
        return "No tasks. Use create_task to add some."
    lines = []
    for t in tasks:
        icon = {"pending": "○", "in_progress": "●",
                "completed": "✓"}.get(t.status, "?")
        deps = f" (blockedBy: {', '.join(t.blockedBy)})" if t.blockedBy else ""
        owner = f" [{t.owner}]" if t.owner else ""
        lines.append(f"  {icon} {t.id}: {t.subject} "
                     f"[{t.status}]{owner}{deps}")
    return "\n".join(lines)


def run_get_task(task_id: str) -> str:
    try:
        return get_task(task_id)
    except FileNotFoundError:
        return f"Error: Task {task_id} not found"


def run_claim_task(task_id: str) -> str:
    return claim_task(task_id, owner="agent")


def run_complete_task(task_id: str) -> str:
    return complete_task(task_id)


TOOLS = [
    # ↑ 工具声明。bash 多了 run_in_background 参数(模型可显式要求后台)。
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object",
                      "properties": {
                          "command": {"type": "string"},
                          "run_in_background": {"type": "boolean"}},
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
    # ↑ 工具分发映射(继承 s12)。
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "create_task": run_create_task, "list_tasks": run_list_tasks,
    "get_task": run_get_task, "claim_task": run_claim_task,
    "complete_task": run_complete_task,
}


# ═══════════════════════════════════════════════════════════
#  Background Tasks (s13 new)
# ═══════════════════════════════════════════════════════════

_bg_counter = 0
# ★ s13 新增: 后台任务计数器(用于生成 bg_id)。
background_tasks: dict[str, dict] = {}   # bg_id → {tool_use_id, command, status}
# ↑ ★ 后台任务状态表: bg_id → 任务信息字典。
#   字典类型注解 dict[str, dict] = 键字符串值字典。
background_results: dict[str, str] = {}   # bg_id → output
# ↑ ★ 后台任务结果表: bg_id → 输出文本。完成后填这里。
background_lock = threading.Lock()
# ↑ ★★★ 互斥锁。防止多个后台线程同时改 background_tasks/results 字典导致数据错乱。
#   threading.Lock() 创建锁对象。用 with background_lock: 包住临界区(见下方)。


def is_slow_operation(tool_name: str, tool_input: dict) -> bool:
    # ★ s13 新增: 启发式判断操作是否"慢"(可能超30秒)。
    #   当模型没明确说 run_in_background 时，用这个兜底判断。
    """Fallback heuristic: commands likely to take > 30s."""
    if tool_name != "bash":
        return False
        # ↑ 只对 bash 命令做判断(其他工具一般都快)。
    cmd = tool_input.get("command", "").lower()
    slow_keywords = ["install", "build", "test", "deploy", "compile",
                     "docker build", "pip install", "npm install",
                     "cargo build", "pytest", "make"]
    # ↑ 慢操作关键词列表(install/build/test 等通常很慢)。
    return any(kw in cmd for kw in slow_keywords)
    # ↑ 命令含任一关键词就判定慢。any 详见 s01。


def should_run_background(tool_name: str, tool_input: dict) -> bool:
    # ★★★ s13 新增: 决定操作是否该后台执行。
    #   优先级: 模型显式要求 > 启发式判断。
    """Model explicit request takes priority; fallback to heuristic."""
    if tool_input.get("run_in_background"):
        return True
        # ↑ ★ 模型显式传 run_in_background=True → 一定后台(尊重模型意愿)。
    return is_slow_operation(tool_name, tool_input)
    # ↑ 否则用启发式判断(含 install/build 等关键词就后台)。


def execute_tool(block) -> str:
    # ★ s13 新增: 统一的工具执行函数(查表 + 调用)。
    #   把"查 TOOL_HANDLERS + 解包参数 + 调用"封装成一处，主循环和后台线程都能用。
    """Execute a tool call block, return output."""
    handler = TOOL_HANDLERS.get(block.name)
    if handler:
        return handler(**block.input)
    return f"Unknown tool: {block.name}"


def start_background_task(block) -> str:
    # ★★★ s13 核心: 在守护线程里异步执行工具。
    #   block: 工具调用块(含 name/input/id)。
    #   返回 bg_id(后台任务编号)。主循环不等待，立刻继续。
    """Run tool in a daemon thread. Returns background task ID."""
    global _bg_counter
    # ↑ 声明修改全局计数器。
    _bg_counter += 1
    bg_id = f"bg_{_bg_counter:04d}"
    # ↑ 生成 bg_id: "bg_" + 4位编号。:04d 详见 s12。
    cmd = block.input.get("command", block.name)
    # ↑ 记录命令文本(用于通知里展示)。没有 command 参数就用工具名。

    def worker():
        # ↑ ★ 内部函数: 线程要执行的实际工作。闭包捕获 bg_id 和 block。
        #   闭包: 内部函数引用外部函数的变量(bg_id/block)。这些变量在内部函数里"活着"。
        result = execute_tool(block)
        # ↑ 执行工具(可能很慢，但在后台线程不阻塞主循环)。
        with background_lock:
            # ↑ ★ with 锁: 进入临界区自动加锁，离开自动释放(即使出错)。
            #   防止多个后台线程同时改字典。
            background_tasks[bg_id]["status"] = "completed"
            background_results[bg_id] = result
            # ↑ 标记完成 + 存结果。collect_background_results 会取走。

    with background_lock:
        # ↑ 加锁注册任务(防止和 worker 同时改字典)。
        background_tasks[bg_id] = {
            "tool_use_id": block.id,
            "command": cmd,
            "status": "running",
        }
    thread = threading.Thread(target=worker, daemon=True)
    # ↑ ★ 创建线程对象。target=worker: 线程跑 worker 函数(注意无括号，是函数本身)。
    #   daemon=True: 守护线程——主程序退出时自动结束(不会卡住退出)。
    thread.start()
    # ↑ ★ 启动线程(开始并行执行 worker)。start 不阻塞，立即返回。
    print(f"  \033[33m[background] dispatched {bg_id}: {cmd[:40]}\033[0m")
    return bg_id


def collect_background_results() -> list[str]:
    # ★★★ s13 核心: 收集已完成的后台结果，转成通知消息。
    #   在每轮工具执行后调用，把完成的后台任务结果注入对话。
    """Collect completed background results as task_notification messages."""
    with background_lock:
        ready_ids = [bid for bid, task in background_tasks.items()
                     if task["status"] == "completed"]
        # ↑ 列表推导: 找出所有已完成的 bg_id。加锁防并发改。
    notifications = []
    for bg_id in ready_ids:
        with background_lock:
            task = background_tasks.pop(bg_id)
            # ↑ pop: 取出并删除(key 存在)。任务被取走(不重复通知)。
            output = background_results.pop(bg_id, "")
            # ↑ pop(bg_id, ""): 取结果，没有返回空串(防 KeyError)。
        summary = output[:200] if len(output) > 200 else output
        # ↑ 结果太长只取前200字符(通知是摘要，全文可让模型再查)。
        notifications.append(
            f"<task_notification>\n"
            f"  <task_id>{bg_id}</task_id>\n"
            f"  <status>completed</status>\n"
            f"  <command>{task['command']}</command>\n"
            f"  <summary>{summary}</summary>\n"
            f"</task_notification>")
        # ↑ ★ 用 XML 格式包装通知。<task_notification> 标签让模型识别"这是后台完成通知"。
        #   ★ 注意: 不复用原 tool_use_id——这是新通知，不是对原工具的回复。
        print(f"  \033[32m[background done] {bg_id}: "
              f"{task['command'][:40]} ({len(output)} chars)\033[0m")
    return notifications


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


# ── Agent Loop (simplified, focused on background tasks) ──

def agent_loop(messages: list, context: dict):
    # ★ s13 agent loop: 慢操作丢后台 + 通知注入。简化错误处理(无 s11 恢复)。
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
            return

        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            return

        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            print(f"\033[36m> {block.name}\033[0m")

            if should_run_background(block.name, block.input):
                # ↑ ★★★ s13 核心改动: 工具执行前判断是否后台。
                bg_id = start_background_task(block)
                # ↑ 丢后台线程执行(不等待)。
                results.append({"type": "tool_result",
                                "tool_use_id": block.id,
                                "content": f"[Background task {bg_id} started] "
                                           f"Command: {block.input.get('command', '')}. "
                                           f"Result will be available when complete."})
                # ↑ ★ 立即返回"已启动"占位结果(不复用原结果，因为还没跑完)。
                #   模型看到"后台已启动"，知道稍后会有通知。
            else:
                output = execute_tool(block)
                # ↑ 不后台 → 同步执行(等结果)。
                print(str(output)[:300])
                results.append({"type": "tool_result",
                                "tool_use_id": block.id,
                                "content": output})

        # Inject tool results + background notifications in one user message
        user_content = list(results)
        # ↑ 复制 results 列表(list(...) 详见 s08)。
        bg_notifications = collect_background_results()
        # ↑ ★ 收集已完成的后台结果(转通知)。
        if bg_notifications:
            for notif in bg_notifications:
                user_content.append({"type": "text", "text": notif})
                # ↑ 把通知作为 text 块加进 user 消息(和 tool_result 合并成一条)。
            print(f"  \033[32m[inject] {len(bg_notifications)} background "
                  f"notification(s)\033[0m")
        messages.append({"role": "user", "content": user_content})
        # ↑ ★ 工具结果 + 后台通知合并成一条 user 消息喂回(模型一次看到所有信息)。
        context = update_context(context, messages)
        system = get_system_prompt(context)


if __name__ == "__main__":
    print("s13: background tasks")
    print("Enter a question, press Enter to send. Type q to quit.\n")
    history = []
    context = update_context({}, [])
    while True:
        try:
            query = input("\033[36ms13 >> \033[0m")
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
        print()
# ↑ 入口主循环(继承 s12)。详见 s10 注释。
