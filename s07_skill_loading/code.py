#!/usr/bin/env python3
# ↑ shebang 行，详见 s01 注释。
"""
s07: Skill Loading — two-level on-demand knowledge injection.

  Layer 1 (cheap, always present):
    SYSTEM prompt includes skill names + one-line descriptions (~100 tokens/skill)
    "Skills available: agent-builder, code-review, mcp-builder, pdf"

  Layer 2 (expensive, on demand):
    Agent calls load_skill("code-review") → full SKILL.md content
    injected via tool_result (~2000 tokens/skill)

  skills/
    agent-builder/SKILL.md
    code-review/SKILL.md
    mcp-builder/SKILL.md
    pdf/SKILL.md

Changes from s06:
  + build_system() — scan skills/ dir at startup, inject catalog into SYSTEM
  + load_skill(name) — return full SKILL.md content via tool_result
  + SKILLS_DIR config
  Loop unchanged: load_skill auto-dispatches via TOOL_HANDLERS.

Run: python s07_skill_loading/code.py
Needs: pip install anthropic python-dotenv pyyaml + ANTHROPIC_API_KEY in .env
"""
# ↑ 模块 docstring，详见 s01 注释。
#   本文件核心: 两层按需知识。
#   L1(便宜): 启动时扫描 skills/ 目录，把每个技能的"名字+一句话描述"塞进 SYSTEM(~100 token/技能)。
#   L2(贵): 模型调 load_skill("名字") 才读全文(~2000 token/技能)，用完即弃。
#   原则: 便宜的先给(让模型知道有什么)，贵的按需取(只在真要用时加载)。

import ast, json, os, subprocess
from pathlib import Path
# ↑ 标准库导入，详见 s06 注释。
import yaml
# ↑ ★ s07 新增: 导入 PyYAML 库(第三方包，pip install pyyaml)。
#   YAML 是一种数据序列化格式(类似 JSON 但更人类友好，用缩进表示层级)。
#   技能文件的"元数据头"(frontmatter)用 YAML 写。yaml.safe_load 把 YAML 文本解析成 Python 字典。

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
SKILLS_DIR = WORKDIR / "skills"
# ↑ ★ s07 新增: 技能目录路径。WORKDIR/"skills" 拼出 .../skills。
#   这个目录下每个子目录是一个技能，里面放 SKILL.md。
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]
CURRENT_TODOS: list[dict] = []
# ↑ 客户端、模型、todo 状态(继承 s05)，详见 s05 注释。

# s07: Skill catalog scan (used by build_system below)
def _parse_frontmatter(text: str) -> tuple[dict, str]:
    # ★ s07 新增: 解析 Markdown 文件的 YAML frontmatter(元数据头)。
    #   技能文件 SKILL.md 格式:
    #     ---
    #     name: code-review
    #     description: Reviews code for issues
    #     ---
    #     (正文内容...)
    #   这个函数把头部的元数据(---之间)和正文分开。
    #   返回 (元数据字典, 正文)。tuple[dict, str] 表示返回"字典+字符串"的元组。
    """Parse YAML frontmatter from SKILL.md. Returns (meta, body)."""
    if not text.startswith("---"):
        # ↑ 如果文件不是以 --- 开头，说明没有 frontmatter。
        return {}, text
        # ↑ 返回空字典 + 原文(把全文当正文)。
    parts = text.split("---", 2)
    # ↑ split("---", 2): 用 --- 分割，最多分 2 刀(得 3 段)。
    #   "...\n---\n...\n---\n..." → ["开头空", "frontmatter", "正文"]
    #   第二个参数 2 是"最大分割次数"。
    if len(parts) < 3:
        # ↑ 不够 3 段(只有一个 --- 或没有)，格式不对。
        return {}, text
    try:
        meta = yaml.safe_load(parts[1]) or {}
        # ↑ ★ yaml.safe_load(字符串): 把 YAML 文本解析成 Python 字典。
        #   parts[1] 是 frontmatter 那段(如 "name: xxx\ndescription: yyy")。
        #   解析后: {"name": "xxx", "description": "yyy"}。
        #   or {}: 如果解析结果是 None(空内容)，用空字典代替。
        #   safe_load 比 load 安全(不会执行任意代码)。
    except yaml.YAMLError:
        # ↑ YAML 格式错误，用空字典兜底。
        meta = {}
    return meta, parts[2].strip()
    # ↑ 返回 (元数据字典, 正文)。.strip() 去掉正文首尾空白。

# Build skill registry at startup (used for safe lookup in load_skill)
SKILL_REGISTRY: dict[str, dict] = {}
# ↑ ★ s07 新增: 技能注册表。全局字典: 技能名 → {name, description, content}。
#   启动时扫描一次填好，之后 load_skill 只查这个表(不走路径遍历，防路径逃逸)。
#   dict[str, dict] 类型注解: 键是字符串，值是字典。

def _scan_skills():
    # ★ s07 新增: 扫描 skills/ 目录，把每个技能登记进 SKILL_REGISTRY。
    #   启动时调一次(见文件末尾 _scan_skills() 调用)。
    """Scan skills/ dir, populate SKILL_REGISTRY with name/description/content."""
    if not SKILLS_DIR.exists():
        # ↑ Path.exists(): 目录/文件是否存在。
        return
        # ↑ 没有 skills/ 目录就不扫描(不报错)。
    for d in sorted(SKILLS_DIR.iterdir()):
        # ↑ SKILLS_DIR.iterdir(): 列出目录里的所有条目(文件和子目录)。
        #   sorted(...): 排序(保证加载顺序稳定，不同系统遍历顺序可能不同)。
        #   d 是每个子目录的 Path 对象。
        if not d.is_dir():
            continue
            # ↑ 跳过非目录(如 skills/ 下的散落文件)。is_dir() 判断是否目录。
        manifest = d / "SKILL.md"
        # ↑ 每个技能目录下应该有个 SKILL.md 文件。d/"SKILL.md" 拼路径。
        if manifest.exists():
            # ↑ SKILL.md 存在才处理。
            raw = manifest.read_text()
            # ↑ 读 SKILL.md 全文。
            meta, body = _parse_frontmatter(raw)
            # ↑ 解析出元数据和正文。
            name = meta.get("name", d.name)
            # ↑ 技能名: 优先用 frontmatter 里的 name，没有就用目录名(d.name)。
            desc = meta.get("description", raw.split("\n")[0].lstrip("#").strip())
            # ↑ 技能描述: 优先用 frontmatter 的 description，没有就用正文第一行(去掉 # 前缀)。
            #   raw.split("\n")[0]: 第一行。.lstrip("#"): 去掉左边的 #。.strip(): 去首尾空格。
            SKILL_REGISTRY[name] = {"name": name, "description": desc, "content": raw}
            # ↑ 登记进注册表: 名字 → {name, description, content 全文}。

_scan_skills()
# ↑ ★ 启动时立即扫描技能目录(模块加载时就执行这行)。SKILL_REGISTRY 填好后供后续使用。

def list_skills() -> str:
    # ★ s07 新增: 列出所有技能的名字+描述(用于注入 SYSTEM)。
    """List all skills (name + one-line description)."""
    if not SKILL_REGISTRY:
        return "(no skills found)"
        # ↑ 没扫描到技能。
    return "\n".join(f"- **{s['name']}**: {s['description']}" for s in SKILL_REGISTRY.values())
    # ↑ SKILL_REGISTRY.values(): 取所有技能字典。
    #   生成器: 每个 s 格式化成 "- **名字**: 描述"。
    #   "\n".join(...): 用换行拼起来。这是 L1 的"目录"内容。

# s07: SYSTEM includes skill catalog (cheap — just names + descriptions)
def build_system() -> str:
    # ★ s07 新增: 组装系统提示词，把技能目录(L1)注入进去。
    #   技能只有"名字+描述"进 SYSTEM(便宜)，全文要模型调 load_skill 才加载(L2)。
    """Build SYSTEM prompt with skill catalog injected at startup."""
    catalog = list_skills()
    # ↑ 拿到技能目录文本。
    return (
        f"You are a coding agent at {WORKDIR}. "
        f"Skills available:\n{catalog}\n"
        "Use load_skill to get full details when needed."
    )
    # ↑ SYSTEM = 身份 + 技能目录 + "需要时用 load_skill 加载全文"的引导。

SYSTEM = build_system()
# ↑ ★ 在模块加载时就构建好 SYSTEM(技能目录扫描一次，注入)。
#   之后 agent_loop 用这个 SYSTEM 调 API。

# s07: subagent gets its own system prompt — no skill loading, no task
SUB_SYSTEM = (
    # ↑ 子代理系统提示词(继承 s06)。子代理不加载技能(简化)，详见 s06 注释。
    f"You are a coding agent at {WORKDIR}. "
    "Complete the task you were given, then return a concise summary. "
    "Do not delegate further."
)


# ═══════════════════════════════════════════════════════════
#  FROM s02-s06 (unchanged): Tool Implementations
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

def extract_text(content) -> str:
    # ↑ 继承自 s06，从消息内容提取纯文字。逐行注释见 s06 的 extract_text。
    if not isinstance(content, list):
        return str(content)
    return "\n".join(getattr(b, "text", "") for b in content if getattr(b, "type", None) == "text")


# ═══════════════════════════════════════════════════════════
#  FROM s06 (unchanged): Subagent
# ═══════════════════════════════════════════════════════════

SUB_TOOLS = [
    # ↑ 子代理工具列表(继承 s06，没有 task/todo_write)。逐行注释见 s06 的 SUB_TOOLS。
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
SUB_HANDLERS = {"bash": run_bash, "read_file": run_read, "write_file": run_write,
                "edit_file": run_edit, "glob": run_glob}
# ↑ 子代理工具分发(继承 s06)，详见 s06 注释。

def spawn_subagent(description: str) -> str:
    # ↑ 子代理派生(继承 s06)。逐行注释见 s06 的 spawn_subagent。
    print(f"\n\033[35m[Subagent spawned]\033[0m")
    messages = [{"role": "user", "content": description}]
    for _ in range(30):
        response = client.messages.create(model=MODEL, system=SUB_SYSTEM,
            messages=messages, tools=SUB_TOOLS, max_tokens=8000)
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            break
        results = []
        for block in response.content:
            if block.type == "tool_use":
                blocked = trigger_hooks("PreToolUse", block)
                if blocked:
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": str(blocked)})
                    continue
                handler = SUB_HANDLERS.get(block.name)
                output = handler(**block.input) if handler else f"Unknown: {block.name}"
                trigger_hooks("PostToolUse", block, output)
                print(f"  \033[90m[sub] {block.name}: {str(output)[:100]}\033[0m")
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})
        messages.append({"role": "user", "content": results})
    result = extract_text(messages[-1]["content"])
    if not result:
        for msg in reversed(messages):
            if msg["role"] == "assistant":
                result = extract_text(msg["content"])
                if result:
                    break
        if not result:
            result = "Subagent stopped after 30 turns without final answer."
    print(f"\033[35m[Subagent done]\033[0m")
    return result


# ═══════════════════════════════════════════════════════════
#  NEW in s07: load_skill — runtime full content loading
# ═══════════════════════════════════════════════════════════

def load_skill(name: str) -> str:
    # ★★★ s07 核心新增: 运行时加载技能全文(L2)。
    #   模型在 SYSTEM 里看到技能目录后，调 load_skill("code-review") 获取全文。
    #   全文通过 tool_result 注入对话(贵，~2000 token/技能)，用完即弃。
    """Load full skill content. Lookup via registry — no path traversal."""
    skill = SKILL_REGISTRY.get(name)
    # ↑ ★ 安全设计: 从注册表查(启动时扫描填好的)，不直接读文件路径。
    #   防止路径遍历攻击(模型传 "../../etc/passwd" 之类的名字读不到系统文件)。
    if not skill:
        return f"Skill not found: {name}"
        # ↑ 注册表里没有这个名字，返回错误。
    return skill["content"]
    # ↑ 返回 SKILL.md 全文(含 frontmatter)。模型看到全文后就能按技能指引工作。


# ═══════════════════════════════════════════════════════════
#  Tool Registry — all tools from s02-s07
# ═══════════════════════════════════════════════════════════

TOOLS = [
    # ↑ 主代理工具声明列表。包含 s02-s06 的所有工具 + s07 新增的 load_skill。
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
    {"name": "task", "description": "Launch a subagent to handle a complex subtask. Returns only the final conclusion.",
     "input_schema": {"type": "object", "properties": {"description": {"type": "string"}}, "required": ["description"]}},
    # s07: skill tool (catalog is already in SYSTEM prompt, this loads full content)
    {"name": "load_skill", "description": "Load the full content of a skill by name.",
     # ↑ ★ s07 新增工具: load_skill。模型用它加载技能全文。
     #   注意: 技能目录(名字+描述)已经在 SYSTEM 里了，这个工具只负责加载全文。
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
]

TOOL_HANDLERS = {
    # ↑ 主代理工具分发映射。新增 load_skill → load_skill 函数。
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob, "todo_write": run_todo_write,
    "task": spawn_subagent, "load_skill": load_skill,
}


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
# ↑ 黑名单(继承 s05)，详见 s05 注释。

def permission_hook(block):
    # ↑ PreToolUse 钩子(继承 s05)。逐行注释见 s05 的 permission_hook。
    if block.name == "bash":
        for p in DENY_LIST:
            if p in block.input.get("command", ""):
                print(f"\n\033[31m⛔ Blocked: '{p}'\033[0m")
                return "Permission denied"
    return None

def log_hook(block):
    # ↑ PreToolUse 钩子(继承 s05)。逐行注释见 s05 的 log_hook。
    print(f"\033[90m[HOOK] {block.name}\033[0m")
    return None

def context_inject_hook(query: str):
    # ↑ UserPromptSubmit 钩子(继承 s05)。逐行注释见 s05。
    print(f"\033[90m[HOOK] UserPromptSubmit: working in {WORKDIR}\033[0m")
    return None

def summary_hook(messages: list):
    # ↑ Stop 钩子(继承 s05)。逐行注释见 s05 的 summary_hook。
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
#  agent_loop — same as s05-s06 + nag reminder
# ═══════════════════════════════════════════════════════════

rounds_since_todo = 0
# ↑ todo nag 计数器(继承 s05)，详见 s05 注释。

def agent_loop(messages: list):
    # ↑ 主代理循环(继承 s05-s06)。load_skill 通过 TOOL_HANDLERS 自动派发，循环不变。
    global rounds_since_todo
    while True:
        if rounds_since_todo >= 3 and messages:
            messages.append({"role": "user",
                             "content": "<reminder>Update your todos.</reminder>"})
            rounds_since_todo = 0
        # ↑ todo nag(继承 s05)，详见 s05 注释。

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
            # ↑ Stop 钩子 + 退出(继承 s04/s05)，详见 s04 注释。

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
            # ↑ ★ 这里 block.name 可能是 "load_skill"。TOOL_HANDLERS["load_skill"] = load_skill 函数。
            output = handler(**block.input) if handler else f"Unknown: {block.name}"
            # ↑ 如果是 load_skill，这里调 load_skill(name=...)，返回技能全文。
            #   全文作为 tool_result 喂给模型(L2 加载完成)。

            trigger_hooks("PostToolUse", block, output)

            if block.name == "todo_write":
                rounds_since_todo = 0

            results.append({"type": "tool_result", "tool_use_id": block.id,
                            "content": output})

        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    print("s07: Skill Loading — catalog in SYSTEM, content on demand")
    print("Type a question, press Enter. Type q to quit.\n")

    history = []
    while True:
        try:
            query = input("\033[36ms07 >> \033[0m")
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
# ↑ 入口主循环(继承 s05/s06)，详见 s01 注释。
