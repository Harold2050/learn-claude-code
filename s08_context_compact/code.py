#!/usr/bin/env python3
# ↑ shebang 行，详见 s01 注释。
"""
s08_context_compact.py - Context Compact

Four-layer compaction pipeline inserted before LLM calls:

    L1: snip_compact      — trim middle messages when count > 50
    L2: micro_compact     — replace old tool_results with placeholders
    L3: tool_result_budget — persist large results to disk
    L4: compact_history   — LLM full summary (1 API call)

    Emergency: reactive_compact — when API still returns prompt_too_long

    ┌─────────────────────────────────────────────────────────────┐
    │  messages[]                                                 │
    │    ↓                                                        │
    │  L3 budget ─→ L1 snip ─→ L2 micro ─→ [token > threshold?]  │
    │                                      ├─ No  → LLM          │
    │                                      └─ Yes → L4 summary   │
    │                                              ↓              │
    │                                          LLM call           │
    │                                    [prompt_too_long?]        │
    │                                      └─ Yes → reactive      │
    └─────────────────────────────────────────────────────────────┘

Core principle: cheap first, expensive last.
Execution order matches CC source: budget → snip → micro → auto.

Builds on s07 (skill loading). Usage:

    python s08_context_compact/code.py
    Needs: pip install anthropic python-dotenv + ANTHROPIC_API_KEY in .env
"""
# ↑ 模块 docstring，详见 s01 注释。
#   本文件核心: 四层压缩管线 + 紧急压缩。
#   上下文(对话历史)会越积越长，超过模型的 token 上限就会报错。
#   压缩管线在每次调 LLM 前清理/缩减历史，原则: 便宜的先做，贵的最后做。
#   执行顺序: L3(落盘大结果)→L1(裁中间)→L2(旧结果占位)→L4(超限才 LLM 摘要)。
#   紧急: 万一还报 prompt_too_long，reactive_compact 再裁一次。

import ast, json, os, subprocess, time
# ↑ 标准库导入。time 是 s08 新增(用于给压缩存档文件起带时间戳的名字)。
#   time.time() 返回当前时间戳(1970年至今的秒数，浮点数)。
from pathlib import Path
# ↑ pathlib.Path，详见 s02 注释。

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
if os.getenv("ANTHROPIC_BASE_URL"): os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
# ↑ 加载 .env、兼容第三方端点，详见 s01 注释。

WORKDIR = Path.cwd()
SKILLS_DIR = WORKDIR / "skills"
TRANSCRIPT_DIR = WORKDIR / ".transcripts"
# ↑ ★ s08 新增: 压缩存档目录。L4 摘要前会把原始历史存成 JSONL 文件(留底，防止信息丢失)。
TOOL_RESULTS_DIR = WORKDIR / ".task_outputs" / "tool-results"
# ↑ ★ s08 新增: 大工具结果落盘目录。L3 把超大输出存到这里，对话里只留预览。
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]
CURRENT_TODOS: list[dict] = []
# ↑ 客户端、模型、todo 状态(继承 s05)，详见 s05 注释。

# s07: Skill catalog scan (inherited from s07)
def _parse_frontmatter(text: str) -> tuple[dict, str]:
    # ↑ 继承自 s07，解析 YAML frontmatter。逐行注释见 s07 的 _parse_frontmatter。
    #   注意: s08 这里改用"手动解析"(不依赖 yaml 库)，逐行 split(":",1)。
    #   原因: s08 不要求装 pyyaml，手动解析更轻量(教学简化)。
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    meta = {}
    for line in parts[1].strip().splitlines():
        # ↑ 遍历 frontmatter 每一行，手动解析 "key: value"。
        if ":" in line:
            k, v = line.split(":", 1)
            # ↑ split(":", 1): 只分割第一个冒号。如 "name: a:b" → ["name", " a:b"]。
            meta[k.strip()] = v.strip().strip('"').strip("'")
            # ↑ .strip(): 去空格。.strip('"'): 去双引号。.strip("'"): 去单引号。
            #   多次 strip 链式调用，处理 "value" 或 'value' 的引号。
    return meta, parts[2].strip()

SKILL_REGISTRY: dict[str, dict] = {}
# ↑ 技能注册表(继承 s07)，详见 s07 注释。

def _scan_skills():
    # ↑ 继承自 s07，扫描技能目录。逐行注释见 s07 的 _scan_skills。
    if not SKILLS_DIR.exists():
        return
    for d in sorted(SKILLS_DIR.iterdir()):
        if not d.is_dir():
            continue
        manifest = d / "SKILL.md"
        if manifest.exists():
            raw = manifest.read_text()
            meta, body = _parse_frontmatter(raw)
            name = meta.get("name", d.name)
            desc = meta.get("description", raw.split("\n")[0].lstrip("#").strip())
            SKILL_REGISTRY[name] = {"name": name, "description": desc, "content": raw}

_scan_skills()

def list_skills() -> str:
    # ↑ 继承自 s07，列出技能目录。逐行注释见 s07 的 list_skills。
    if not SKILL_REGISTRY:
        return "(no skills found)"
    return "\n".join(f"- **{s['name']}**: {s['description']}" for s in SKILL_REGISTRY.values())

def load_skill(name: str) -> str:
    # ↑ 继承自 s07，加载技能全文。逐行注释见 s07 的 load_skill。
    skill = SKILL_REGISTRY.get(name)
    if not skill:
        return f"Skill not found: {name}"
    return skill["content"]

# s08: SYSTEM includes skill catalog (inherited from s07 build_system)
def build_system() -> str:
    # ↑ 继承自 s07，组装系统提示词。逐行注释见 s07 的 build_system。
    catalog = list_skills()
    return (
        f"You are a coding agent at {WORKDIR}. "
        f"Skills available:\n{catalog}\n"
        "Use load_skill to get full details when needed."
    )

SYSTEM = build_system()

# s08: subagent gets its own system prompt — no compact, no skill loading
SUB_SYSTEM = (
    # ↑ 子代理系统提示词(继承 s06)。子代理不做压缩(它的历史本来就丢弃，没必要)。
    f"You are a coding agent at {WORKDIR}. "
    "Complete the task you were given, then return a concise summary. "
    "Do not delegate further."
)


# ═══════════════════════════════════════════════════════════
#  FROM s02-s07 (unchanged): Basic Tools
# ═══════════════════════════════════════════════════════════

def safe_path(p: str) -> Path:
    # ↑ 继承自 s02，路径安全校验。逐行注释见 s02 的 safe_path。
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR): raise ValueError(f"Path escapes workspace: {p}")
    return path

def run_bash(command: str) -> str:
    # ↑ 继承自 s01/s02，执行 shell 命令。逐行注释见 s02 的 run_bash。
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR, capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired: return "Error: Timeout (120s)"

def run_read(path: str, limit: int | None = None) -> str:
    # ↑ 继承自 s02，读文件。逐行注释见 s02 的 run_read。
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines): lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e: return f"Error: {e}"

def run_write(path: str, content: str) -> str:
    # ↑ 继承自 s02，写文件。逐行注释见 s02 的 run_write。
    try:
        file_path = safe_path(path); file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content); return f"Wrote {len(content)} bytes to {path}"
    except Exception as e: return f"Error: {e}"

def run_edit(path: str, old_text: str, new_text: str) -> str:
    # ↑ 继承自 s02，编辑文件。逐行注释见 s02 的 run_edit。
    try:
        file_path = safe_path(path)
        text = file_path.read_text()
        if old_text not in text: return f"Error: text not found in {path}"
        file_path.write_text(text.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e: return f"Error: {e}"

def run_glob(pattern: str) -> str:
    # ↑ 继承自 s02，通配符查文件。逐行注释见 s02 的 run_glob。
    import glob as g
    try:
        results = []
        for match in g.glob(pattern, root_dir=WORKDIR):
            if (WORKDIR / match).resolve().is_relative_to(WORKDIR):
                results.append(match)
        return "\n".join(results) if results else "(no matches)"
    except Exception as e: return f"Error: {e}"

def _normalize_todos(todos):
    # ↑ 继承自 s05，规范化 todo。逐行注释见 s05 的 _normalize_todos。
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
    # ↑ 继承自 s05，更新 todo。逐行注释见 s05 的 run_todo_write。
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
    # ↑ 继承自 s06，提取纯文字。逐行注释见 s06 的 extract_text。
    if not isinstance(content, list): return str(content)
    return "\n".join(getattr(b, "text", "") for b in content if getattr(b, "type", None) == "text")


# ═══════════════════════════════════════════════════════════
#  FROM s06-s07 (unchanged): Subagent
# ═══════════════════════════════════════════════════════════

SUB_TOOLS = [
    # ↑ 子代理工具列表(继承 s06)。逐行注释见 s06 的 SUB_TOOLS。
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
#  NEW in s08: Four-Layer Compaction Pipeline
# ═══════════════════════════════════════════════════════════

CONTEXT_LIMIT = 50000
# ↑ ★ 上下文大小阈值(字符数)。超过这个就触发 L4(LLM 摘要)。
#   注意: 这是"字符数"的粗略估计，不是精确 token 数(精确算 token 要额外开销)。
KEEP_RECENT = 3
# ↑ L2(micro_compact)保留最近几条 tool_result 不压缩。
PERSIST_THRESHOLD = 30000
# ↑ L3(落盘)的阈值: 单个工具结果超过 30000 字符才落盘。

def estimate_size(msgs): return len(str(msgs))
# ↑ ★ 估算消息列表的"大小"。str(msgs) 把整个列表转字符串，len() 取字符数。
#   粗略但够用(精确 token 计算太贵)。返回整数。

def _block_type(block):
    # ★ 辅助函数: 取一个内容块的 type，兼容"字典"和"对象"两种形态。
    #   消息里的块可能是:
    #   - 字典(如 {"type": "tool_result", ...})——我们自己造的 tool_result
    #   - 对象(如 Anthropic SDK 返回的 TextBlock/ToolUseBlock)——API 返回的
    #   这个函数统一取 type，不用关心是哪种。
    return block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
    # ↑ isinstance(block, dict): 是字典就用 block.get("type")。
    #   else: 是对象就用 getattr(block, "type", None)(详见 s04 的 getattr 说明)。


def _message_has_tool_use(msg):
    # ★ 辅助函数: 这条消息是不是"含 tool_use 块的 assistant 消息"。
    #   用于压缩时保护配对——不能把 tool_use 和它的 tool_result 拆散。
    if msg.get("role") != "assistant":
        return False
        # ↑ 只有 assistant 消息才可能含 tool_use。
    content = msg.get("content")
    if not isinstance(content, list):
        return False
        # ↑ content 不是列表(可能是字符串)就没有 tool_use。
    return any(_block_type(block) == "tool_use" for block in content)
    # ↑ any(...): 只要有一个块是 tool_use 类型就返回 True。any 详见 s01。


def _is_tool_result_message(msg):
    # ★ 辅助函数: 这条消息是不是"含 tool_result 块的 user 消息"。
    #   tool_result 是工具执行结果，作为 user 消息喂回(详见 s01 的 results 逻辑)。
    if msg.get("role") != "user":
        return False
    content = msg.get("content")
    if not isinstance(content, list):
        return False
    return any(isinstance(block, dict) and block.get("type") == "tool_result"
               for block in content)
    # ↑ 遍历 content，只要有字典且 type=="tool_result" 就返回 True。


# L1: snipCompact — trim middle messages
def snip_compact(messages, max_messages=50):
    # ★★★ L1 压缩: 消息数超过 50 条时，裁掉中间的(保留头3条+尾部)。
    #   为什么保留头尾? 头部通常是原始任务/重要指令，尾部是最近上下文，中间是已完成的细节。
    #   关键: 不能裁散 tool_use/tool_result 配对(API 要求它们必须成对出现)。
    if len(messages) <= max_messages: return messages
    # ↑ 没超过 50 条，不用压缩，原样返回。
    keep_head, keep_tail = 3, max_messages - 3
    # ↑ 头部保留 3 条，尾部保留 47 条(3+47=50)。
    head_end, tail_start = keep_head, len(messages) - keep_tail
    # ↑ head_end=3(头部结束位置，即裁剪起点)。
    #   tail_start=总数-47(尾部开始位置，即裁剪终点)。
    #   要裁掉的是 messages[head_end:tail_start] 这段中间区域。
    if head_end > 0 and _message_has_tool_use(messages[head_end - 1]):
        # ↑ ★ 配对保护1: 如果裁剪起点(head_end)前一条是 tool_use 消息，
        #   说明它的 tool_result 可能在裁剪区里——不能裁散，把起点往后移到配对结束。
        while head_end < len(messages) and _is_tool_result_message(messages[head_end]):
            head_end += 1
            # ↑ 持续后移，跳过所有紧跟的 tool_result 消息(把它们保留)。
    if (tail_start > 0 and tail_start < len(messages)
            and _is_tool_result_message(messages[tail_start])
            and _message_has_tool_use(messages[tail_start - 1])):
        # ↑ ★ 配对保护2: 如果裁剪终点(tail_start)是 tool_result，且它前一条是 tool_use，
        #   说明这对配对被裁剪线切开了——把终点往前移一位，把这对也保留。
        tail_start -= 1
    if head_end >= tail_start:
        return messages
        # ↑ 配对保护后，头尾重叠了(没东西可裁)，原样返回。
    snipped = tail_start - head_end
    # ↑ 被裁掉的消息数。
    return messages[:head_end] + [{"role": "user", "content": f"[snipped {snipped} messages]"}] + messages[tail_start:]
    # ↑ ★ 拼接结果: 头部 + 一条占位提示("[snipped N messages]") + 尽部。
    #   messages[:head_end]: 切片取头部(0 到 head_end)。
    #   messages[tail_start:]: 切片取尾部(tail_start 到末尾)。
    #   用 + 拼接三个列表成一个新列表。占位提示让模型知道"这里删了一些内容"。


# L2: microCompact — old result placeholders
def collect_tool_results(messages):
    # ★ 辅助函数: 收集所有 tool_result 块(及其位置)，供 micro_compact 用。
    blocks = []
    for mi, msg in enumerate(messages):
        # ↑ enumerate: 同时拿"消息索引 mi"和"消息 msg"。详见 s05。
        if msg.get("role") != "user" or not isinstance(msg.get("content"), list): continue
        # ↑ 只看 user 消息且 content 是列表的(tool_result 在 user 消息里)。
        for bi, block in enumerate(msg["content"]):
            # ↑ bi: 块在该消息 content 里的索引。
            if isinstance(block, dict) and block.get("type") == "tool_result":
                blocks.append((mi, bi, block))
                # ↑ 记录三元组: (消息索引, 块索引, 块对象)。
    return blocks

def micro_compact(messages):
    # ★★★ L2 压缩: 把"旧的"tool_result 内容替换成占位符(保留最近 3 条不动)。
    #   比删掉更安全——保留块结构(tool_use 和 tool_result 配对不散)，只清空内容省空间。
    tool_results = collect_tool_results(messages)
    if len(tool_results) <= KEEP_RECENT: return messages
    # ↑ tool_result 不超过 3 条，不用压缩。
    for _, _, block in tool_results[:-KEEP_RECENT]:
        # ↑ tool_results[:-KEEP_RECENT]: 切片，取"除了最后3条"的所有(即旧的)。
        #   :-3 表示从头到倒数第3条(不含)。
        #   解包 _: 消息索引(不用)，_: 块索引(不用)，block: 块对象。
        if len(block.get("content", "")) > 120:
            # ↑ 只有内容超过 120 字符的才替换(短的留着不亏)。
            block["content"] = "[Earlier tool result compacted. Re-run if needed.]"
            # ↑ ★ 替换成占位符。注意是原地修改 block(它是列表里的字典对象，改它就改了 messages)。
            #   "Re-run if needed" 告诉模型: 需要的话重新跑工具获取。
    return messages


# L3: toolResultBudget — persist large results to disk
def persist_large_output(tool_use_id, output):
    # ★ L3 辅助函数: 把超大工具输出存到磁盘，返回带预览的占位符。
    if len(output) <= PERSIST_THRESHOLD: return output
    # ↑ 没超过 30000 字符，不落盘，原样返回。
    TOOL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    # ↑ 确保落盘目录存在(不存在就建)。
    path = TOOL_RESULTS_DIR / f"{tool_use_id}.txt"
    # ↑ 用 tool_use_id 当文件名(唯一标识这次工具调用)。
    if not path.exists(): path.write_text(output)
    # ↑ 文件不存在才写(防重复写覆盖)。exists() 检查存在。
    return f"<persisted-output>\nFull output: {path}\nPreview:\n{output[:2000]}\n</persisted-output>"
    # ↑ ★ 返回占位符: 告诉模型"完整输出在哪个文件"，并给前 2000 字符预览。
    #   模型需要完整内容时可以 read_file(path) 读回来。

def tool_result_budget(messages, max_bytes=200_000):
    # ★★★ L3 压缩: 如果最后一条消息的 tool_result 总大小超预算，把最大的几个落盘。
    #   只看最后一条消息(本轮的工具结果)，因为这是最新、最大的。
    #   max_bytes=200_000: 下划线是数字分隔符(200000，Python 3.6+ 支持的语法糖，纯可读性)。
    last = messages[-1] if messages else None
    # ↑ 取最后一条消息(messages 非空时)。if messages else None 是三元表达式(详见 s01)。
    if not last or last.get("role") != "user" or not isinstance(last.get("content"), list): return messages
    # ↑ 三道关卡: 没消息/不是 user/不是列表 → 不处理。
    blocks = [(i, b) for i, b in enumerate(last["content"]) if isinstance(b, dict) and b.get("type") == "tool_result"]
    # ↑ 列表推导: 找出最后一条消息里所有 tool_result 块，记录(索引, 块)。
    total = sum(len(str(b.get("content", ""))) for _, b in blocks)
    # ↑ 算所有 tool_result 的总字符数。sum(生成器) 求和。len(str(...)) 取字符数。
    if total <= max_bytes: return messages
    # ↑ 没超预算，不处理。
    ranked = sorted(blocks, key=lambda p: len(str(p[1].get("content", ""))), reverse=True)
    # ↑ ★ 按 tool_result 大小排序(大的在前)。
    #   sorted(列表, key=排序依据, reverse=True 降序)。
    #   key=lambda p: ...: 对每个元素 p，按内容长度排序。
    #   reverse=True: 从大到小(先压缩最大的，最有效)。
    for _, block in ranked:
        # ↑ 从大到小遍历。
        if total <= max_bytes: break
        # ↑ 已经压到预算内了，停。
        content = str(block.get("content", ""))
        if len(content) <= PERSIST_THRESHOLD: continue
        # ↑ 太小的不落盘(不值得，保留原样)。
        tid = block.get("tool_use_id", "unknown")
        block["content"] = persist_large_output(tid, content)
        # ↑ 落盘，content 换成占位符。
        total = sum(len(str(b.get("content", ""))) for _, b in blocks)
        # ↑ 重新算总大小(因为 content 变小了)。
    return messages


# L4: autoCompact — LLM full summary
def write_transcript(messages):
    # ★ L4 辅助函数: 把当前完整历史存成 JSONL 文件(留底)。
    #   JSONL = JSON Lines，每行一个 JSON 对象。防止压缩丢失信息(可回溯)。
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    # ↑ 文件名带时间戳(int(time.time()) 转整数)，如 transcript_1700000000.jsonl。
    with path.open("w") as f:
        # ↑ with open(...) as f: 上下文管理器，自动关闭文件。详见下方。
        #   path.open("w"): 以写模式打开("w"=write)。
        for msg in messages: f.write(json.dumps(msg, default=str) + "\n")
        # ↑ 每条消息转 JSON 写一行。
        #   json.dumps(msg, default=str): 把 msg 序列化成 JSON 文本。
        #     default=str: 遇到无法序列化的对象(如 SDK 的 TextBlock)就转字符串。
        #     不加 default 会报错(那些对象不是基本类型)。
        #   + "\n": 末尾加换行(JSONL 格式要求)。
    return path

def summarize_history(messages):
    # ★ L4 辅助函数: 让 LLM 把历史总结成摘要(花 1 次 API 调用，但能大幅缩短)。
    conversation = json.dumps(messages, default=str)[:80000]
    # ↑ 把整个历史转 JSON 文本，截断到 80000 字符(防止太长)。
    prompt = ("Summarize this coding-agent conversation so work can continue.\n"
              "Preserve: 1. current goal, 2. key findings/decisions, 3. files read/changed, "
              "4. remaining work, 5. user constraints.\nBe compact but concrete.\n\n" + conversation)
    # ↑ 指示模型总结哪些要点(目标/发现/文件/剩余工作/约束)。
    response = client.messages.create(model=MODEL, messages=[{"role": "user", "content": prompt}], max_tokens=2000)
    # ↑ 单独调一次 LLM 做摘要(不带工具，max_tokens 限 2000)。
    return "\n".join(
        getattr(block, "text", "")
        for block in response.content
        if getattr(block, "type", None) == "text").strip() or "(empty summary)"
    # ↑ 提取摘要文字。.strip() 去首尾空白。or "(empty summary)": 空就用兜底文案。

def compact_history(messages):
    # ★★★ L4 压缩: 存档 + LLM 摘要，返回全新的(只有摘要的)历史。
    transcript_path = write_transcript(messages)
    # ↑ 先存档(防丢失)。
    print(f"[transcript saved: {transcript_path}]")
    summary = summarize_history(messages)
    # ↑ 让 LLM 总结。
    return [{"role": "user", "content": f"[Compacted]\n\n{summary}"}]
    # ↑ ★ 返回全新历史: 只有一条 user 消息，内容是 [Compacted] + 摘要。
    #   原来的几十条消息被压缩成 1 条。模型从摘要继续工作。


# Emergency: reactiveCompact — on API error
def reactive_compact(messages):
    # ★★★ 紧急压缩: API 报 prompt_too_long 时调用(防线)。
    #   和 compact_history 区别: 保留最近 5 条(不全部摘要)，因为可能正在工具调用中途。
    transcript = write_transcript(messages)
    # ↑ 先存档。
    tail_start = max(0, len(messages) - 5)
    # ↑ 保留最后 5 条的起点。max(0, ...): 防负数(消息少于5条时取0)。
    if (tail_start > 0 and tail_start < len(messages)
            and _is_tool_result_message(messages[tail_start])
            and _message_has_tool_use(messages[tail_start - 1])):
        # ↑ 配对保护: 保留区的第一条是 tool_result 且前一条是 tool_use → 往前多留一条。
        tail_start -= 1
    summary = summarize_history(messages[:tail_start])
    # ↑ 只总结"要被丢弃的部分"(保留区不总结)。
    return [{"role": "user", "content": f"[Reactive compact]\n\n{summary}"}, *messages[tail_start:]]
    # ↑ ★ 返回: [摘要消息] + [保留的最近5条]。
    #   *messages[tail_start:]: 星号"解包"列表——把列表元素展开成单独参数。
    #   [..., *list] 会把 list 的元素逐个加进新列表。这是 Python 3.5+ 的"可迭代解包"。


# ═══════════════════════════════════════════════════════════
#  FROM s07: Tool Definitions
# ═══════════════════════════════════════════════════════════

TOOLS = [
    # ↑ 工具声明列表(继承 s07 的 8 个)。逐项注释见 s07 的 TOOLS。
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
    {"name": "load_skill", "description": "Load the full content of a skill by name.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    # s08 change: new compact tool — triggers compact_history, not a no-op
    {"name": "compact", "description": "Summarize earlier conversation to free context space.",
     # ↑ ★ s08 新增工具: compact。模型可以主动调它来压缩历史(不只能等自动触发)。
     "input_schema": {"type": "object", "properties": {"focus": {"type": "string"}}}},
]

TOOL_HANDLERS = {
    # ↑ 工具分发映射(继承 s07)。注意: compact 没在这里登记——它在 agent_loop 里特殊处理(见下)。
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob, "todo_write": run_todo_write,
    "task": spawn_subagent, "load_skill": load_skill,
}

# FROM s04 (unchanged): Hooks
HOOKS = {"PreToolUse": [], "PostToolUse": []}
# ↑ 钩子注册表(s08 精简版，只留 PreToolUse/PostToolUse)。详见 s04 注释。
def trigger_hooks(event, *args):
    for cb in HOOKS[event]:
        r = cb(*args)
        if r is not None: return r
    return None
# ↑ 触发钩子(继承 s04)，详见 s04 注释。

DENY_LIST = ["rm -rf /", "sudo", "shutdown"]
def permission_hook(block):
    if block.name == "bash":
        for p in DENY_LIST:
            if p in block.input.get("command", ""): return "Permission denied"
    return None
def log_hook(block):
    print(f"\033[90m[HOOK] {block.name}\033[0m")
    return None
# ↑ 黑名单钩子 + 日志钩子(继承 s04/s05 精简版)。详见 s04 注释。

HOOKS["PreToolUse"].append(permission_hook)
HOOKS["PreToolUse"].append(log_hook)
# ↑ 直接往 HOOKS 字典的列表里 append(等价于 register_hook，s08 简化写法)。


# ═══════════════════════════════════════════════════════════
#  agent_loop — s08 core: run compaction pipeline before LLM
# ═══════════════════════════════════════════════════════════

MAX_REACTIVE_RETRIES = 1  # retry limit for reactive compact
# ↑ 紧急压缩最多重试 1 次(防死循环: 压了还报错→压了还报错)。

def agent_loop(messages: list):
    # ★★★ s08 核心 agent loop: 在调 LLM 前跑压缩管线 + API 报错紧急压缩。
    reactive_retries = 0
    # ↑ 记录紧急压缩已重试几次。
    while True:
        # s08 change: three preprocessors (0 API calls, cheap first)
        # Order matches CC source: budget → snip → micro
        messages[:] = tool_result_budget(messages)    # L3: persist large results first
        messages[:] = snip_compact(messages)          # L1: trim middle
        messages[:] = micro_compact(messages)         # L2: old result placeholders
        # ↑ ★★★ messages[:] = ... 是"切片赋值"——原地替换列表内容(保持引用不变)。
        #   和 messages = ... 的区别:
        #     messages = func(messages): 让局部变量指向新列表(调用方的原列表不变)。
        #     messages[:] = func(messages): 把新内容写回原列表(调用方能看到变化)。
        #   这里用 messages[:] 是因为 agent_loop 外部(main 的 history)也持有这个列表引用，
        #   要让压缩结果反映到 history 上。
        #   顺序: L3→L1→L2(便宜的先做)。这三步都不调 API(0 成本)。

        # s08 change: tokens still over threshold → LLM summary (1 API call)
        if estimate_size(messages) > CONTEXT_LIMIT:
            print("[auto compact]")
            messages[:] = compact_history(messages)
            # ↑ ★ 三层压缩后仍超 50000 字符 → L4: 花 1 次 API 调用做 LLM 摘要。
            #   摘要后历史变成 1 条，肯定不超了。

        try:
            response = client.messages.create(model=MODEL, system=SYSTEM, messages=messages, tools=TOOLS, max_tokens=8000)
            reactive_retries = 0  # reset on successful API call
            # ↑ API 调用成功，重置紧急压缩计数。
        except Exception as e:
            # ↑ 捕获所有异常。as e: 把异常对象存到 e。
            if ("prompt_too_long" in str(e).lower() or "too many tokens" in str(e).lower()) and reactive_retries < MAX_REACTIVE_RETRIES:
                # ↑ ★ 模糊匹配错误信息: 把异常转字符串(str(e))→转小写(.lower())→检查是否含 "prompt_too_long"。
                #   这是最紧急情况: 压缩管线没防住，API 还是说太长。
                #   且重试次数没超上限(<1)才处理。
                print("[reactive compact]")
                messages[:] = reactive_compact(messages)
                # ↑ 紧急压缩: 保留最近5条 + 摘要其余。
                reactive_retries += 1
                continue
                # ↑ 跳过本轮后续，回到 while 顶部重新调 API。
            raise
            # ↑ 不是 prompt_too_long 或重试超限 → 重新抛出异常(re-raise，让程序崩溃/上层处理)。

        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use": return
        # ↑ 存历史 + 不调工具就结束，详见 s01 注释。

        results = []
        for block in response.content:
            if block.type != "tool_use": continue
            print(f"\033[36m> {block.name}\033[0m")

            # s08: compact tool triggers compact_history, not a no-op string
            if block.name == "compact":
                # ↑ ★ s08 特殊处理: compact 工具不在 TOOL_HANDLERS 里(没登记)，
                #   在这里手动拦截——调 compact_history 真正压缩。
                messages[:] = compact_history(messages)
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": "[Compacted. Conversation history has been summarized.]"})
                messages.append({"role": "user", "content": results})
                break  # end current turn, start fresh with compacted context
                # ↑ ★ break 跳出 for 循环。这里 break 会触发下方的 for-else 的 else 不执行。

            blocked = trigger_hooks("PreToolUse", block)
            if blocked:
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(blocked)})
                continue
            handler = TOOL_HANDLERS.get(block.name)
            output = handler(**block.input) if handler else f"Unknown: {block.name}"
            trigger_hooks("PostToolUse", block, output)
            print(str(output)[:200])
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)})
        else:
            # normal path: no compact was called
            # ↑ ★ Python 的 for-else 语法: else 块在 for 循环【正常结束】(没被 break)时执行。
            #   上面 compact 分支会 break → 跳过这个 else。
            #   正常工具执行完(没 break) → 走 else，把 results 喂回去继续循环。
            messages.append({"role": "user", "content": results})
            continue
        # compact was called: results already appended above
        continue
        # ↑ compact 被调用时(break 出来了)，到这里也 continue 回 while 顶部(用压缩后的历史重新调 API)。


if __name__ == "__main__":
    print("s08: Context Compact — four-layer compaction pipeline")
    print("输入问题，回车发送。输入 q 退出。\n")
    history = []
    while True:
        try: query = input("\033[36ms08 >> \033[0m")
        except (EOFError, KeyboardInterrupt): break
        if query.strip().lower() in ("q", "exit", ""): break
        history.append({"role": "user", "content": query})
        agent_loop(history)
        for block in history[-1]["content"]:
            if getattr(block, "type", None) == "text": print(block.text)
        print()
# ↑ 入口主循环(继承 s07，去掉了 UserPromptSubmit 钩子触发——s08 精简了钩子系统)。详见 s01 注释。
