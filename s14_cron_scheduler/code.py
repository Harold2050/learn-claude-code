#!/usr/bin/env python3
# ↑ shebang 行，详见 s01 注释。
"""
s14: Cron Scheduler — independent daemon thread + queue processor.

Run:  python s14_cron_scheduler/code.py
Need: pip install anthropic python-dotenv + .env with ANTHROPIC_API_KEY

Changes from s13:
  - CronJob dataclass (id, cron, prompt, recurring, durable)
  - cron_matches: 5-field cron expression matching with DOM/DOW OR semantics
  - schedule_job / cancel_job: register/remove cron jobs (with validation)
  - cron_scheduler_loop: independent daemon thread, polls every 1s
  - cron_queue: thread-safe queue, scheduler writes, queue processor delivers
  - queue_processor_loop: auto-runs agent_loop when cron_queue has work
  - Durable storage: .scheduled_tasks.json (survives restart)
  - 3 new tools: schedule_cron, list_crons, cancel_cron

Four layers:
  1. Scheduler: daemon thread checks time → fires matching jobs
  2. Queue: cron_queue decouples scheduler from agent loop
  3. Queue processor: wakes the agent when queued work exists and it is idle
  4. Consumer: agent_loop consumes queued jobs and injects them into messages
"""
# ↑ 模块 docstring，详见 s01 注释。
#   本文件核心: 定时任务(cron)。让 agent 能"每天9点跑测试"这类定时工作。
#   四层架构(这是 s14 的精髓):
#   1. 调度线程: 每秒检查时间，匹配的 job 放进队列。
#   2. 队列: 解耦调度和执行(生产者-消费者模式)。
#   3. 队列处理器: agent 空闲时从队列取 job 唤醒 agent。
#   4. 消费者: agent_loop 把 job 注入对话。

import os, subprocess, json, time, random, threading
# ↑ 标准库导入。threading 见 s13；time/random 见 s11；json 见 s09。
from pathlib import Path
from datetime import datetime
# ↑ ★ s14 新增: datetime 模块，处理日期时间。
#   datetime.now(): 返回当前本地时间的 datetime 对象(含年月日时分秒)。
#   dt.weekday(): 返回星期几(Monday=0...Sunday=6)。
#   dt.minute/hour/day/month: 各时间分量(整数)。
from dataclasses import dataclass, asdict
# ↑ dataclasses 详见 s12。

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
# ↓ 任务系统(继承 s12 完整复制)。逐行注释见 s12。

TASKS_DIR = WORKDIR / ".tasks"
TASKS_DIR.mkdir(exist_ok=True)


@dataclass
class Task:
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
    # ↑ 提示片段(继承 s10)。tools 段加了 cron 工具。
    "identity": "You are a coding agent. Act, don't explain.",
    "tools": "Available tools: bash, read_file, write_file, "
             "create_task, list_tasks, get_task, claim_task, complete_task, "
             "schedule_cron, list_crons, cancel_cron.",
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
    # ↑ 继承自 s13(run_in_background 由 agent_loop 处理)。逐行注释见 s13 的 run_bash。
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
# ↓ 5 个任务工具(继承 s12)。逐行注释见 s12。

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


# ── Background Tasks (from s13, synced) ──
# ↓ 后台任务系统(继承 s13 完整复制)。逐行注释见 s13。
#   execute_tool 里新增了 cron 工具的映射(见下方)。

_bg_counter = 0
background_tasks: dict[str, dict] = {}
background_results: dict[str, str] = {}
background_lock = threading.Lock()


def is_slow_operation(tool_name: str, tool_input: dict) -> bool:
    """Fallback heuristic: commands likely to take > 30s."""
    if tool_name != "bash":
        return False
    cmd = tool_input.get("command", "").lower()
    slow_keywords = ["install", "build", "test", "deploy", "compile",
                     "docker build", "pip install", "npm install",
                     "cargo build", "pytest", "make"]
    return any(kw in cmd for kw in slow_keywords)


def should_run_background(tool_name: str, tool_input: dict) -> bool:
    """Model explicit request takes priority; fallback to heuristic."""
    if tool_input.get("run_in_background"):
        return True
    return is_slow_operation(tool_name, tool_input)


def execute_tool(block) -> str:
    # ↑ 统一执行(继承 s13)。★ 注意这里 handlers 字典加了 cron 工具。
    """Execute a tool call block, return output."""
    handler = {
        "bash": run_bash, "read_file": run_read, "write_file": run_write,
        "create_task": run_create_task, "list_tasks": run_list_tasks,
        "get_task": run_get_task, "claim_task": run_claim_task,
        "complete_task": run_complete_task,
        "schedule_cron": run_schedule_cron, "list_crons": run_list_crons,
        "cancel_cron": run_cancel_cron,
        # ↑ ★ 新增 cron 工具映射(这些函数在后面定义，但 Python 运行时才调用，没问题)。
    }.get(block.name)
    if handler:
        return handler(**block.input)
    return f"Unknown tool: {block.name}"


def start_background_task(block) -> str:
    """Run tool in a daemon thread. Returns background task ID."""
    global _bg_counter
    _bg_counter += 1
    bg_id = f"bg_{_bg_counter:04d}"
    cmd = block.input.get("command", block.name)

    def worker():
        result = execute_tool(block)
        with background_lock:
            background_tasks[bg_id]["status"] = "completed"
            background_results[bg_id] = result

    with background_lock:
        background_tasks[bg_id] = {
            "tool_use_id": block.id,
            "command": cmd,
            "status": "running",
        }
    threading.Thread(target=worker, daemon=True).start()
    print(f"  \033[33m[background] dispatched {bg_id}: {cmd[:40]}\033[0m")
    return bg_id


def collect_background_results() -> list[str]:
    """Collect completed background results as task_notification messages."""
    with background_lock:
        ready_ids = [bid for bid, task in background_tasks.items()
                     if task["status"] == "completed"]
    notifications = []
    for bg_id in ready_ids:
        with background_lock:
            task = background_tasks.pop(bg_id)
            output = background_results.pop(bg_id, "")
        summary = output[:200] if len(output) > 200 else output
        notifications.append(
            f"<task_notification>\n"
            f"  <task_id>{bg_id}</task_id>\n"
            f"  <status>completed</status>\n"
            f"  <command>{task['command']}</command>\n"
            f"  <summary>{summary}</summary>\n"
            f"</task_notification>")
        print(f"  \033[32m[background done] {bg_id}: "
              f"{task['command'][:40]} ({len(output)} chars)\033[0m")
    return notifications


# ═══════════════════════════════════════════════════════════
#  Cron Scheduler (s14 new)
# ═══════════════════════════════════════════════════════════

DURABLE_PATH = WORKDIR / ".scheduled_tasks.json"
# ↑ ★ s14 新增: 持久化文件路径。durable 任务存这里，重启后自动加载。


@dataclass
class CronJob:
    # ★★★ s14 核心新增: cron 任务数据类。
    #   cron: Unix cron 语法，5字段 "分 时 日 月 周"。如 "0 9 * * *" = 每天9点。
    id: str
    cron: str        # "0 9 * * *"
    prompt: str      # message to inject when fired
    # ↑ 到点时要注入对话的提示文本(告诉 agent 该干什么)。
    recurring: bool  # True = recurring, False = one-shot
    # ↑ True=周期性(每天跑)；False=一次性(只跑一次就删)。
    durable: bool    # True = persist to disk
    # ↑ True=持久化(存盘，重启还在)；False=会话级(重启丢失)。


scheduled_jobs: dict[str, CronJob] = {}
# ↑ ★ 已注册的 cron 任务表: job_id → CronJob。
cron_queue: list[CronJob] = []
# ↑ ★★ 触发队列: 调度线程把到点的 job 放这里，agent_loop 取走执行。
#   这是"生产者-消费者"模式的缓冲区(解耦调度和执行)。
cron_lock = threading.Lock()
# ↑ cron 相关数据的锁(防并发改)。
agent_lock = threading.Lock()
# ↑ ★★ agent 执行锁: 防止多个 agent_loop 同时跑(用户输入和队列处理器可能并发触发)。
_last_fired: dict[str, str] = {}  # job_id → "YYYY-MM-DD HH:MM"
# ↑ ★ 记录每个 job 上次触发的时间标记(精确到分钟)。防止同一分钟内重复触发。


def _cron_field_matches(field: str, value: int) -> bool:
    # ★★★ s14 核心新增: 匹配单个 cron 字段。
    #   cron 字段语法: "*"=任意, "*/N"=每N, "1,3,5"=列表, "1-5"=范围, "9"=精确。
    """Match a single cron field against a value."""
    if field == "*":
        return True
        # ↑ "*" 匹配任意值。
    if field.startswith("*/"):
        # ↑ ★ "*/N": 每N个单位。startswith 前缀匹配。
        step = int(field[2:])
        # ↑ field[2:]: 去掉 "*/" 取步长部分。如 "*/15" → "15"。int 转整数。
        return step > 0 and value % step == 0
        # ↑ value % step == 0: value 能被 step 整除就匹配。
        #   如 "*/15" 匹配 0,15,30,45。step>0 防 "*/0"。
    if "," in field:
        # ↑ 列表 "1,3,5": 逗号分隔多个值。
        return any(_cron_field_matches(f.strip(), value)
                   for f in field.split(","))
        # ↑ 任一子字段匹配即可。递归调用自己处理每个子字段。.strip() 去空格。
    if "-" in field:
        # ↑ 范围 "1-5": 连字符表区间。
        lo, hi = field.split("-", 1)
        # ↑ split("-", 1): 只切第一个连字符。如 "1-5" → ["1","5"]。
        return int(lo) <= value <= int(hi)
        # ↑ value 在 [lo, hi] 区间内即匹配。链式比较: a <= x <= b(Python 特有)。
    return value == int(field)
    # ↑ 精确值: value == 整数。如 "9" 匹配 9。


def cron_matches(cron_expr: str, dt: datetime) -> bool:
    # ★★★ s14 核心: 检查 5 字段 cron 表达式是否匹配给定时间。
    """Check if a 5-field cron expression matches the given datetime.
    Standard cron semantics: DOM and DOW use OR when both are constrained."""
    fields = cron_expr.strip().split()
    # ↑ .strip().split(): 去首尾空白 + 按空白分割成列表。
    #   "0 9 * * *" → ["0","9","*","*","*"]。
    if len(fields) != 5:
        return False
        # ↑ 必须正好5字段(分 时 日 月 周)。
    minute, hour, dom, month, dow = fields
    # ↑ ★ 多重赋值: 5个变量一次性赋值。分别对应5个字段。
    dow_val = (dt.weekday() + 1) % 7  # Python Monday=0 → cron Sunday=0
    # ↑ ★ 星期转换: Python 的 weekday() 周一=0...周日=6。
    #   cron 的惯例 周日=0...周六=6。
    #   (dt.weekday()+1) % 7: 把 Python 的 6(周日) 变成 cron 的 0。
    #   % 是取模(求余)。如 7%7=0。

    m = _cron_field_matches(minute, dt.minute)
    h = _cron_field_matches(hour, dt.hour)
    dom_ok = _cron_field_matches(dom, dt.day)
    month_ok = _cron_field_matches(month, dt.month)
    dow_ok = _cron_field_matches(dow, dow_val)
    # ↑ 分别匹配5个字段。

    # Minute, hour, month must all match
    if not (m and h and month_ok):
        return False
        # ↑ 分/时/月 必须【全部】匹配(AND)。
    # DOM and DOW: if both constrained, either matching is enough (OR)
    dom_unconstrained = dom == "*"
    dow_unconstrained = dow == "*"
    # ↑ 判断日和周是否是"*"(未约束)。
    if dom_unconstrained and dow_unconstrained:
        return True
        # ★ 两个都没约束 → 直接匹配(其他字段已 AND 过)。
    if dom_unconstrained:
        return dow_ok
        # 日没约束 → 看周。
    if dow_unconstrained:
        return dom_ok
        # 周没约束 → 看日。
    return dom_ok or dow_ok
    # ★★★ 标准 cron 语义: 日和周都约束时用 OR(任一匹配即可)。
    #   这是 cron 的特殊规则，不是普通 AND。


def _validate_cron_field(field: str, lo: int, hi: int) -> str | None:
    # ★ s14 新增: 校验单个 cron 字段是否合法。返回错误信息或 None(合法)。
    """Validate a single cron field value is within [lo, hi]."""
    if field == "*":
        return None
    if field.startswith("*/"):
        step_str = field[2:]
        if not step_str.isdigit():
            return f"Invalid step: {field}"
        # ↑ ★ str.isdigit(): 字符串是否全由数字组成。防 "*/abc"。
        step = int(step_str)
        if step <= 0:
            return f"Step must be > 0: {field}"
        return None
    if "," in field:
        for part in field.split(","):
            err = _validate_cron_field(part.strip(), lo, hi)
            if err: return err
            # ↑ 递归校验列表每个子字段，任一错就返回错误。
        return None
    if "-" in field:
        parts = field.split("-", 1)
        if not parts[0].isdigit() or not parts[1].isdigit():
            return f"Invalid range: {field}"
        a, b = int(parts[0]), int(parts[1])
        if a < lo or a > hi or b < lo or b > hi:
            return f"Range {field} out of bounds [{lo}-{hi}]"
        if a > b:
            return f"Range start > end: {field}"
        return None
    if not field.isdigit():
        return f"Invalid field: {field}"
    val = int(field)
    if val < lo or val > hi:
        return f"Value {val} out of bounds [{lo}-{hi}]"
    return None


def validate_cron(cron_expr: str) -> str | None:
    # ★ s14 新增: 校验整个 cron 表达式(5字段 + 各自范围)。
    """Validate a cron expression. Returns error message or None."""
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return f"Expected 5 fields, got {len(fields)}"
    bounds = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
    # ↑ 各字段的合法范围: 分0-59/时0-23/日1-31/月1-12/周0-6。
    names = ["minute", "hour", "day-of-month", "month", "day-of-week"]
    for i, (field, (lo, hi), name) in enumerate(zip(fields, bounds, names)):
        # ↑ ★ zip(列表1, 列表2, 列表3): 把多个列表"拉链"合并，每次取各列表同位置元素。
        #   如 zip(["a","b"], [1,2]) → [("a",1), ("b",2)]。
        #   enumerate 加索引。这里同时拿: 索引i、字段、范围(lo,hi)、字段名。
        err = _validate_cron_field(field, lo, hi)
        if err:
            return f"{name}: {err}"
    return None


def save_durable_jobs():
    # ★ s14 新增: 把 durable 任务持久化到磁盘。
    """Persist durable jobs to .scheduled_tasks.json."""
    durable = [asdict(j) for j in scheduled_jobs.values() if j.durable]
    # ↑ 列表推导: 只存 durable=True 的任务，每个转字典(asdict 详见 s12)。
    DURABLE_PATH.write_text(json.dumps(durable, indent=2))


def load_durable_jobs():
    # ★ s14 新增: 启动时从磁盘加载 durable 任务。
    """Load durable jobs from disk on startup."""
    if not DURABLE_PATH.exists():
        return
    try:
        jobs = json.loads(DURABLE_PATH.read_text())
        for j in jobs:
            job = CronJob(**j)
            # ↑ 字典解包构造 CronJob(**字典 详见 s12)。
            err = validate_cron(job.cron)
            if err:
                print(f"  \033[31m[cron] skipping invalid job {job.id}: {err}\033[0m")
                continue
                # ↑ 校验失败的跳过(防脏数据)。
            scheduled_jobs[job.id] = job
        valid = [j for j in jobs if j["id"] in scheduled_jobs]
        if valid:
            print(f"  \033[35m[cron] loaded {len(valid)} durable job(s)\033[0m")
    except Exception:
        pass


def schedule_job(cron: str, prompt: str, recurring: bool = True,
                 durable: bool = True) -> CronJob | str:
    # ★★★ s14 核心: 注册新 cron 任务。返回 CronJob 或错误字符串。
    """Register a new cron job. Returns CronJob or error string."""
    err = validate_cron(cron)
    if err:
        return err
        # ↑ cron 表达式不合法 → 返回错误字符串。
    job = CronJob(
        id=f"cron_{random.randint(0, 999999):06d}",
        # ↑ 6位随机 ID。:06d 详见 s12。
        cron=cron, prompt=prompt,
        recurring=recurring, durable=durable,
    )
    with cron_lock:
        scheduled_jobs[job.id] = job
        # ↑ 加锁注册(防并发)。
    if durable:
        save_durable_jobs()
        # ↑ durable 任务存盘。
    print(f"  \033[35m[cron register] {job.id} '{cron}' → {prompt[:40]}\033[0m")
    return job


def cancel_job(job_id: str) -> str:
    # ★ s14 新增: 取消 cron 任务。
    """Cancel a cron job."""
    with cron_lock:
        job = scheduled_jobs.pop(job_id, None)
        # ↑ pop(key, None): 取出并删除，不存在返回 None(不报错)。
    if not job:
        return f"Job {job_id} not found"
    if job.durable:
        save_durable_jobs()
        # ↑ durable 任务还要更新存盘文件。
    print(f"  \033[31m[cron cancel] {job_id}\033[0m")
    return f"Cancelled {job_id}"


def cron_scheduler_loop():
    # ★★★★ s14 核心(第1层): 调度守护线程。每秒检查时间，到点的 job 放进队列。
    #   这是个无限循环(while True)，在独立线程跑(daemon)。
    """Independent daemon thread: poll every 1s, fire matching jobs.
    Individual job errors are caught to prevent one bad job from
    killing the entire scheduler thread."""
    while True:
        time.sleep(1)
        # ↑ 每秒检查一次。sleep(1) 阻塞1秒。
        now = datetime.now()
        # Date-aware marker prevents daily jobs from skipping on day 2+
        minute_marker = now.strftime("%Y-%m-%d %H:%M")
        # ↑ ★ 日期感知标记: 精确到分钟的时间字符串。如 "2024-01-15 09:30"。
        #   strftime: 格式化时间。%Y年 %m月 %d日 %H时 %M分。
        #   作用: 防止每日任务跨日重复触发(用日期+分钟当唯一键)。
        with cron_lock:
            for job in list(scheduled_jobs.values()):
                # ↑ list(...): 复制一份再遍历(防遍历时改字典报错)。
                try:
                    if cron_matches(job.cron, now):
                        # ↑ 时间匹配 cron 表达式。
                        if _last_fired.get(job.id) != minute_marker:
                            # ↑ ★ 同一分钟内不重复触发(_last_fired 记录上次触发)。
                            cron_queue.append(job)
                            # ↑ ★★ 放进队列(生产者)。agent_loop 会取走。
                            _last_fired[job.id] = minute_marker
                            print(f"  \033[35m[cron fire] {job.id} → "
                                  f"{job.prompt[:40]}\033[0m")
                        if not job.recurring:
                            scheduled_jobs.pop(job.id, None)
                            # ↑ 一次性任务触发后删除。
                            if job.durable:
                                save_durable_jobs()
                except Exception as e:
                    print(f"  \033[31m[cron error] {job.id}: {e}\033[0m")
                    # ↑ 单个 job 出错不影响其他 job(捕获异常继续)。


def consume_cron_queue() -> list[CronJob]:
    # ★★ s14 核心(第4层用): agent_loop 调这个取走队列里的 job。
    """Consume fired jobs from cron_queue (called by agent_loop)."""
    with cron_lock:
        fired = list(cron_queue)
        # ↑ 复制一份(快照)。
        cron_queue.clear()
        # ↑ 清空队列(已取出，防止重复消费)。
    return fired


def has_cron_queue() -> bool:
    # ★ s14 新增: 队列是否有待处理的 job(队列处理器用来判断要不要唤醒)。
    """Return whether fired cron jobs are waiting to be delivered."""
    with cron_lock:
        return bool(cron_queue)
        # ↑ bool(列表): 空列表→False，非空→True。


# Load durable jobs on startup, then start scheduler thread
load_durable_jobs()
# ↑ ★ 模块加载时立即读取持久化任务。
threading.Thread(target=cron_scheduler_loop, daemon=True).start()
# ↑ ★ 启动调度守护线程(第1层)。daemon=True: 主程序退出时自动结束。
print("  \033[35m[cron] scheduler thread started\033[0m")


# ── Cron Tools ──
# ↓ 3 个 cron 工具的处理函数。

def run_schedule_cron(cron: str, prompt: str,
                      recurring: bool = True, durable: bool = True) -> str:
    result = schedule_job(cron, prompt, recurring, durable)
    if isinstance(result, str):
        return f"Error: {result}"
        # ↑ schedule_job 返回字符串表示错误(成功返回 CronJob 对象)。
    return f"Scheduled {result.id}: '{cron}' → {prompt}"


def run_list_crons() -> str:
    with cron_lock:
        jobs = list(scheduled_jobs.values())
    if not jobs:
        return "No cron jobs. Use schedule_cron to add one."
    lines = []
    for j in jobs:
        tag = "recurring" if j.recurring else "one-shot"
        dur = "durable" if j.durable else "session"
        lines.append(f"  {j.id}: '{j.cron}' → {j.prompt[:40]} "
                     f"[{tag}, {dur}]")
    return "\n".join(lines)


def run_cancel_cron(job_id: str) -> str:
    return cancel_job(job_id)


# ── Tool Definitions ──

TOOLS = [
    # ↑ 工具声明(继承 s13 + 3个 cron 工具)。
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
    {"name": "schedule_cron",
     # ↑ ★ s14 新工具: 注册 cron 任务。
     "description": "Schedule a cron job. cron is 5-field: min hour dom month dow.",
     "input_schema": {"type": "object",
                      "properties": {
                          "cron": {"type": "string",
                                   "description": "5-field cron expression"},
                          "prompt": {"type": "string",
                                     "description": "Message to inject when fired"},
                          "recurring": {"type": "boolean",
                                        "description": "True=recurring, False=one-shot"},
                          "durable": {"type": "boolean",
                                      "description": "True=persist to disk"}},
                      "required": ["cron", "prompt"]}},
    {"name": "list_crons",
     "description": "List all registered cron jobs.",
     "input_schema": {"type": "object", "properties": {},
                      "required": []}},
    {"name": "cancel_cron",
     "description": "Cancel a cron job by ID.",
     "input_schema": {"type": "object",
                      "properties": {"job_id": {"type": "string"}},
                      "required": ["job_id"]}},
]


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
        "enabled_tools": [t["name"] for t in TOOLS],
        "workspace": str(WORKDIR),
        "memories": memories,
    }


# ── Agent Loop (simplified, focused on cron scheduler) ──

def agent_loop(messages: list, context: dict) -> dict:
    # ★ s14 agent loop(第4层 消费者): 每轮开头消费 cron 队列。
    #   返回 context(s14 改动: 返回 dict，便于外层传递)。
    system = get_system_prompt(context)
    while True:
        # Layer 4: consume fired cron jobs → inject as messages
        fired = consume_cron_queue()
        # ↑ ★★ 第4层: 取走队列里的 job(消费 cron_queue)。
        for job in fired:
            messages.append({"role": "user",
                             "content": f"[Scheduled] {job.prompt}"})
            # ↑ ★ 把 cron job 的 prompt 作为 user 消息注入([Scheduled] 前缀标识来源)。
            print(f"  \033[35m[inject cron] {job.prompt[:50]}\033[0m")

        try:
            response = client.messages.create(
                model=MODEL, system=system, messages=messages,
                tools=TOOLS, max_tokens=8000)
        except Exception as e:
            messages.append({"role": "assistant", "content": [
                {"type": "text",
                 "text": f"[Error] {type(e).__name__}: {e}"}]})
            return context

        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            return context

        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            print(f"\033[36m> {block.name}\033[0m")

            if should_run_background(block.name, block.input):
                bg_id = start_background_task(block)
                results.append({"type": "tool_result",
                                "tool_use_id": block.id,
                                "content": f"[Background task {bg_id} started] "
                                           f"Result will be available when complete."})
            else:
                output = execute_tool(block)
                print(str(output)[:300])
                results.append({"type": "tool_result",
                                "tool_use_id": block.id,
                                "content": output})
            # ↑ 工具执行(继承 s13，后台判断 + 查表执行)。

        # Merge background tool results + notifications into one user message
        user_content = list(results)
        bg_notifications = collect_background_results()
        if bg_notifications:
            for notif in bg_notifications:
                user_content.append({"type": "text", "text": notif})
        messages.append({"role": "user", "content": user_content})
        context = update_context(context, messages)
        system = get_system_prompt(context)


session_history: list = []
session_context = update_context({}, [])
# ↑ ★ s14 新增: 全局会话状态(history + context)。供 queue_processor 和 input 共用。


def print_latest_assistant_text(messages: list):
    """Print text blocks from the latest assistant message."""
    if not messages:
        return
    msg = messages[-1]
    if not isinstance(msg, dict) or msg.get("role") != "assistant":
        return
    content = msg.get("content", "")
    if isinstance(content, str):
        print(content)
        return
    for block in content:
        if getattr(block, "type", None) == "text":
            print(block.text)
        elif isinstance(block, dict) and block.get("type") == "text":
            print(block.get("text", ""))


def run_agent_turn_locked(user_query: str | None = None):
    # ★★★ s14 核心(第3层辅助): 跑一轮 agent turn。
    #   ★ 调用者必须先持有 agent_lock(防并发)。这个函数本身不锁。
    """Run one agent turn. Caller must hold agent_lock."""
    global session_context
    if user_query is not None:
        session_history.append({"role": "user", "content": user_query})
    session_context = agent_loop(session_history, session_context)
    session_context = update_context(session_context, session_history)
    print_latest_assistant_text(session_history)
    print()


def queue_processor_loop():
    # ★★★★ s14 核心(第3层): 队列处理器线程。
    #   agent 空闲时，如果队列有 job 就自动唤醒 agent 跑一轮。
    """Auto-deliver fired cron jobs when the agent is idle."""
    global session_context
    while True:
        time.sleep(0.2)
        # ↑ 每0.2秒检查一次(比调度线程频繁，及时响应)。
        if not has_cron_queue():
            continue
            # ↑ 队列空，跳过。
        if not agent_lock.acquire(blocking=False):
            continue
            # ↑ ★ acquire(blocking=False): 尝试加锁，不阻塞。
            #   锁被占(用户正在交互)→ 返回 False → 跳过(等下次)。
            #   不阻塞防止队列处理器卡死。
        try:
            if not has_cron_queue():
                continue
                # ★ 二次检查(拿锁后可能已被消费)，防重复触发。
            print("\n  \033[35m[queue processor] delivering scheduled work\033[0m")
            run_agent_turn_locked()
            # ↑ 不传 user_query(用队列里的 cron job 当输入)。
        finally:
            agent_lock.release()
            # ↑ finally: 无论是否出错都释放锁(防死锁)。


if __name__ == "__main__":
    print("s14: cron scheduler")
    print("Enter a question, press Enter to send. Type q to quit.\n")
    threading.Thread(target=queue_processor_loop, daemon=True).start()
    # ↑ ★ 启动队列处理器线程(第3层)。
    print("  \033[35m[queue processor] started\033[0m")
    while True:
        try:
            query = input("\033[36ms14 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        with agent_lock:
            # ↑ ★ 用户输入也加锁(和队列处理器互斥，防止同时跑两轮)。
            run_agent_turn_locked(query)
