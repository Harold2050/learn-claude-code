#!/usr/bin/env python3
# ↑ shebang 行，详见 s01 注释。
"""
s10: System Prompt — Runtime prompt assembly with caching.

Run:  python s10_system_prompt/code.py
Need: pip install anthropic python-dotenv + .env with ANTHROPIC_API_KEY

Changes from s09:
  - PROMPT_SECTIONS: topic-keyed dict of prompt fragments
  - assemble_system_prompt(context): select + join sections by real state
  - get_system_prompt(context): deterministic cache via json.dumps
  - agent_loop uses get_system_prompt(context) instead of hardcoded SYSTEM

Memory section loads when .memory/MEMORY.md exists (real state, not keywords).
"""
# ↑ 模块 docstring，详见 s01 注释。
#   本文件核心: 运行时组装系统提示词(而非硬编码)。
#   前 9 章的 SYSTEM 是固定字符串(写死的)。s10 改成"按当前状态选段拼接"。
#   比如有记忆就加记忆段，没有就不加(按真实状态，不按关键词猜)。
#   配套: get_system_prompt 用缓存(状态没变就不重新拼)。

import os, subprocess, json
# ↑ 标准库导入，详见 s08 注释。json 用于缓存 key 序列化(见 get_system_prompt)。
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
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
# ↑ 加载 .env、兼容第三方端点，详见 s01 注释。

WORKDIR = Path.cwd()
MEMORY_DIR = WORKDIR / ".memory"
# ↑ 工作目录、记忆目录(s10 复用 s09 的记忆存储，但只读 MEMORY.md 索引)。
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"
# ↑ 记忆索引文件(继承 s09 概念)。详见 s09 注释。
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]
# ↑ 客户端、模型(继承 s05)，详见 s05 注释。


# ── Prompt Sections ──

PROMPT_SECTIONS = {
    # ★★★ s10 核心新增: 提示片段字典。按主题分块，每个片段是一段文字。
    #   key 是主题名，value 是片段内容。
    #   运行时按"当前状态"决定加载哪些片段(灵活组合)。
    #   好处: 改一处只改这个字典，不用动 agent_loop；新主题加一条即可扩展。
    "identity": "You are a coding agent. Act, don't explain.",
    # ↑ 身份段: 告诉模型它是谁。
    "tools": "Available tools: bash, read_file, write_file.",
    # ↑ 工具段: 列出可用工具(提示模型能力边界)。
    "workspace": f"Working directory: {WORKDIR}",
    # ↑ 工作区段: 告诉模型在哪个目录工作(f-string 注入 WORKDIR)。
    "memory": "Relevant memories are injected below when available.",
    # ↑ 记忆段: 提示模型有记忆会被注入(占位，实际记忆在对话里注入)。
}


def assemble_system_prompt(context: dict) -> str:
    # ★★★ s10 核心新增: 按 context(当前状态)选段拼接成系统提示。
    #   context 是个字典，描述"当前是什么情况"(有哪些工具/工作目录/有无记忆)。
    #   函数根据 context 的真实值决定加载哪些片段。
    """Select and join prompt sections based on current context."""
    sections = []

    # Always loaded — identity, tools, workspace
    sections.append(PROMPT_SECTIONS["identity"])
    sections.append(PROMPT_SECTIONS["tools"])
    sections.append(PROMPT_SECTIONS["workspace"])
    # ↑ 三个基础段永远加载(身份/工具/工作区)。

    # Conditional — memory loaded when MEMORY.md exists and has content
    memories = context.get("memories", "")
    # ↑ 从 context 取"记忆内容"字段，没有就空串。
    if memories:
        sections.append(f"Relevant memories:\n{memories}")
        # ↑ ★ 有记忆内容才加这一段。这是"按真实状态"——记忆为空就不加，避免浪费 token。
        #   对比前面章节硬编码 SYSTEM，s10 是动态的。

    return "\n\n".join(sections)
    # ↑ "\n\n".join(...): 用两个换行(空行)分隔各段，拼成一个完整系统提示。


_last_context_key = None
_last_prompt = None
# ↑ ★ s10 新增: 缓存变量(全局)。记住"上次组装时的 context"和"上次的提示词"。
#   如果这次 context 和上次一样，就直接复用上次的提示词(省去重新拼接)。
#   这俩是模块级全局变量，用 global 声明才能在函数里修改(见 get_system_prompt)。


def get_system_prompt(context: dict) -> str:
    # ★★★ s10 核心新增: 带缓存的系统提示组装器。
    #   思想: context 没变就不重新拼(拼接虽便宜，但缓存能省，且便于未来接入 API 级 prompt cache)。
    #   用 json.dumps(context) 当 key(确定性序列化)，不用 hash()(hash 有进程随机化，不可靠)。
    """Cache wrapper — reassemble only when context changes.

    Uses json.dumps for deterministic serialization, not Python's hash()
    which has process randomization and fails on nested dicts/lists.
    This cache only avoids redundant string assembly within a process.
    Real Claude Code additionally protects API-level prompt cache via
    stable section ordering and SYSTEM_PROMPT_DYNAMIC_BOUNDARY.
    """
    global _last_context_key, _last_prompt
    # ↑ 声明要修改全局变量(不加 global 会创建局部变量)。
    key = json.dumps(context, sort_keys=True, ensure_ascii=False, default=str)
    # ↑ ★ 把 context 字典序列化成字符串，作为缓存 key。
    #   sort_keys=True: 字典键按字母排序(保证同样的内容生成同样的 key，不受插入顺序影响)。
    #   ensure_ascii=False: 保留中文等非 ASCII 字符(不转义成 \uXXXX)。
    #   default=str: 遇到无法序列化的对象转字符串(防报错)。
    #   为什么不用 hash(context)? 因为:
    #     1. hash() 有进程随机化(同程序不同次运行的 hash 值不同，不能跨进程复用)。
    #     2. hash() 不能直接用于字典/列表(不可哈希)。
    #   json.dumps 是"确定性"的——同内容永远同字符串，适合做 key。
    if key == _last_context_key and _last_prompt:
        # ↑ ★ 缓存命中: key 和上次一样 且 上次结果非空。
        print("  \033[90m[cache hit] system prompt unchanged\033[0m")
        return _last_prompt
        # ↑ 直接返回缓存的提示词，不重新组装。
    _last_context_key = key
    _last_prompt = assemble_system_prompt(context)
    # ↑ 缓存未命中: 更新 key，重新组装提示词存起来。

    loaded = ["identity", "tools", "workspace"]
    if context.get("memories"):
        loaded.append("memory")
    print(f"  \033[32m[assembled] sections: {', '.join(loaded)}\033[0m")
    # ↑ 打印本次加载了哪些段(绿色)。', '.join(列表) 用逗号空格连接。
    return _last_prompt


# ── Tools ──

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


TOOLS = [
    # ↑ 工具声明(s10 精简到 3 个，聚焦系统提示主题)。逐项注释见 s02 的 TOOLS。
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
]

TOOL_HANDLERS = {"bash": run_bash, "read_file": run_read, "write_file": run_write}
# ↑ 工具分发映射(s10 精简版)。详见 s02 的 TOOL_HANDLERS。


# ── Context ──

def update_context(context: dict, messages: list) -> dict:
    # ★★★ s10 新增: 从真实状态派生 context 字典。
    #   不靠关键词猜——直接检查"文件是否存在""工具有哪些"等真实情况。
    #   这是 s10 的核心理念: 按真实状态组装，不按关键词。
    """Derive context from real state: which tools exist, whether memory files exist."""
    memories = ""
    if MEMORY_INDEX.exists():
        # ↑ ★ 检查文件是否真实存在(不是猜"用户可能说过记忆")。
        content = MEMORY_INDEX.read_text().strip()
        if content:
            memories = content
            # ↑ 文件存在且有内容才设 memories。
    return {
        "enabled_tools": list(TOOL_HANDLERS.keys()),
        # ↑ list(dict.keys()): 取所有工具名(转列表)。描述"当前有哪些工具"。
        "workspace": str(WORKDIR),
        # ↑ 工作目录(转字符串)。
        "memories": memories,
        # ↑ 记忆索引内容(有就非空，没有就空串)。
    }


# ── Agent Loop ──

def agent_loop(messages: list, context: dict):
    # ★★★ s10 agent loop: 接收 context 参数，用 get_system_prompt 组装提示(而非硬编码 SYSTEM)。
    #   注意: s10 的 agent_loop 比 s05-s09 多一个 context 参数。
    """Main loop — uses assembled system prompt instead of hardcoded SYSTEM."""
    system = get_system_prompt(context)
    # ↑ ★ 进入循环前，按当前 context 组装系统提示词。
    while True:
        response = client.messages.create(
            model=MODEL, system=system, messages=messages,
            tools=TOOLS, max_tokens=8000)
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            return
            # ↑ 调 API + 存历史 + 不调工具就结束，详见 s01 注释。

        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            print(f"\033[36m> {block.name}\033[0m")
            handler = TOOL_HANDLERS.get(block.name)
            output = handler(**block.input) if handler else f"Unknown: {block.name}"
            print(str(output)[:200])
            results.append({"type": "tool_result",
                            "tool_use_id": block.id, "content": output})
        messages.append({"role": "user", "content": results})

        # Re-evaluate context and prompt after each tool round
        context = update_context(context, messages)
        system = get_system_prompt(context)
        # ↑ ★★★ s10 核心改动: 每轮工具执行后，重新评估 context 并重组系统提示。
        #   为什么? 因为工具可能改变了真实状态(如 write_file 创建了 MEMORY.md)，
        #   下一轮系统提示应该反映这个变化(动态适应)。
        #   get_system_prompt 有缓存——状态没变就直接复用，不重复拼接。


if __name__ == "__main__":
    print("s10: system prompt — runtime assembly")
    print("Enter a question, press Enter to send. Type q to quit.\n")
    history = []
    context = update_context({}, [])
    # ↑ ★ 程序启动时，先用空 context 初始化真实状态(检查文件等)。
    while True:
        try:
            query = input("\033[36ms10 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history.append({"role": "user", "content": query})
        agent_loop(history, context)
        # ↑ ★ 把 context 传给 agent_loop(s10 多了这个参数)。
        context = update_context(context, history)
        # ↑ 每轮用户交互后更新 context(反映最新状态)。
        for block in history[-1]["content"]:
            if getattr(block, "type", None) == "text":
                print(block.text)
        print()
# ↑ 入口主循环。和 s01-s09 区别: 多了 context 的初始化和传递。详见 s01 注释。
