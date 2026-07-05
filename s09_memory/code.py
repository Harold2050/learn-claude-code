#!/usr/bin/env python3
# ↑ shebang 行，详见 s01 注释。
"""
s09_memory.py - Memory System

Persistent, cross-session knowledge for the coding agent.

Storage:
    .memory/
      MEMORY.md          ← index (one line per memory, ≤200 lines)
      feedback_tabs.md    ← individual memory files (Markdown + YAML frontmatter)
      user_profile.md
      project_facts.md

Flow in agent_loop:
    1. Load MEMORY.md index into SYSTEM prompt (cheap, always present)
    2. Select relevant memories by filename/description → inject content
    3. Run compression pipeline from s08
    4. After each turn ends → extract new memories from original messages
    5. Periodically consolidate (Dream)

Builds on s08 (context compact). Usage:

    python s09_memory/code.py
    Needs: pip install anthropic python-dotenv + ANTHROPIC_API_KEY in .env
"""
# ↑ 模块 docstring，详见 s01 注释。
#   本文件核心: 跨会话持久记忆。
#   前面的 agent 重启就失忆(历史只在内存)。s09 把重要信息写到 .memory/ 目录，
#   下次启动还在。三个子系统:
#   1. 选(select): 每轮按相关度挑记忆注入对话
#   2. 抽(extract): 每轮结束从对话提取新记忆落盘
#   3. 整(consolidate): 记忆太多时合并去重(Dream)
#   索引(MEMORY.md)进 SYSTEM(便宜)，全文按需注入。

import os, subprocess, json, time, re
# ↑ 标准库导入。re 是 s09 新增(正则表达式库)。
#   re.search(模式, 字符串): 在字符串里找匹配模式的部分，返回匹配对象或 None。
#   这里用于从 LLM 回复里提取 JSON 数组(模型可能把 JSON 包在文字里)。
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
MEMORY_DIR = WORKDIR / ".memory"; MEMORY_DIR.mkdir(exist_ok=True)
# ↑ ★ s09 新增: 记忆目录 .memory/。mkdir(exist_ok=True): 不存在就建，已存在不报错。
#   分号 ; 把两条语句放一行(等价于两行)。这里建目录 + 赋值。
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"
# ↑ ★ 记忆索引文件: 每行一条记忆的"名字+描述"，注入 SYSTEM(便宜)。模型看了知道有哪些记忆。
SKILLS_DIR = WORKDIR / "skills"
TRANSCRIPT_DIR = WORKDIR / ".transcripts"
TOOL_RESULTS_DIR = WORKDIR / ".task_outputs" / "tool-results"
# ↑ 技能/存档/落盘目录(继承 s07/s08)，详见对应注释。
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]
# ↑ 客户端、模型(继承 s05)，详见 s05 注释。


# ═══════════════════════════════════════════════════════════
#  NEW in s09: Memory System
# ═══════════════════════════════════════════════════════════

MEMORY_TYPES = ["user", "feedback", "project", "reference"]
# ↑ ★ 记忆分四类:
#   user(用户偏好，如"喜欢用 tabs")、
#   feedback(反馈指导，如"别加注释")、
#   project(项目事实，如"用 React 19")、
#   reference(外部引用，如文档链接)。

def _parse_frontmatter(text: str) -> tuple[dict, str]:
    # ↑ 继承自 s08 的手动解析版(不依赖 yaml)。逐行注释见 s08 的 _parse_frontmatter。
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    meta = {}
    for line in parts[1].strip().splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip().strip('"').strip("'")
    return meta, parts[2].strip()


def write_memory_file(name: str, mem_type: str, description: str, body: str):
    # ★ s09 新增: 写一个记忆文件。每个记忆是独立的 .md 文件(含 YAML frontmatter)。
    """Write a single memory file with YAML frontmatter."""
    slug = name.lower().replace(" ", "-").replace("/", "-")
    # ↑ ★ slugify: 把名字转成文件名安全的格式。
    #   .lower() 转小写。.replace(" ", "-"): 空格换连字符。.replace("/", "-"): 斜杠换连字符。
    #   如 "User Preference Tabs" → "user-preference-tabs"。
    filename = f"{slug}.md"
    filepath = MEMORY_DIR / filename
    filepath.write_text(
        f"---\nname: {name}\ndescription: {description}\ntype: {mem_type}\n---\n\n{body}\n"
    )
    # ↑ 写文件: frontmatter(name/description/type) + 正文(body)。
    _rebuild_index()
    # ↑ ★ 写完文件要重建索引(把新记忆加进 MEMORY.md)。
    return filepath


def _rebuild_index():
    # ★ s09 新增: 重建 MEMORY.md 索引(扫描所有记忆文件，汇总成一行一条)。
    """Rebuild MEMORY.md index from all memory files."""
    lines = []
    for f in sorted(MEMORY_DIR.glob("*.md")):
        # ↑ MEMORY_DIR.glob("*.md"): 找目录下所有 .md 文件(Path 的 glob 方法)。
        #   sorted(...): 排序(稳定顺序)。
        if f.name == "MEMORY.md":
            continue
            # ↑ 跳过索引文件本身(它不是记忆)。
        raw = f.read_text()
        meta, body = _parse_frontmatter(raw)
        name = meta.get("name", f.stem)
        # ↑ f.stem: Path 的属性，文件名去掉扩展名。如 "user_profile.md" → "user_profile"。
        desc = meta.get("description", body.split("\n")[0][:80])
        # ↑ 描述: 优先 frontmatter，没有就取正文第一行(截80字符)。
        lines.append(f"- [{name}]({f.name}) — {desc}")
        # ↑ 一行一条: "- [名字](文件名) — 描述"。Markdown 链接格式。
    MEMORY_INDEX.write_text("\n".join(lines) + "\n" if lines else "")
    # ↑ 写入索引文件。如果有内容，每行用换行连接 + 末尾换行；没内容写空。


def read_memory_index() -> str:
    # ★ s09 新增: 读索引(注入 SYSTEM 用)。每轮调 build_system 时用。
    """Read MEMORY.md index (injected into SYSTEM every turn)."""
    if not MEMORY_INDEX.exists():
        return ""
    text = MEMORY_INDEX.read_text().strip()
    return text if text else ""
    # ↑ 存在且非空就返回内容，否则返回空串。.strip() 去首尾空白。


def read_memory_file(filename: str) -> str | None:
    # ★ s09 新增: 读单个记忆文件全文。返回 None 表示不存在。
    """Read a single memory file's full content."""
    path = MEMORY_DIR / filename
    if not path.exists():
        return None
    return path.read_text()


def list_memory_files() -> list[dict]:
    # ★ s09 新增: 列出所有记忆的元数据+正文(供 select/extract/consolidate 用)。
    """List all memory files with metadata."""
    result = []
    for f in sorted(MEMORY_DIR.glob("*.md")):
        if f.name == "MEMORY.md":
            continue
        raw = f.read_text()
        meta, body = _parse_frontmatter(raw)
        result.append({
            "filename": f.name,
            "name": meta.get("name", f.stem),
            "description": meta.get("description", ""),
            "type": meta.get("type", "user"),
            "body": body,
        })
        # ↑ 每个记忆打包成字典(filename/name/description/type/body)。
    return result


def select_relevant_memories(messages: list, max_items: int = 5) -> list[str]:
    # ★★★ s09 核心新增(选): 按相关度挑选记忆。
    #   先试 LLM 选择(把记忆目录给模型，让它挑相关的)，失败则降级为关键词匹配。
    """Select relevant memory filenames by matching recent conversation against
    memory names/descriptions. Uses a simple LLM call (or falls back to keyword
    matching on name+description)."""
    files = list_memory_files()
    if not files:
        return []
        # ↑ 没记忆可选。

    # Collect recent user text for context
    recent_texts = []
    for msg in reversed(messages):
        # ↑ reversed: 从最新往回找(最近的用户输入最相关)。
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, list):
                # ↑ content 可能是块列表(含 tool_result)，提取其中的文字。
                content = " ".join(
                    str(getattr(b, "text", "")) for b in content
                    if getattr(b, "type", None) == "text"
                )
            if isinstance(content, str):
                recent_texts.append(content)
            if len(recent_texts) >= 3:
                break
            # ↑ 最多取最近 3 条用户输入。
    recent = " ".join(reversed(recent_texts))[:2000]
    # ↑ 把取到的(从旧到新)拼起来，截到 2000 字符。reversed 再反转回正序。

    if not recent.strip():
        return []
        # ↑ 没有用户文字可分析。

    # Build catalog of name + description for LLM to choose from
    catalog_lines = []
    for i, f in enumerate(files):
        catalog_lines.append(f"{i}: {f['name']} — {f['description']}")
    catalog = "\n".join(catalog_lines)
    # ↑ 给 LLM 看的"记忆菜单": 编号 + 名字 + 描述。模型按编号挑。

    prompt = (
        "Given the recent conversation and the memory catalog below, "
        "select the indices of memories that are clearly relevant. "
        "Return ONLY a JSON array of integers, e.g. [0, 3]. "
        "If none are relevant, return [].\n\n"
        f"Recent conversation:\n{recent}\n\n"
        f"Memory catalog:\n{catalog}"
    )
    # ↑ 指示模型返回 JSON 数组(如 [0, 3]，表示选第0和第3条记忆)。

    try:
        response = client.messages.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200,
        )
        text = extract_text(response.content).strip()
        # Extract JSON array from response
        match = re.search(r'\[.*?\]', text, re.DOTALL)
        # ↑ ★ re.search(模式, 文本, 标志): 在 text 里找第一个匹配 模式 的地方。
        #   \[.*?\]: 匹配 "[...]" 这种 JSON 数组(贪婪用 ? 非贪婪，匹配最近的 ])。
        #     \[ 匹配左方括号([是特殊字符，要转义)。.*? 匹配任意字符(非贪婪)。\] 匹配右方括号。
        #   re.DOTALL: 标志，让 . 也匹配换行符(数组可能跨行)。
        #   返回 match 对象(找到)或 None(没找到)。
        if match:
            indices = json.loads(match.group())
            # ↑ match.group(): 返回匹配到的整个文本(如 "[0, 3]")。
            #   json.loads: 把 JSON 文本解析成 Python 列表(如 [0, 3])。
            selected = []
            for idx in indices:
                if isinstance(idx, int) and 0 <= idx < len(files):
                    # ↑ 校验: idx 是整数且在合法范围(防模型乱给编号)。
                    selected.append(files[idx]["filename"])
                    if len(selected) >= max_items:
                        break
                    # ↑ 最多选 5 条。
            return selected
    except Exception:
        pass
        # ↑ LLM 选择失败(网络/格式错)，降级到下面的关键词匹配。

    # Fallback: keyword matching on name + description
    keywords = [w.lower() for w in recent.split() if len(w) > 3]
    # ↑ ★ 降级方案: 关键词匹配。
    #   把最近对话拆成词，只保留长度>3的(去掉 the/a 等短词)，转小写。
    selected = []
    for f in files:
        text = (f["name"] + " " + f["description"]).lower()
        if any(kw in text for kw in keywords):
            # ↑ 记忆的 name+description 里只要含任一关键词就选中。
            selected.append(f["filename"])
            if len(selected) >= max_items:
                break
    return selected


def load_memories(messages: list) -> str:
    # ★★★ s09 新增: 加载相关记忆内容，组装成注入对话的文本块。
    """Load relevant memory content for injection into context."""
    selected_files = select_relevant_memories(messages)
    if not selected_files:
        return ""

    parts = ["<relevant_memories>"]
    # ↑ 用 XML 标签包裹，让模型清楚这是"注入的记忆"而非用户输入。
    for filename in selected_files:
        content = read_memory_file(filename)
        if content:
            parts.append(content)
    parts.append("</relevant_memories>")
    return "\n\n".join(parts)


def extract_memories(messages: list):
    # ★★★ s09 核心新增(抽): 每轮结束后从对话提取新记忆。
    #   注意: 用【压缩前快照】(在 agent_loop 里保存的)，保证提取的信息完整(压缩会丢细节)。
    """Extract new memories from recent dialogue. Runs after each turn."""
    # Collect recent conversation text
    dialogue_parts = []
    for msg in messages[-10:]:
        # ↑ 只看最近 10 条(够新，不会太老)。
        role = msg.get("role", "?")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                str(getattr(b, "text", "")) for b in content
                if getattr(b, "type", None) == "text"
            )
        if isinstance(content, str) and content.strip():
            dialogue_parts.append(f"{role}: {content}")
    dialogue = "\n".join(dialogue_parts)

    if not dialogue.strip():
        return

    # Check existing memories to avoid duplicates
    existing = list_memory_files()
    existing_desc = "\n".join(f"- {m['name']}: {m['description']}" for m in existing) if existing else "(none)"
    # ↑ 把已有记忆的描述列出来给模型，让它别重复提取。

    prompt = (
        "Extract user preferences, constraints, or project facts from this dialogue.\n"
        "Return a JSON array. Each item: {name, type, description, body}.\n"
        "- name: short kebab-case identifier (e.g. 'user-preference-tabs')\n"
        "- type: one of 'user' (user preference), 'feedback' (guidance), "
        "'project' (project fact), 'reference' (external pointer)\n"
        "- description: one-line summary for index lookup\n"
        "- body: full detail in markdown\n"
        "If nothing new or already covered by existing memories, return [].\n\n"
        f"Existing memories:\n{existing_desc}\n\n"
        f"Dialogue:\n{dialogue[:4000]}"
    )

    try:
        response = client.messages.create(
            model=MODEL, messages=[{"role": "user", "content": prompt}], max_tokens=800
        )
        text = extract_text(response.content).strip()
        # Extract JSON array from response
        match = re.search(r'\[.*\]', text, re.DOTALL)
        # ↑ 注意这里用 \[.*\](贪婪)，和 select 的 \[.*?\](非贪婪)不同。
        #   贪婪匹配到【最后一个】]——因为提取的 JSON 可能嵌套数组，非贪婪会只取第一个 ]。
        if not match:
            return
        items = json.loads(match.group())
        if not items:
            return
        count = 0
        for mem in items:
            name = mem.get("name", f"memory_{int(time.time())}")
            # ↑ 模型没给名字就用时间戳兜底(防重名)。
            mem_type = mem.get("type", "user")
            desc = mem.get("description", "")
            body = mem.get("body", "")
            if desc and body:
                write_memory_file(name, mem_type, desc, body)
                count += 1
        if count:
            print(f"\n\033[33m[Memory: extracted {count} new memories]\033[0m")
    except Exception:
        pass


CONSOLIDATE_THRESHOLD = 10

def consolidate_memories():
    # ★★★ s09 新增(整): 记忆太多(≥10)时合并去重(Dream)。
    #   让 LLM 把所有记忆融合，去重/删旧/保留重要的。
    """Merge duplicate/stale memories. Triggered when file count ≥ threshold."""
    files = list_memory_files()
    if len(files) < CONSOLIDATE_THRESHOLD:
        return
        # ↑ 不到 10 个，不用合并。

    catalog = "\n\n".join(
        f"## {f['filename']}\nname: {f['name']}\ndescription: {f['description']}\n{f['body']}"
        for f in files
    )

    prompt = (
        "Consolidate the following memory files. Rules:\n"
        "1. Merge duplicates into one\n"
        "2. Remove outdated/contradicted memories\n"
        "3. Keep the total under 30 memories\n"
        "4. Preserve important user preferences above all\n"
        "Return a JSON array. Each item: {name, type, description, body}.\n\n"
        f"{catalog[:16000]}"
    )

    try:
        response = client.messages.create(
            model=MODEL, messages=[{"role": "user", "content": prompt}], max_tokens=3000
        )
        text = extract_text(response.content).strip()
        match = re.search(r'\[.*\]', text, re.DOTALL)
        if not match:
            return
        items = json.loads(match.group())

        # Remove old memory files (keep MEMORY.md)
        for f in MEMORY_DIR.glob("*.md"):
            if f.name != "MEMORY.md":
                f.unlink()
                # ↑ ★ f.unlink(): 删除文件(Path 方法)。把旧记忆全删，下面重写合并后的。

        for mem in items:
            name = mem.get("name", f"memory_{int(time.time())}")
            mem_type = mem.get("type", "user")
            desc = mem.get("description", "")
            body = mem.get("body", "")
            if desc and body:
                write_memory_file(name, mem_type, desc, body)

        print(f"\n\033[33m[Memory: consolidated {len(files)} → {len(items)} memories]\033[0m")
    except Exception:
        pass


# Build SYSTEM with memory index
def build_system() -> str:
    # ★ s09 新增: 组装 SYSTEM，注入记忆索引(便宜，只名字+描述)。
    index = read_memory_index()
    memories_section = f"\n\nMemories available:\n{index}" if index else ""
    # ↑ 有索引就加"Memories available"段，没有就空。
    return (
        f"You are a coding agent at {WORKDIR}."
        f"{memories_section}\n"
        "Relevant memories are injected below. Respect user preferences from memory.\n"
        "When the user says 'remember' or expresses a clear preference, extract it as a memory."
    )

SUB_SYSTEM = (
    # ↑ 子代理系统提示词(继承 s06/s08)。详见 s06 注释。
    f"You are a coding agent at {WORKDIR}. "
    "Complete the task you were given, then return a concise summary. "
    "Do not delegate further."
)


# ═══════════════════════════════════════════════════════════
#  FROM s02-s08 (skeleton): Basic tools
# ═══════════════════════════════════════════════════════════

def safe_path(p: str) -> Path:
    # ↑ 继承自 s02，路径安全校验。逐行注释见 s02 的 safe_path。
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR): raise ValueError(f"Path escapes workspace: {p}")
    return path

def run_bash(command: str) -> str:
    # ↑ 继承自 s01/s02。逐行注释见 s02 的 run_bash。
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR, capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired: return "Error: Timeout (120s)"

def run_read(path: str, limit: int | None = None) -> str:
    # ↑ 继承自 s02。逐行注释见 s02 的 run_read。
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines): lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e: return f"Error: {e}"

def run_write(path: str, content: str) -> str:
    # ↑ 继承自 s02。逐行注释见 s02 的 run_write。
    try:
        file_path = safe_path(path); file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content); return f"Wrote {len(content)} bytes to {path}"
    except Exception as e: return f"Error: {e}"

def run_edit(path: str, old_text: str, new_text: str) -> str:
    # ↑ 继承自 s02。逐行注释见 s02 的 run_edit。
    try:
        file_path = safe_path(path)
        text = file_path.read_text()
        if old_text not in text: return f"Error: text not found in {path}"
        file_path.write_text(text.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e: return f"Error: {e}"

def run_glob(pattern: str) -> str:
    # ↑ 继承自 s02。逐行注释见 s02 的 run_glob。
    import glob as g
    try:
        results = []
        for match in g.glob(pattern, root_dir=WORKDIR):
            if (WORKDIR / match).resolve().is_relative_to(WORKDIR):
                results.append(match)
        return "\n".join(results) if results else "(no matches)"
    except Exception as e: return f"Error: {e}"

def extract_text(content) -> str:
    # ↑ 继承自 s06，提取纯文字。逐行注释见 s06 的 extract_text。
    if not isinstance(content, list): return str(content)
    return "\n".join(getattr(b, "text", "") for b in content if getattr(b, "type", None) == "text")

# Subagent (simplified from s06-s07)
SUB_TOOLS = [
    # ↑ 子代理工具(s09 精简到 3 个，聚焦记忆)。逐行注释见 s06 的 SUB_TOOLS。
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to a file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
]
SUB_HANDLERS = {"bash": run_bash, "read_file": run_read, "write_file": run_write}

def spawn_subagent(description: str) -> str:
    # ↑ 子代理派生(继承 s06，s09 简化版)。逐行注释见 s06 的 spawn_subagent。
    print(f"\n\033[35m[Subagent spawned]\033[0m")
    messages = [{"role": "user", "content": description}]
    for _ in range(30):
        response = client.messages.create(model=MODEL, system=SUB_SYSTEM,
            messages=messages, tools=SUB_TOOLS, max_tokens=8000)
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use": break
        results = []
        for block in response.content:
            if block.type == "tool_use":
                handler = SUB_HANDLERS.get(block.name)
                output = handler(**block.input) if handler else f"Unknown: {block.name}"
                print(f"  \033[90m[sub] {block.name}: {str(output)[:100]}\033[0m")
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})
        messages.append({"role": "user", "content": results})
    result = extract_text(messages[-1]["content"])
    if not result:
        for msg in reversed(messages):
            if msg["role"] == "assistant":
                result = extract_text(msg["content"])
                if result: break
        if not result: result = "Subagent stopped after 30 turns without final answer."
    print(f"\033[35m[Subagent done]\033[0m")
    return result


# ═══════════════════════════════════════════════════════════
#  FROM s08 (skeleton): Compaction pipeline
# ═══════════════════════════════════════════════════════════

CONTEXT_LIMIT = 50000; KEEP_RECENT = 3; PERSIST_THRESHOLD = 30000
# ↑ 压缩阈值(继承 s08)，详见 s08 注释。

def estimate_size(msgs): return len(str(msgs))
# ↑ 估算大小(继承 s08)，详见 s08 注释。

def _block_type(block):
    # ↑ 兼容 dict/对象取 type(继承 s08)。逐行注释见 s08 的 _block_type。
    return block.get("type") if isinstance(block, dict) else getattr(block, "type", None)

def _message_has_tool_use(msg):
    # ↑ 判断消息是否含 tool_use(继承 s08)。逐行注释见 s08。
    if msg.get("role") != "assistant":
        return False
    content = msg.get("content")
    if not isinstance(content, list):
        return False
    return any(_block_type(block) == "tool_use" for block in content)

def _is_tool_result_message(msg):
    # ↑ 判断消息是否是 tool_result(继承 s08)。逐行注释见 s08。
    if msg.get("role") != "user":
        return False
    content = msg.get("content")
    if not isinstance(content, list):
        return False
    return any(isinstance(block, dict) and block.get("type") == "tool_result" for block in content)

def snip_compact(msgs, mx=50):
    # ★ L1 压缩(继承 s08)。逐行注释见 s08 的 snip_compact。
    #   ★ 注意: 这里参数名是 mx=，不是 s08 的 max_messages=。
    #   这是有意为之——测试 test_compaction_tool_pairs.py 靠这个差异分别驱动 s08 和 s09 两套实现。
    #   不要统一这两个参数名！(详见 AGENTS.md "看似 bug 的简化")
    if len(msgs) <= mx: return msgs
    head_end, tail_start = 3, len(msgs) - (mx - 3)
    if head_end > 0 and _message_has_tool_use(msgs[head_end - 1]):
        while head_end < len(msgs) and _is_tool_result_message(msgs[head_end]):
            head_end += 1
    if (tail_start > 0 and tail_start < len(msgs)
            and _is_tool_result_message(msgs[tail_start])
            and _message_has_tool_use(msgs[tail_start - 1])):
        tail_start -= 1
    if head_end >= tail_start:
        return msgs
    return msgs[:head_end] + [{"role": "user", "content": f"[snipped {tail_start - head_end} msgs]"}] + msgs[tail_start:]

def collect_tool_results(msgs):
    # ↑ 收集 tool_result(继承 s08)。逐行注释见 s08 的 collect_tool_results。
    blocks = []
    for mi, msg in enumerate(msgs):
        if msg.get("role") != "user" or not isinstance(msg.get("content"), list): continue
        for bi, block in enumerate(msg["content"]):
            if isinstance(block, dict) and block.get("type") == "tool_result": blocks.append((mi, bi, block))
    return blocks

def micro_compact(msgs):
    # ★ L2 压缩(继承 s08)。逐行注释见 s08 的 micro_compact。
    tr = collect_tool_results(msgs)
    if len(tr) <= KEEP_RECENT: return msgs
    for _, _, b in tr[:-KEEP_RECENT]:
        if len(b.get("content", "")) > 120: b["content"] = "[Earlier tool result compacted.]"
    return msgs

def persist_large(tid, out):
    # ↑ L3 落盘(继承 s08)。逐行注释见 s08 的 persist_large_output。
    if len(out) <= PERSIST_THRESHOLD: return out
    TOOL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    p = TOOL_RESULTS_DIR / f"{tid}.txt"
    if not p.exists(): p.write_text(out)
    return f"<persisted-output>\nFull: {p}\nPreview:\n{out[:2000]}\n</persisted-output>"

def tool_result_budget(msgs, mx=200_000):
    # ★ L3 压缩(继承 s08)。逐行注释见 s08 的 tool_result_budget。
    last = msgs[-1] if msgs else None
    if not last or last.get("role") != "user" or not isinstance(last.get("content"), list): return msgs
    blocks = [(i, b) for i, b in enumerate(last["content"]) if isinstance(b, dict) and b.get("type") == "tool_result"]
    total = sum(len(str(b.get("content", ""))) for _, b in blocks)
    if total <= mx: return msgs
    for _, block in sorted(blocks, key=lambda p: len(str(p[1].get("content", ""))), reverse=True):
        if total <= mx: break
        c = str(block.get("content", ""))
        if len(c) <= PERSIST_THRESHOLD: continue
        block["content"] = persist_large(block.get("tool_use_id", "?"), c)
        total = sum(len(str(b.get("content", ""))) for _, b in blocks)
    return msgs

def write_transcript(msgs):
    # ↑ 存档(继承 s08)。逐行注释见 s08 的 write_transcript。
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    p = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    with p.open("w") as f:
        for m in msgs: f.write(json.dumps(m, default=str) + "\n")
    return p

def summarize_history(msgs):
    # ↑ LLM 摘要(继承 s08)。逐行注释见 s08 的 summarize_history。
    conv = json.dumps(msgs, default=str)[:80000]
    r = client.messages.create(model=MODEL, messages=[{"role": "user", "content":
        "Summarize this coding-agent conversation so work can continue.\n"
        "Preserve: 1. current goal, 2. key findings, 3. files changed, 4. remaining work, 5. user constraints.\n\n" + conv}],
        max_tokens=2000)
    return extract_text(r.content).strip()

def compact_history(msgs):
    # ★ L4 压缩(继承 s08)。逐行注释见 s08 的 compact_history。
    write_transcript(msgs)
    summary = summarize_history(msgs)
    return [{"role": "user", "content": f"[Compacted]\n\n{summary}"}]

def reactive_compact(msgs):
    # ★ 紧急压缩(继承 s08)。逐行注释见 s08 的 reactive_compact。
    write_transcript(msgs)
    tail_start = max(0, len(msgs) - 5)
    if (tail_start > 0 and tail_start < len(msgs)
            and _is_tool_result_message(msgs[tail_start])
            and _message_has_tool_use(msgs[tail_start - 1])):
        tail_start -= 1
    summary = summarize_history(msgs[:tail_start])
    return [{"role": "user", "content": f"[Reactive compact]\n\n{summary}"}, *msgs[tail_start:]]


# ═══════════════════════════════════════════════════════════
#  Tool Definitions (skeleton — fewer tools to focus on memory)
# ═══════════════════════════════════════════════════════════

TOOLS = [
    # ↑ 工具声明(s09 精简版，聚焦记忆)。逐项注释见 s02/s05/s06。
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
    {"name": "task", "description": "Launch a subagent to handle a subtask.",
     "input_schema": {"type": "object", "properties": {"description": {"type": "string"}}, "required": ["description"]}},
]

TOOL_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob, "task": spawn_subagent,
}


# ═══════════════════════════════════════════════════════════
#  agent_loop — s09: inject memories + extract after each turn
# ═══════════════════════════════════════════════════════════

MAX_REACTIVE_RETRIES = 1

def agent_loop(messages: list):
    # ★★★ s09 agent loop: 在 s08 基础上加记忆注入 + 每轮提取记忆。
    reactive_retries = 0
    # s09: inject relevant memory content into the current user turn
    memories_content = load_memories(messages)
    # ↑ ★ 循环开始前先选并加载相关记忆(选子系统)。
    memory_turn = len(messages) - 1 if messages and isinstance(messages[-1].get("content"), str) else None
    # ↑ ★ 记录"要注入记忆的回合号"——就是当前最后一条(刚输入的用户消息)。
    #   条件: 消息非空 且 最后一条 content 是字符串(纯文本，不是工具结果列表)。
    #   None 表示不注入(没有合适的回合)。
    # s09: build system once per user turn; memory is updated after the loop returns
    system = build_system()
    # ↑ 组装 SYSTEM(含记忆索引)。每次进入 agent_loop 调一次。

    while True:
        # s09: save pre-compression snapshot for accurate memory extraction
        pre_compress = [m if isinstance(m, dict) else {"role": m.get("role",""),
            "content": str(m.get("content",""))} for m in messages]
        # ↑ ★★★ 列表推导: 在压缩前给 messages 拍快照。
        #   为什么? 压缩会改 messages(裁剪/占位)。提取记忆要原始信息(保真)。
        #   m if isinstance(m, dict): 字典消息原样保留。
        #   else {...}: 非字典(如 SDK 对象)转成字典(role + content 转字符串)。
        #   这样 pre_compress 是纯字典列表(可安全用于记忆提取)。

        # s08: compression pipeline (budget → snip → micro)
        messages[:] = tool_result_budget(messages)
        messages[:] = snip_compact(messages)
        messages[:] = micro_compact(messages)
        # ↑ 压缩三层(继承 s08)。messages[:] 原地替换，详见 s08 注释。

        if estimate_size(messages) > CONTEXT_LIMIT:
            print("[auto compact]")
            messages[:] = compact_history(messages)
            # ↑ 超 50000 字符 → L4 摘要(继承 s08)。

        try:
            request_messages = messages
            # ↑ 默认: 直接用 messages 调 API。
            if memories_content and memory_turn is not None and memory_turn < len(messages):
                # ↑ ★ 如果有记忆内容 且 有合法注入回合。
                request_messages = messages.copy()
                # ↑ 先复制一份(不改原 messages——记忆注入只在 API 请求时，不污染真实历史)。
                request_messages[memory_turn] = {
                    **messages[memory_turn],
                    "content": memories_content + "\n\n" + messages[memory_turn]["content"],
                }
                # ↑ ★★★ 这三行是 s09 精华，详细拆解:
                #   request_messages[memory_turn] = {...}: 修改副本的某一回合。
                #   **messages[memory_turn]: 字典展开——把原消息的所有键值对"摊开"放进新字典。
                #     如 {"role":"user","content":"hi"} → role:user, content:hi 都进新字典。
                #   "content": ...: 然后【覆盖】content 键(其他键如 role 保持不变)。
                #   content = 记忆内容 + "\n\n" + 原用户输入。
                #   效果: 模型看到的当前回合 = 记忆 + 用户问题(记忆"注入"到对话里)。
            response = client.messages.create(
                model=MODEL, system=system, messages=request_messages, tools=TOOLS, max_tokens=8000
            )
            reactive_retries = 0
        except Exception as e:
            if ("prompt_too_long" in str(e).lower() or "too many tokens" in str(e).lower()) and reactive_retries < MAX_REACTIVE_RETRIES:
                print("[reactive compact]")
                messages[:] = reactive_compact(messages)
                reactive_retries += 1
                continue
            raise
            # ↑ 紧急压缩(继承 s08)，详见 s08 注释。

        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            # s09: extract from pre-compression snapshot for full fidelity
            extract_memories(pre_compress)
            # ↑ ★ 循环退出前，从【压缩前快照】提取记忆(抽子系统)。
            #   用 pre_compress 而非 messages，因为 messages 可能已被压缩丢细节。
            consolidate_memories()
            # ↑ 提取后检查是否需要合并(整子系统，≥10 才触发)。
            return

        results = []
        for block in response.content:
            if block.type != "tool_use": continue
            print(f"\033[36m> {block.name}\033[0m")
            handler = TOOL_HANDLERS.get(block.name)
            output = handler(**block.input) if handler else f"Unknown: {block.name}"
            print(str(output)[:200])
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    print("s09: Memory — persistent cross-session knowledge")
    print("输入问题，回车发送。输入 q 退出。\n")
    history = []
    while True:
        try: query = input("\033[36ms09 >> \033[0m")
        except (EOFError, KeyboardInterrupt): break
        if query.strip().lower() in ("q", "exit", ""): break
        history.append({"role": "user", "content": query})
        agent_loop(history)
        for block in history[-1]["content"]:
            if getattr(block, "type", None) == "text": print(block.text)
        print()
# ↑ 入口主循环(继承 s08)。详见 s01 注释。
