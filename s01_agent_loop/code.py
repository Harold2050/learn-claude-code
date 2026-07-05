#!/usr/bin/env python3
# ↑ 这是"shebang"行(#!开头)。告诉 Unix/Linux 系统:用 python3 解释器运行本文件。
# 在命令行直接 ./code.py 执行时有用(需要 chmod +x)。Windows 下可忽略。

"""
s01_agent_loop.py - The Agent Loop

The entire secret of an AI coding agent in one pattern:

    while stop_reason == "tool_use":
        response = LLM(messages, tools)
        execute tools
        append results

    +----------+      +-------+      +---------+
    |   User   | ---> |  LLM  | ---> |  Tool   |
    |  prompt  |      |       |      | execute |
    +----------+      +---+---+      +----+----+
                          ^               |
                          |   tool_result |
                          +---------------+
                          (loop continues)

This is the core loop: feed tool results back to the model
until the model decides to stop. Production agents layer
policy, hooks, and lifecycle controls on top.

Usage:
    pip install anthropic python-dotenv
    ANTHROPIC_API_KEY=... python s01_agent_loop/code.py
"""
# ↑ 上面这整个用三引号 """...""" 包裹的多行文本叫"文档字符串"(docstring)。
#   写在文件/函数/类的最开头，用来描述它是干嘛的。
#   Python 的 help() 和 IDE 悬浮提示会显示这段文字。
#   注意: docstring 内部不能再出现 """ (会让字符串提前结束)，所以对它的解释只能放在外面用 # 注释。

# ── 导入模块 ──────────────────────────────────────────────
# import 语句:把别的 Python 文件(标准库或第三方包)的功能引入当前文件。
# 语法: import 模块名。之后用 模块名.功能 来调用。

import os
# ↑ os 是 Python 标准库，提供操作系统相关功能:
#   - os.getcwd(): 获取当前工作目录(Get Current Working Directory)
#   - os.environ: 系统环境变量(字典)
#   - os.getenv("XXX"): 读取某个环境变量

import subprocess
# ↑ subprocess 标准库:在 Python 里运行外部命令(如 ls、git、npm)。
#   核心函数 subprocess.run(...) 见下方 run_bash。

try:
    # try/except: 异常处理。try 块里"尝试"执行代码，出错就跳到 except。
    import readline
    # ↑ readline 是 Python 标准库，增强命令行输入体验(用上下箭头翻历史输入)。
    #   但它在 Windows 上不存在，所以用 try/except 包住:导不导入都不影响运行。
    # macOS 的 libedit 在处理中文输入时有退格问题，这四行修复它
    # ↓ parse_and_bind 是 readline 的函数:设置 readline 的行为选项。
    readline.parse_and_bind('set bind-tty-special-chars off')
    readline.parse_and_bind('set input-meta on')      # 允许输入 8 位字符(中文)
    readline.parse_and_bind('set output-meta on')     # 允许输出 8 位字符
    readline.parse_and_bind('set convert-meta off')   # 不转换 meta 键
except ImportError:
    # ↑ 如果 import readline 失败(Windows 没这个模块)，就跳过，不报错。
    pass
    # ↑ pass 是"什么都不做"的占位语句。Python 语法要求 except 后必须有内容，
    #   这里我们确实什么都不用做，所以写 pass。

from anthropic import Anthropic
# ↑ from 包 import 类:从 anthropic 这个第三方包里，只引入 Anthropic 这个类。
#   anthropic 是 Anthropic 公司的官方 Python SDK(软件开发工具包)。
#   安装: pip install anthropic
#   Anthropic 类是用来调用 Claude API 的客户端。

from dotenv import load_dotenv
# ↑ 从 python-dotenv 包引入 load_dotenv 函数。
#   这个包的作用:读取 .env 文件里的配置，塞进环境变量。
#   安装: pip install python-dotenv

load_dotenv(override=True)
# ↑ 读取当前目录的 .env 文件，把里面的 KEY=VALUE 写入 os.environ。
#   override=True:如果环境变量已存在，用 .env 里的值覆盖它。
#   .env 文件通常存 API 密钥等敏感信息(已 gitignore，不上传到 git)。

if os.getenv("ANTHROPIC_BASE_URL"):
    # ↑ os.getenv("XXX"):读环境变量 XXX，不存在返回 None(空)。
    #   if None: 条件为假，跳过。if 有值: 条件为真，执行下面。
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
    # ↑ 如果设置了自定义 API 地址(如 GLM/Kimi/DeepSeek 兼容端点)，
    #   就把 ANTHROPIC_AUTH_TOKEN 这个环境变量删掉(pop 第二个参数 None
    #   表示"不存在也不报错")。这是为了兼容第三方提供商，避免认证冲突。

client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
# ↑ 创建 Anthropic 客户端对象，赋值给变量 client。
#   base_url 参数:API 地址。os.getenv 读 ANTHROPIC_BASE_URL，
#   如果没设这个环境变量，返回 None，SDK 会用默认官方地址。
#   之后用 client.messages.create(...) 调用 Claude API。
MODEL = os.environ["MODEL_ID"]
# ↑ 从环境变量读模型 ID(如 "claude-sonnet-4-5")。
#   os.environ["KEY"] 和 os.getenv("KEY") 的区别:
#   - os.environ["KEY"]: 不存在会抛 KeyError 崩溃。
#   - os.getenv("KEY"): 不存在返回 None，不崩溃。
#   这里用 os.environ[] 是故意的:MODEL_ID 是必填项，没设就该报错。

SYSTEM = f"You are a coding agent at {os.getcwd()}. Use bash to solve tasks. Act, don't explain."
# ↑ f"..." 是 f-string(格式化字符串):用 {表达式} 把变量的值嵌进字符串。
#   os.getcwd() 返回当前工作目录路径(如 /home/user/project)。
#   所以 SYSTEM 实际值类似: "You are a coding agent at /home/user/project. ..."
#   SYSTEM 是"系统提示词":告诉模型它的身份和工作环境。每次 API 调用都会带上。

# ── Tool definition: just bash ────────────────────────────
# ↓ TOOLS 是一个"工具列表"，告诉模型"你能调用哪些工具"。
#   格式是 JSON Schema(一种描述数据结构的标准)。模型看了这个声明才知道怎么调用。
TOOLS = [{
    # ↑ [{...}] 是"列表里放一个字典"。列表用 []，字典用 {key: value}。
    "name": "bash",                     # 工具名:模型调用时用这个名字
    "description": "Run a shell command.",  # 描述:告诉模型这个工具能干嘛
    "input_schema": {                   # 输入模式:描述这个工具接受什么参数
        "type": "object",               # 参数整体是个对象(JSON 的 object = Python 的 dict)
        "properties": {                 # 定义每个参数
            "command": {"type": "string"}  # 参数名 command，类型 string(字符串)
        },
        "required": ["command"],        # command 是必填参数
    },
}]


# ── Tool execution ────────────────────────────────────────
# ↓ def 定义函数。语法: def 函数名(参数: 类型提示) -> 返回类型:
#   command: str 表示参数 command 应该是字符串(str)。
#   -> str 表示函数返回字符串。这只是"提示"，Python 不强制检查。
def run_bash(command: str) -> str:
    # ↑ 这是"工具的执行器":模型说"帮我跑这个命令"，这里真正执行它。
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    # ↑ 一个"危险命令"黑名单列表。防止模型执行会搞坏系统的命令。
    if any(d in command for d in dangerous):
        # ↑ any(...):只要里面有一个为 True，就返回 True。
        #   d in command for d in dangerous 是"生成器":遍历 dangerous 列表，
        #   检查每个危险词 d 是否出现在 command 里。
        #   等价于: for d in dangerous: if d in command: return True
        return "Error: Dangerous command blocked"
        # ↑ 命中黑名单，返回错误，不执行。
    try:
        r = subprocess.run(command, shell=True, cwd=os.getcwd(),
        # ↑ subprocess.run: 执行外部命令，返回一个结果对象(存到 r)。
        #   command: 要执行的命令字符串(如 "ls -la")。
        #   shell=True: 通过 shell 解析命令(支持管道 |、重定向 > 等)。
        #   cwd=...: 在哪个目录执行(Change Working Directory)。
                           capture_output=True, text=True, timeout=120)
        #   capture_output=True: 捕获输出(stdout 和 stderr)，不打印到屏幕。
        #   text=True: 输出以文本(字符串)返回，而非字节。
        #   timeout=120: 最多跑 120 秒，超时强制杀掉(防止命令卡死)。
        out = (r.stdout + r.stderr).strip()
        # ↑ r.stdout 是标准输出(正常结果)，r.stderr 是标准错误(报错信息)。
        #   + 拼接两段字符串。.strip() 去掉首尾空白字符(空格、换行)。
        return out[:50000] if out else "(no output)"
        # ↑ A if 条件 else B 是"三元表达式"(一行写 if-else)。
        #   out[:50000]:切片，只取前 50000 个字符(防止输出太长撑爆上下文)。
        #   [:N] 语法:从开头取 N 个。详见列表/字符串切片。
        #   if out: 如果 out 非空(有内容)。else: 如果为空返回 "(no output)"。
    except subprocess.TimeoutExpired:
        # ↑ 如果命令超时(120 秒)，subprocess 会抛 TimeoutExpired 异常。
        return "Error: Timeout (120s)"
    except (FileNotFoundError, OSError) as e:
        # ↑ 捕获多种异常: FileNotFoundError(命令不存在)、OSError(系统错误)。
        #   as e: 把异常对象存到变量 e，方便打印错误信息。
        return f"Error: {e}"
        # ↑ f-string:把异常信息嵌进字符串返回。


# ── The core pattern: a while loop that calls tools until the model stops ──
# ↓ ★★★ 这是整个教程的核心: agent loop(智能体循环)。★★★
def agent_loop(messages: list):
    # ↑ messages 是"对话历史"，一个列表。每条消息是一个字典。
    #   格式: [{"role": "user", "content": "..."}, {"role": "assistant", "content": ...}, ...]
    #   role 有两种: "user"(用户说的话)、"assistant"(模型回的话)。
    while True:
        # ↑ while True: 无限循环。True 永远为真，会一直转，直到遇到 return 才退出。
        #   return 会立即结束整个函数(从而结束循环)。
        response = client.messages.create(
            # ↑ ★ 调用 Claude API。client.messages.create 是核心方法。
            #   返回的 response 对象包含模型的回复。
            model=MODEL, system=SYSTEM, messages=messages,
            #   model: 用哪个模型(如 claude-sonnet-4-5)。
            #   system: 系统提示词(模型身份/规则)。
            #   messages: 对话历史(模型根据它理解上下文)。
            tools=TOOLS, max_tokens=8000,
            #   tools: 允许模型调用的工具列表。
            #   max_tokens: 模型最多生成多少 token(约=字数)。8000 够长了。
        )

        # Append assistant turn
        messages.append({"role": "assistant", "content": response.content})
        # ↑ messages.append(...):往 messages 列表末尾添加一条消息。
        #   把模型的回复(response.content)作为 assistant 消息存进历史。
        #   这样下一轮 API 调用时，模型能看到自己上一轮说了什么。

        # If the model didn't call a tool, we're done
        if response.stop_reason != "tool_use":
            # ↑ response.stop_reason: 模型为什么停止生成。
            #   "tool_use": 模型想调用工具(还没说完，要继续)。
            #   "end_turn": 模型说完了(正常结束)。
            #   "max_tokens": 达到长度上限被截断。
            #   != 是"不等于"。如果停止原因不是"调用工具"，说明模型答完了。
            return
            # ↑ 结束函数，退出循环。agent_loop 完成。

        # Execute each tool call, collect results
        results = []
        # ↑ 准备一个空列表，用来收集这一轮所有工具的执行结果。
        for block in response.content:
            # ↑ response.content 是一个"内容块列表"。模型一条回复可能含多个块:
            #   - text 块: 普通文字
            #   - tool_use 块: 工具调用请求
            if block.type == "tool_use":
                # ↑ block.type: 这个块的类型。只处理"工具调用"块，跳过文字块。
                print(f"\033[33m$ {block.input['command']}\033[0m")
                # ↑ print(...): 打印到屏幕。
                #   \033[33m...\033[0m 是 ANSI 转义码，让终端显示黄色文字。
                #   \033[33m = 开始黄色，\033[0m = 重置颜色。
                #   block.input['command']: 模型要执行的命令(从工具调用块取参数)。
                output = run_bash(block.input["command"])
                # ↑ 调用 run_bash 真正执行命令，结果存到 output。
                print(output[:200])
                # ↑ 打印输出前 200 个字符(只看预览，完整结果会喂给模型)。
                results.append({
                    "type": "tool_result",
                    # ↑ 标记这是个"工具结果"消息块。
                    "tool_use_id": block.id,
                    # ↑ block.id: 这次工具调用的唯一编号。
                    #   tool_result 必须用同一个 id 关联到对应的 tool_use，
                    #   模型才知道"这个结果对应我刚才哪次调用"。
                    "content": output,
                    # ↑ 工具的实际输出。
                })

        # Feed tool results back, loop continues
        messages.append({"role": "user", "content": results})
        # ↑ ★ 把工具结果作为"user"消息喂回历史。为什么是 user 不是 tool?
        #   因为 Anthropic API 的对话格式只有 user/assistant 两种角色。
        #   工具结果从"非模型"一方提供，所以归到 user。
        #   content 是个列表(可以有多个 tool_result 块)。
        #   然后 while 循环回到顶部，带着新历史再次调用模型——模型看到结果，继续。


# ── Entry point ──────────────────────────────────────────
if __name__ == "__main__":
    # ↑ __name__ 是 Python 内置变量。直接运行本文件时它等于 "__main__"，
    #   被别人 import 时等于模块名(如 "code")。
    #   这个 if 的作用:只有直接运行才执行下面的代码，被导入时不执行。
    print("s01: Agent Loop")
    print("输入问题，回车发送。输入 q 退出。\n")
    # ↑ \n 是换行符，这里表示多空一行。

    history = []
    # ↑ 对话历史，空列表。整个会话共享这一个列表(多轮对话靠它维持上下文)。
    while True:
        # ↑ 外层循环:反复接收用户输入，每输入一次就跑一轮 agent_loop。
        try:
            query = input("\033[36ms01 >> \033[0m")
            # ↑ input("提示符"): 在终端显示提示符，等待用户输入，回车后返回字符串。
            #   \033[36m...\033[0m: 青色提示符。
        except (EOFError, KeyboardInterrupt):
            # ↑ EOFError: 输入流结束(如 Ctrl+D)。
            #   KeyboardInterrupt: 用户按 Ctrl+C 中断。
            break
            # ↑ break: 跳出当前 while 循环(结束程序)。
        if query.strip().lower() in ("q", "exit", ""):
            # ↑ .strip(): 去掉首尾空格。.lower(): 转小写。
            #   in (元组): 检查是否在元组里(元组用 ()，类似列表但不可改)。
            #   匹配 q/exit/空输入就退出。
            break
        history.append({"role": "user", "content": query})
        # ↑ 把用户问题作为 user 消息加入历史。
        agent_loop(history)
        # ↑ 调用 agent_loop——模型可能调用多次工具，最后给出文字回答。
        #   agent_loop 会修改 history(往里加 assistant 和 tool_result 消息)。
        # Print the model's final text response
        response_content = history[-1]["content"]
        # ↑ history[-1]: 列表"负索引"，取最后一个元素。-1=最后一个，-2=倒数第二个。
        #   agent_loop 结束时最后一条是 assistant 消息(模型的最终文字回答)。
        if isinstance(response_content, list):
            # ↑ isinstance(x, 类型): 检查 x 是不是该类型。
            #   content 可能是列表(多个块)或字符串，这里处理列表情况。
            for block in response_content:
                if getattr(block, "type", None) == "text":
                    # ↑ getattr(对象, 属性名, 默认值): 安全地取对象属性。
                    #   如果 block 没有 type 属性，返回 None(不报错)。
                    #   这里找文字块并打印。
                    print(block.text)
        print()
        # ↑ 空打印，输出一个换行(让界面整洁)。
