#!/usr/bin/env python3
# ↑ shebang 行，详见 s01 注释。
"""
s11: Error Recovery — three recovery paths + exponential backoff.

Run:  python s11_error_recovery/code.py
Need: pip install anthropic python-dotenv + .env with ANTHROPIC_API_KEY

Changes from s10:
  - LLM call wrapped in try/except with three recovery paths
  - Path 1: max_tokens -> escalate 8K->64K (no append on first escalation),
            then continuation prompt (max 3)
  - Path 2: prompt_too_long -> reactive compact -> retry (once)
  - Path 3: 429/529 -> exponential backoff with jitter (max 10),
            fallback model on consecutive 529
  - with_retry wrapper for transient errors
  - RecoveryState tracks escalation / compact / 529 / model

ASCII flow:
  messages -> prompt assembly -> compress+load -> [try] LLM [except] -> tools -> loop
                                                    |          |
                                              stop_reason   error type
                                              max_tokens?   prompt_too_long? -> compact
                                              escalate /    429/529? -> backoff
                                              continue      other? -> log + exit
"""
# ↑ 模块 docstring，详见 s01 注释。
#   本文件核心: 错误恢复。LLM 调用可能因各种原因失败(超长/限流/过载/截断)。
#   s11 给每类错误设计恢复路径:
#   路径1 max_tokens(输出被截断): 升级 8K→64K → 续写提示。
#   路径2 prompt_too_long(输入太长): 紧急压缩 → 重试。
#   路径3 429/529(限流/过载): 指数退避(等一会再试) → 连续529切备用模型。

import os, subprocess, time, random, json
# ↑ 标准库导入。
#   time: s11 用于退避等待(time.sleep)。详见 s08。
#   random: s11 新增。random.uniform(a,b) 生成 [a,b] 间的随机浮点数，用于"抖动"。
#   json: 详见 s09。
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
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
PRIMARY_MODEL = os.environ["MODEL_ID"]
# ↑ 主模型 ID。os.environ["KEY"] 详见 s01 注释。
FALLBACK_MODEL = os.getenv("FALLBACK_MODEL_ID")
# ↑ ★ s11 新增: 备用模型 ID(可选)。用 os.getenv 而非 os.environ[]——因为它是可选的，
#   没设返回 None(不崩溃)。主模型连续 529(过载)时切到这个备用模型。

# ── Constants ──
# ↓ s11 的常量配置。

ESCALATED_MAX_TOKENS = 64000
# ↑ 路径1 升级后的 token 上限(64K)。默认 8K 不够用时升级到这个。
DEFAULT_MAX_TOKENS = 8000
# ↑ 默认 token 上限(8K)。
MAX_RECOVERY_RETRIES = 3
# ↑ 路径1 续写提示最多重试 3 次(防无限续写)。
MAX_RETRIES = 10
# ↑ 路径3 退避最多重试 10 次(防无限等待)。
BASE_DELAY_MS = 500
# ↑ 退避起始延迟 500 毫秒。每次翻倍(指数退避)。
MAX_CONSECUTIVE_529 = 3
# ↑ 连续 529(过载) 达到 3 次就切换备用模型。
CONTINUATION_PROMPT = (
    # ↑ 路径1 续写提示词: 64K 还截断时，让模型从断点继续。
    "Output token limit hit. Resume directly — "
    "no apology, no recap. Pick up mid-thought."
)

# ── Prompt Assembly (from s10, synced) ──

PROMPT_SECTIONS = {
    # ↑ 提示片段字典(继承 s10)。逐行注释见 s10 的 PROMPT_SECTIONS。
    "identity": "You are a coding agent. Act, don't explain.",
    "tools": "Available tools: bash, read_file, write_file.",
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
        print("  \033[90m[cache hit] system prompt unchanged\033[0m")
        return _last_prompt
    _last_context_key = key
    _last_prompt = assemble_system_prompt(context)

    loaded = ["identity", "tools", "workspace"]
    if context.get("memories"):
        loaded.append("memory")
    print(f"  \033[32m[assembled] sections: {', '.join(loaded)}\033[0m")
    return _last_prompt


# ── Tools (unchanged) ──

def safe_path(p: str) -> Path:
    # ↑ 继承自 s02，路径安全校验。逐行注释见 s02 的 safe_path。
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
        file_path = safe_path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


TOOLS = [
    # ↑ 工具声明(继承 s10)。逐项注释见 s10 的 TOOLS。
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
# ↑ 工具分发(继承 s10)。详见 s10 注释。


# ── Error Recovery (s11 new) ──

class RecoveryState:
    # ★★★ s11 核心新增: 错误恢复状态类。
    #   class 定义类。语法: class 类名: 然后缩进写方法。
    #   这个类用来"记住"恢复过程中发生过什么(升级过没/压缩过没/连续529几次/当前用哪个模型)。
    #   为什么需要它? 恢复要跨多轮循环持续跟踪状态，不能用局部变量(每次循环重新创建)。
    """Track recovery attempts across the loop."""
    def __init__(self):
        # ↑ __init__ 是"构造函数"——创建对象时自动调用。
        #   self 代表"当前对象实例"(类似其他语言的 this)。
        #   给对象的属性赋初值用 self.属性名 = 值。
        self.has_escalated = False
        # ↑ 是否已经升级过 max_tokens(8K→64K)。升级过就不再升级。
        self.recovery_count = 0
        # ↑ 续写提示已用几次(上限 MAX_RECOVERY_RETRIES=3)。
        self.consecutive_529 = 0
        # ↑ 连续遇到 529(过载) 的次数。到 MAX_CONSECUTIVE_529=3 就切备用模型。
        self.has_attempted_reactive_compact = False
        # ↑ 是否已试过紧急压缩(路径2)。只试一次，防止"压缩→还报错→压缩→还报错"死循环。
        self.current_model = PRIMARY_MODEL
        # ↑ 当前使用的模型 ID。初始是主模型，连续529后可能切 FALLBACK_MODEL。


def retry_delay(attempt, retry_after=None):
    # ★ s11 新增: 计算退避延迟时间(秒)。
    #   指数退避: 每次等待时间翻倍(500ms→1s→2s→4s...)，给服务器恢复时间。
    #   抖动(jitter): 加一点随机量，防止多个客户端同时重试(惊群效应)。
    """Exponential backoff with jitter. Retry-After takes priority."""
    if retry_after:
        return retry_after
        # ↑ 如果 API 返回了 Retry-After 头(建议等待时间)，直接用它(优先)。
    base = min(BASE_DELAY_MS * (2 ** attempt), 32000) / 1000
    # ↑ ★ 指数退避计算:
    #   2 ** attempt: 2 的 attempt 次方(**是幂运算)。attempt=0→1, 1→2, 2→4...
    #   BASE_DELAY_MS * (2**attempt): 500*1, 500*2, 500*4... 毫秒。
    #   min(..., 32000): 上限 32 秒(防指数爆炸等太久)。
    #   / 1000: 毫秒转秒。
    jitter = random.uniform(0, base * 0.25)
    # ↑ ★ 抖动: 在 [0, base*0.25] 范围内加随机量。
    #   random.uniform(a, b): 返回 [a,b] 间随机浮点数。
    #   base*0.25: 抖动量是基础延迟的 25%(不会偏太远)。
    return base + jitter
    # ↑ 实际延迟 = 基础 + 抖动。


def with_retry(fn, state: RecoveryState):
    # ★★★ s11 核心新增: 带退避重试的 LLM 调用包装器。
    #   fn: 要执行的函数(无参，通常是 lambda 包了 API 调用)。
    #   state: RecoveryState 对象，跟踪恢复状态。
    #   思想: 瞬时错误(429限流/529过载)重试可能成功；非瞬时错误(参数错)抛出给上层。
    """Exponential backoff for transient errors (429/529).
    Non-transient errors are re-raised for the outer handler."""
    for attempt in range(MAX_RETRIES):
        # ↑ range(MAX_RETRIES)=range(10): 最多重试 10 次。attempt 从 0 到 9。
        try:
            result = fn()
            # ↑ 调用传入的函数(执行 LLM 调用)。
            state.consecutive_529 = 0
            # ↑ 成功了，重置连续529计数(过载已缓解)。
            return result
            # ↑ 成功返回结果。
        except Exception as e:
            # ↑ 捕获异常。
            name = type(e).__name__
            # ↑ ★ type(e): 异常对象的类型(类)。
            #   type(e).__name__: 类的名字(字符串)。如 RateLimitError、APIStatusError。
            #   __name__ 是类的内置属性，存类名。
            msg = str(e).lower()
            # ↑ 异常转字符串并转小写，便于模糊匹配。

            # 429 rate limit -> exponential backoff
            if "ratelimit" in name.lower() or "429" in msg:
                # ↑ 检测 429 限流: 异常类名含 "ratelimit" 或消息含 "429"。
                delay = retry_delay(attempt)
                print(f"  \033[33m[429 rate limit] retry {attempt+1}/{MAX_RETRIES},"
                      f" wait {delay:.1f}s\033[0m")
                # ↑ f"{}:.1f": 格式化浮点数保留1位小数。如 1.5s。
                time.sleep(delay)
                # ↑ 等待 delay 秒(阻塞当前线程)。
                continue
                # ↑ 回到 for 顶部重试。

            # 529 overloaded -> exponential backoff + fallback model
            if "overloaded" in name.lower() or "529" in msg or "overloaded" in msg:
                # ↑ 检测 529 过载。
                state.consecutive_529 += 1
                if state.consecutive_529 >= MAX_CONSECUTIVE_529:
                    # ↑ 连续 529 达到 3 次 → 切备用模型。
                    if FALLBACK_MODEL:
                        state.current_model = FALLBACK_MODEL
                        state.consecutive_529 = 0
                        print(f"  \033[31m[529 x{MAX_CONSECUTIVE_529}]"
                              f" switching to {FALLBACK_MODEL}\033[0m")
                        # ↑ 有备用模型就切换，重置计数。
                    else:
                        state.consecutive_529 = 0
                        print(f"  \033[31m[529 x{MAX_CONSECUTIVE_529}]"
                              f" no FALLBACK_MODEL_ID configured, continuing retry\033[0m")
                        # ↑ 没配备用模型，重置计数继续重试主模型。
                delay = retry_delay(attempt)
                print(f"  \033[33m[529 overloaded] retry {attempt+1}/{MAX_RETRIES},"
                      f" wait {delay:.1f}s\033[0m")
                time.sleep(delay)
                continue

            # Not transient -> re-raise for outer try/except
            raise
            # ↑ ★ raise 单独用: 重新抛出当前异常(不处理，交给上层)。
            #   非瞬时错误(如参数错、认证错)重试无用，抛给 agent_loop 的外层 try/except。
    raise RuntimeError(f"Max retries ({MAX_RETRIES}) exceeded")
    # ↑ for 循环正常结束(10次都没成功)→ 抛"超过最大重试"错误。


def is_prompt_too_long_error(e: Exception) -> bool:
    # ★ s11 新增: 判断异常是不是"输入太长"类型。
    #   不同提供商的错误消息措辞不同，这里模糊匹配多种说法。
    """Check whether an API error indicates prompt/context too long."""
    msg = str(e).lower()
    return (("prompt" in msg and "long" in msg)
            or "prompt_is_too_long" in msg
            or "context_length_exceeded" in msg
            or "max_context_window" in msg)
    # ↑ 四种匹配条件(任一满足即返回 True):
    #   1. 同时含 "prompt" 和 "long"
    #   2. 含 "prompt_is_too_long"(Anthropic)
    #   3. 含 "context_length_exceeded"(OpenAI)
    #   4. 含 "max_context_window"


def reactive_compact(messages: list) -> list:
    # ★ s11 新增: 紧急压缩(路径2)。输入太长时，裁掉旧消息只留最近5条。
    #   教学版简化: 只留尾部5条。真实CC会用LLM生成摘要(s08/s09已演示LLM压缩)。
    """Emergency compact — teaching version keeps last N messages.
    Real CC generates a compact summary via LLM, then retries with
    the compacted message list. Teaching version simplifies to tail
    retention since s08/s09 already cover LLM-based compact."""
    print("  \033[31m[reactive compact] trimming to last 5 messages\033[0m")
    tail = messages[-5:]
    # ↑ 切片取最后5条。-5 表示从倒数第5条到末尾。
    return [{"role": "user",
             "content": "[Reactive compact] Earlier conversation trimmed. "
                        "Continue from where you left off."}, *tail]
    # ↑ ★ *tail 解包: 把列表元素展开加进新列表。详见 s08 的 reactive_compact。
    #   返回: [提示消息] + [最近5条]。


# ── Context ──

def update_context(context: dict, messages: list) -> dict:
    # ↑ 更新上下文(继承 s10)。逐行注释见 s10。
    """Derive context from real state: which tools exist, whether memory files exist."""
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


# ── Agent Loop ──

def agent_loop(messages: list, context: dict):
    # ★★★ s11 核心 agent loop: LLM 调用包了三层错误恢复。
    """Main loop with error recovery wrapping LLM calls."""
    system = get_system_prompt(context)
    state = RecoveryState()
    # ↑ ★ 创建恢复状态对象(每轮会话一个)。在循环间持续跟踪恢复状态。
    max_tokens = DEFAULT_MAX_TOKENS
    # ↑ 初始 token 上限 8K。

    while True:
        # ── LLM call: with_retry handles 429/529, outer handles rest ──
        try:
            response = with_retry(
                # ↑ ★ 路径3: 用 with_retry 包装 LLM 调用(处理429/529)。
                lambda mt=max_tokens, mdl=state.current_model:
                    client.messages.create(
                        model=mdl, system=system, messages=messages,
                        tools=TOOLS, max_tokens=mt),
                # ↑ ★★★ 这个 lambda 很精妙，详细拆解:
                #   lambda mt=max_tokens, mdl=state.current_model: ...
                #   1) lambda: 匿名函数(一行函数)。语法 lambda 参数: 表达式。
                #   2) mt=max_tokens: 默认参数捕获当前 max_tokens 的值。
                #      ★ 关键: 用默认参数而不是闭包变量，是为了"冻结"当前值。
                #      如果写 lambda: client...(max_tokens=max_tokens)，
                #      lambda 会在【调用时】读 max_tokens(那时可能已变)。
                #      用默认参数 mt=max_tokens 在【定义时】就固定了值。
                #   3) mdl=state.current_model: 同理冻结当前模型。
                #   with_retry 会多次调这个 lambda，每次用同一组参数。
                state)
        except Exception as e:
            # ↑ with_retry 抛出的非瞬时错误(包括路径3用尽重试)在这里处理。
            # Path 2: prompt_too_long -> reactive compact (once)
            if is_prompt_too_long_error(e):
                # ↑ 路径2: 输入太长。
                if not state.has_attempted_reactive_compact:
                    # ↑ 只试一次紧急压缩。
                    messages[:] = reactive_compact(messages)
                    state.has_attempted_reactive_compact = True
                    continue
                    # ↑ messages[:] 原地替换(详见 s08)，重新调 API。
                print("  \033[31m[unrecoverable] still too long after compact\033[0m")
                messages.append({"role": "assistant", "content": [
                    {"type": "text",
                     "text": "[Error] Context too large, cannot continue."}]})
                return
                # ↑ 压缩后还是太长 → 报错退出。

            # Unrecoverable
            name = type(e).__name__
            print(f"  \033[31m[unrecoverable] {name}: {str(e)[:100]}\033[0m")
            messages.append({"role": "assistant", "content": [
                {"type": "text", "text": f"[Error] {name}: {str(e)[:200]}"}]})
            return
            # ↑ 其他无法恢复的错误(认证失败等) → 记录错误退出。

        # ── Path 1: max_tokens -> escalate or continue ──
        if response.stop_reason == "max_tokens":
            # ↑ ★ 路径1: 输出被截断(stop_reason 是 "max_tokens")。
            # First escalation: don't append truncated output, retry same request
            if not state.has_escalated:
                # ↑ 第一次截断: 升级 token 上限。
                max_tokens = ESCALATED_MAX_TOKENS
                state.has_escalated = True
                print(f"  \033[33m[max_tokens] escalating"
                      f" {DEFAULT_MAX_TOKENS} -> {ESCALATED_MAX_TOKENS}\033[0m")
                continue
                # ↑ ★ 不 append 截断的输出，直接重试(用更大的 max_tokens)。
                #   不 append 是因为截断的输出不完整，留着会干扰。
            # 64K still truncated: save truncated output + continuation prompt
            messages.append({"role": "assistant", "content": response.content})
            if state.recovery_count < MAX_RECOVERY_RETRIES:
                # ↑ 升级后还截断: 存截断输出 + 发续写提示。
                messages.append({"role": "user", "content": CONTINUATION_PROMPT})
                state.recovery_count += 1
                print(f"  \033[33m[max_tokens] continuation"
                      f" {state.recovery_count}/{MAX_RECOVERY_RETRIES}\033[0m")
                continue
            print("  \033[31m[max_tokens] recovery limit reached\033[0m")
            return
            # ↑ 续写 3 次还不行 → 放弃。

        # Normal completion: append assistant response
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            return
            # ↑ 正常完成(不调工具)就退出。

        # ── Tool execution ──
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
        # ↑ 工具执行(继承 s01/s02)，详见 s02 注释。

        context = update_context(context, messages)
        system = get_system_prompt(context)
        # ↑ 每轮重组提示(继承 s10)。


if __name__ == "__main__":
    print("s11: error recovery")
    print("Enter a question, press Enter to send. Type q to quit.\n")
    history = []
    context = update_context({}, [])
    while True:
        try:
            query = input("\033[36ms11 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        turn_start = len(history)
        # ↑ ★ 记录本轮开始前 history 的长度，用于定位本轮新增的 assistant 消息。
        history.append({"role": "user", "content": query})
        agent_loop(history, context)
        context = update_context(context, history)
        for msg in history[turn_start:]:
            # ↑ history[turn_start:]: 切片，取本轮新增的所有消息(可能多条，因为 max_tokens 续写)。
            if msg.get("role") != "assistant":
                continue
                # ↑ 只看 assistant 消息。
            for block in msg["content"]:
                if getattr(block, "type", None) == "text":
                    print(block.text)
        print()
# ↑ 入口主循环。和 s10 区别: 用 turn_start 定位本轮所有 assistant 消息(因为 s11 可能多次续写)。
