#!/usr/bin/env python3
# ↑ shebang 行，详见 s01 注释。

"""
s02: Tool Use — 在 s01 基础上新增 4 个工具 + 分发映射。

运行: python s02_tool_use/code.py
需要: pip install anthropic python-dotenv + .env 中配置 ANTHROPIC_API_KEY

本文件 = s01 的全部代码 + 以下新增:
  + run_read / run_write / run_edit / run_glob 四个工具实现
  + TOOL_HANDLERS 分发映射（替代 s01 中硬编码的 run_bash 调用）
  + safe_path 路径安全校验

循环本身（agent_loop）与 s01 完全一致。
"""

# ── 导入模块 ──────────────────────────────────────────────
import os, subprocess
# ↑ 一行导入多个模块(逗号分隔)。等价于:
#   import os
#   import subprocess
#   os/subprocess 详见 s01 注释。
from pathlib import Path
# ↑ pathlib 是 Python 标准库，用"面向对象"方式操作文件路径。
#   Path 是它的核心类。比传统的 os.path 更好用。
#   例如: Path("a/b.txt").read_text() 直接读文件内容。
#        Path.cwd() 返回当前目录(等价于 os.getcwd())。

try:
    import readline
    readline.parse_and_bind('set bind-tty-special-chars off')
    readline.parse_and_bind('set input-meta on')
    readline.parse_and_bind('set output-meta on')
    readline.parse_and_bind('set convert-meta off')
except ImportError:
    pass
# ↑ readline 中文输入修复，详见 s01 注释。

from anthropic import Anthropic
from dotenv import load_dotenv
# ↑ Anthropic 客户端、dotenv，详见 s01 注释。

load_dotenv(override=True)
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
# ↑ 加载 .env、兼容第三方 API 端点，详见 s01 注释。

WORKDIR = Path.cwd()
# ↑ ★ s02 新增: 把当前工作目录存成全局变量 WORKDIR(一个 Path 对象)。
#   s01 用 os.getcwd() 返回字符串；s02 用 Path.cwd() 返回 Path 对象。
#   Path 对象的好处:支持 / 拼接(如 WORKDIR / "file.txt")、.read_text() 等。
#   后面所有工具都用 WORKDIR 作为"工作区根目录"。
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]
# ↑ 客户端、模型 ID，详见 s01 注释。

SYSTEM = f"You are a coding agent at {WORKDIR}. Use tools to solve tasks. Act, don't explain."
# ↑ 系统提示词。和 s01 类似，但 s02 强调"Use tools"(用多种工具)，不是只用 bash。


# ═══════════════════════════════════════════════════════════
#  FROM s01 (unchanged)
# ═══════════════════════════════════════════════════════════

def run_bash(command: str) -> str:
    # ↑ 继承自 s01，执行 shell 命令。完整逐行注释见 s01 的 run_bash。
    #   唯一区别: cwd 从 os.getcwd() 改成 WORKDIR(等价，只是变量名更明确)。
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120)
        # ↑ encoding="utf-8": 强制用 UTF-8 解码输出(支持中文)。
        #   errors="replace": 遇到无法解码的字节不崩溃，替换成 ?。更健壮。
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"


# ═══════════════════════════════════════════════════════════
#  NEW in s02: 4 个新工具
# ═══════════════════════════════════════════════════════════

def safe_path(p: str) -> Path:
    # ★★★ s02 核心新增: 路径安全校验。防止模型读写工作区之外的文件。
    #   想象: 如果模型调 write_file(path="../../../etc/passwd")，
    #   没有校验就会篡改系统文件。safe_path 就是堵这个漏洞。
    path = (WORKDIR / p).resolve()
    # ↑ WORKDIR / p: Path 对象用 / 拼接路径。如 WORKDIR/"a/b.txt" 得到完整路径。
    #   .resolve(): 把路径"解析"成绝对路径，展开所有 . 和 .. 和符号链接。
    #   例如 "/a/b/../c.txt".resolve() → "/a/c.txt"。
    if not path.is_relative_to(WORKDIR):
        # ↑ is_relative_to(目录): 检查 path 是否在该目录"内部"。
        #   如果 resolve 后的路径跑出了 WORKDIR(比如 .. 跳出来了)，返回 False。
        raise ValueError(f"Path escapes workspace: {p}")
        # ↑ raise: 主动抛出异常(错误)。ValueError 是"值不合法"的异常类型。
        #   抛出后，调用方需要用 try/except 接住(见下方各工具)。
    return path
    # ↑ 校验通过，返回安全的绝对路径(Path 对象)。


def run_read(path: str, limit: int | None = None) -> str:
    # ↑ int | None = None: 参数 limit 可以是整数或 None(空)。
    #   int | None 是 Python 3.10+ 的"联合类型"写法(等价于旧写法 Optional[int])。
    #   = None: 默认值是 None(调用时不传 limit 就用 None)。
    try:
        lines = safe_path(path).read_text().splitlines()
        # ↑ safe_path(path): 先校验路径安全，返回 Path 对象。
        #   .read_text(): 读取整个文件内容为字符串。
        #   .splitlines(): 按换行符分割成"行列表"。如 "a\nb" → ["a", "b"]。
        if limit and limit < len(lines):
            # ↑ if limit: limit 非 None 且非 0(0 在 if 里算假)。
            #   limit < len(lines): 请求的行数少于实际行数，需要截断。
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
            # ↑ lines[:limit]: 切片，取前 limit 行。
            #   + [...]: 列表拼接，末尾加一条提示"还有多少行没显示"。
            #   len(lines) - limit: 总行数减已显示行数 = 剩余行数。
        return "\n".join(lines)
        # ↑ "\n".join(列表): 用换行符把列表元素拼成一个字符串。
        #   join 的反操作是 split。join 是字符串方法，split 是它的对称操作。
    except Exception as e:
        # ↑ Exception 是所有异常的基类。捕获 Exception = 捕获一切错误。
        #   在工具里这样写很实用:工具不会因为异常崩溃整个 agent。
        return f"Error: {e}"


def run_write(path: str, content: str) -> str:
    # ↑ 写文件工具。模型用它创建或覆盖文件。
    try:
        file_path = safe_path(path)
        # ↑ 校验路径安全。
        file_path.parent.mkdir(parents=True, exist_ok=True)
        # ↑ file_path.parent: 父目录(如 /a/b/c.txt 的 parent 是 /a/b)。
        #   .mkdir(...): 创建目录。
        #   parents=True: 递归创建(如 /a/b/c 都不存在，全建出来)。
        #   exist_ok=True: 目录已存在不报错(否则会抛 FileExistsError)。
        file_path.write_text(content)
        # ↑ 把 content 写入文件(覆盖原有内容)。
        return f"Wrote {len(content)} bytes to {path}"
        # ↑ len(content): 字符串长度(字符数)。返回成功信息。
    except Exception as e:
        return f"Error: {e}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    # ↑ 编辑工具。精确替换文件中的一段文字(只替换第一处)。
    #   比整体覆盖(write)更安全——只改需要改的地方。
    try:
        file_path = safe_path(path)
        text = file_path.read_text()
        # ↑ 读出当前文件全文。
        if old_text not in text:
            # ↑ in 运算符: 检查 old_text 是否是 text 的子串。
            #   如果要替换的文字不存在，说明模型记错了，返回错误。
            return f"Error: text not found in {path}"
        file_path.write_text(text.replace(old_text, new_text, 1))
        # ↑ text.replace(旧, 新, 1): 字符串替换方法。
        #   第三个参数 1 = 只替换第一处(不替换后面所有匹配)。
        #   这样避免误改多处同名内容。
        #   替换后的完整文本再写回文件。
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


def run_glob(pattern: str) -> str:
    # ↑ glob 工具。按"通配符模式"查找文件。如 "*.py" 找所有 Python 文件。
    import glob as g
    # ↑ 在函数内部 import: 用到才导入(惰性导入)。
    #   as g: 给模块起别名 glob → g，写起来短。
    #   (注意: import glob 和 from pathlib import Path 不冲突，是两个不同的模块)
    try:
        results = []
        for match in g.glob(pattern, root_dir=WORKDIR):
            # ↑ g.glob(模式, root_dir=目录): 在指定目录下按通配符找文件。
            #   通配符: * 匹配任意字符，** 匹配任意层目录，? 匹配单字符。
            #   返回的是"相对路径"列表(相对于 root_dir)。
            if (WORKDIR / match).resolve().is_relative_to(WORKDIR):
                # ↑ 双重安全: glob 理论上不会返回工作区外的路径，
                #   但万一是符号链接，再校验一次。和 safe_path 思路一致。
                results.append(match)
        return "\n".join(results) if results else "(no matches)"
        # ↑ 有结果用换行拼起来返回；没结果返回 "(no matches)"。
        #   三元表达式，详见 s01 注释。
    except Exception as e:
        return f"Error: {e}"


# ═══════════════════════════════════════════════════════════
#  NEW in s02: 工具定义（s01 只有一个 bash，现在扩展到 5 个）
# ═══════════════════════════════════════════════════════════

TOOLS = [
    # ↑ 工具声明列表。每个工具是一个字典(含 name/description/input_schema)。
    #   这个列表会传给 API，模型看了才知道有哪些工具可用。
    #   input_schema 里的 JSON Schema 语法见 s01 注释。
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
     # ↑ limit 是可选(不在 required 里)。read_file 实现里默认 None。
    {"name": "write_file", "description": "Write content to a file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in a file once.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "glob", "description": "Find files matching a glob pattern.",
     "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}},
]

# ═══════════════════════════════════════════════════════════
#  NEW in s02: 工具分发映射（s01 是硬编码 run_bash，现在改为查表）
# ═══════════════════════════════════════════════════════════

TOOL_HANDLERS = {
    # ★★★ s02 灵魂: "工具名 → 处理函数"的映射字典(查表)。
    #   s01 里写死了 output = run_bash(...)，只能跑 bash。
    #   s02 改成查表: handler = TOOL_HANDLERS[工具名]，再调 handler(...)。
    #   这样加新工具只需: 1) 写个函数；2) 在这里登记一行。不用改 agent_loop。
    #   字典的 value 可以是函数(函数在 Python 里是"一等对象"，能当值传递)。
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob,
    # ↑ "bash": run_bash 表示键 "bash" 映射到函数 run_bash(注意没括号，是函数本身不是调用结果)。
}


# ═══════════════════════════════════════════════════════════
#  agent_loop — 与 s01 结构完全一致，只改了工具执行那部分
#  s01: output = run_bash(block.input["command"])
#  s02: output = TOOL_HANDLERS[block.name](**block.input)
# ═══════════════════════════════════════════════════════════

def agent_loop(messages: list):
    # ↑ agent 循环。整体结构和 s01 完全一样(while True + 调 API + 跑工具 + 喂回)。
    #   agent 工程的核心思想: 循环恒定，机制叠加。后面 19 章都改这个循环的细节。
    while True:
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        # ↑ 调 Claude API，详见 s01 注释。
        messages.append({"role": "assistant", "content": response.content})
        # ↑ 把模型回复加入历史，详见 s01 注释。

        if response.stop_reason != "tool_use":
            return
            # ↑ 模型不调工具就结束，详见 s01 注释。

        results = []
        for block in response.content:
            if block.type == "tool_use":
                print(f"\033[33m> {block.name}\033[0m")
                # ↑ 打印工具名(黄色)。s01 打印命令；s02 打印工具名(因为有多种工具)。
                handler = TOOL_HANDLERS.get(block.name)
                # ↑ ★ s02 关键改动: 从映射表查处理函数。
                #   dict.get(key): 按 key 取值，不存在返回 None(不报错)。
                #   对比 dict[key]: 不存在会抛 KeyError。
                #   这里用 get 更安全(模型可能调了不存在的工具名)。
                output = handler(**block.input) if handler else f"Unknown: {block.name}"
                # ↑ ★★★ 这行是 s02 的精华，详细拆解:
                #   1) handler 如果存在(if handler):
                #      handler(**block.input) —— 调用函数。
                #      ** 是"字典解包":把 block.input 字典展开成关键字参数。
                #      例: block.input = {"path": "a.txt", "content": "hi"}
                #          **block.input 等价于 handler(path="a.txt", content="hi")
                #      这样不同工具的参数自动对应到函数的形参，无需手动判断。
                #   2) handler 不存在(else): 返回 "Unknown: 工具名"。
                #   三元表达式 A if 条件 else B，详见 s01。
                print(str(output)[:200])
                # ↑ 打印输出预览前 200 字符。str() 转字符串(防止 output 不是字符串)。
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})
                # ↑ 收集工具结果，详见 s01 注释。

        messages.append({"role": "user", "content": results})
        # ↑ 把结果喂回历史，详见 s01 注释。


if __name__ == "__main__":
    print("s02: Tool Use — 在 s01 基础上加了 4 个工具")
    print("输入问题，回车发送。输入 q 退出。\n")

    history = []
    while True:
        try:
            query = input("\033[36ms02 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history.append({"role": "user", "content": query})
        agent_loop(history)
        for block in history[-1]["content"]:
            if getattr(block, "type", None) == "text":
                print(block.text)
        print()
# ↑ 入口主循环，逻辑与 s01 一致(读取输入→调 agent_loop→打印回答)，详见 s01 注释。
