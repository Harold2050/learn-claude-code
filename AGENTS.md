# AGENTS.md

供 OpenCode 会话阅读的本仓库指引。读一遍即可上手。中文是本仓库的源语言（`README.md` 为中文，`README.en.md` / `README.ja.md` 为翻译）。

## 这个仓库是什么

`learn-claude-code` 是一个**教学仓库**，主题是 *harness engineering*（线缆/外壳工程）——即围绕一个 LLM 搭建运行环境（工具、知识、上下文、权限），仿照 Claude Code 的结构。**代码是 harness（外壳），模型才是 agent（智能体）。**

核心理解（理解一切的前提）：
- **Agent = 模型 + harness。** 智能（能动性）来自模型训练，不来自代码编排。harness 给模型"手脚、眼睛、工作台"。
- **循环是恒定的，机制是变化的。** 全部 20 章都在同一个 `while` 循环上加东西，循环本身从未改变。学习时要盯住"这一章在循环的哪个位置插入了什么"。

## 不变的核心模式（一切章节的基准）

每一章的教学代码都在同一个 agent loop 上叠加一个新机制：

```python
def agent_loop(messages):
    while True:
        response = client.messages.create(model=MODEL, system=SYSTEM, messages=messages, tools=TOOLS, ...)
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":   # 模型决定停下
            return
        results = []
        for block in response.content:
            if block.type == "tool_use":
                output = TOOL_HANDLERS[block.name](**block.input)   # 执行工具
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})
        messages.append({"role": "user", "content": results})       # 喂回，继续循环
```

记忆口诀：**模型决定何时调用工具、何时停止；代码只负责执行模型要求的，并把结果喂回去。** 后面每一章只是改 `TOOLS` / `TOOL_HANDLERS` / 在循环前后插一段处理，循环主体不动。

## 两条赛道——不要混

仓库有两条并行的课程线，**章号在两条线之间不对应**：

- **现行 / 规范赛道（所有改动都打这里）**：根目录 `s01_agent_loop/` … `s20_comprehensive/`。每章含 `code.py` + `README.md`(中) + `README.en.md`(英) + `README.ja.md`(日) + 可选 `images/`。
- **旧赛道（为旧链接和 web 应用保留，未经要求不要改）**：`agents/`（可运行的 `sNN_*.py` + 合集 `s_full.py`）和 `docs/{en,zh,ja}/`（旧 12 课）。

跨线迁移前，务必对照 `README.md` 的 "Legacy-to-Current Mapping" 表确认主题，**永远不要只按章号在两条线之间搬改动**。两线技术细节也会漂移（旧线 `s_full.py` 用 `TASK_MGR`/`TEAM`/`BUS` 类，新线 s12+ 用裸函数 + `MessageBus`）。

## 每章到底讲了什么（按代码实读总结）

| 章 | 一句话机制 | 循环怎么变 |
|---|---|---|
| **s01** Agent Loop | 一个 `while True` + 一个 bash 工具 = 一个 agent | 这是循环本身 |
| **s02** Tool Use | 加 read/write/edit/glob 四工具；**引入 `TOOL_HANDLERS` 分发映射**（查表替代 s01 硬编码 `run_bash`）+ `safe_path` 防路径逃逸 | 工具执行那行从 `run_bash(...)` 变成 `TOOL_HANDLERS[block.name](**block.input)` |
| **s03** Permission | 工具执行前插**三道闸门**：黑名单 deny / 规则匹配 / 用户审批 | 循环里加一行 `if not check_permission(block): continue` |
| **s04** Hooks | 把 s03 的权限逻辑**搬出循环、搬到钩子上**。`HOOKS` 字典 + `register_hook`/`trigger_hooks`，事件点 `UserPromptSubmit`/`PreToolUse`/`PostToolUse`/`Stop`；`trigger_hooks` 返回非 None 即拦截 | 循环里 `check_permission` 换成 `trigger_hooks("PreToolUse", block)` |
| **s05** TodoWrite | 加 `todo_write` 工具（**只规划不执行**）+ `rounds_since_todo` 计数，**3 轮没更新就注入 `<reminder>` 提醒**（唾面自干式 nag）。`SYSTEM` 加 "plan before execute" | 循环开头加 nag 判断；`todo_write` 走 `TOOL_HANDLERS` 自动派发 |
| **s06** Subagent | 加 `task` 工具 + `spawn_subagent`：**开全新 messages[]（上下文隔离）**，最多 30 轮，**只把最终摘要返回，中间历史全丢弃**；子代理**没有 task 工具**（防递归）；子代理也跑 hooks（权限继承） | `task` 走 `TOOL_HANDLERS`，循环不变 |
| **s07** Skill Loading | **两层按需知识**：L1 启动扫 `skills/` 把 name+description 注入 SYSTEM（便宜~100 token）；L2 模型调 `load_skill(name)` 才读全文（贵~2000 token）。`SKILL_REGISTRY` 查表，**不走路径遍历** | `load_skill` 走 `TOOL_HANDLERS`，循环不变 |
| **s08** Context Compact | **四层压缩 + 紧急**：L3 `tool_result_budget`（大结果持久化到磁盘）→ L1 `snip_compact`（裁中间保头尾，**且保护 tool_use/tool_result 配对不被拆散**）→ L2 `micro_compact`（旧 result 占位符）→ L4 超 `CONTEXT_LIMIT` 则 LLM 摘要并写 transcript；`reactive_compact`：API 报 `prompt_too_long` 时紧急裁尾。**原则：便宜先做，贵的最后** | 循环开头加 `tool_result_budget → snip → micro → (超限则 compact)`，API 调用包 try/except |
| **s09** Memory | 跨会话持久记忆：`.memory/MEMORY.md`(索引) + 单文件(YAML frontmatter)。三子系统：**选**(按相关度挑) / **抽**(每轮结束从**压缩前快照**提取新记忆，保证保真) / **整**(超 10 个做 Dream 去重)。索引入 SYSTEM，内容注入当前 user turn | 循环前注入记忆、返回前 `extract_memories`+`consolidate` |
| **s10** System Prompt | **运行时组装而非硬编码**：`PROMPT_SECTIONS` 按 context 选段拼接；`get_system_prompt` 用 `json.dumps(context)` 当 key 做缓存；记忆段按真实状态（MEMORY.md 是否存在）加载，不是按关键词 | LLM 调用改用 `get_system_prompt(context)`；每轮工具后 `update_context` |
| **s11** Error Recovery | **三条恢复路径 + 指数退避**。Path1 `max_tokens`→先升级 8K→64K（**第一次不 append 截断输出**），再 continuation prompt(≤3)；Path2 `prompt_too_long`→reactive compact(1 次)；Path3 429/529→退避抖动 + 连续 529 切 `FALLBACK_MODEL_ID`。`with_retry` 包瞬时错误，`RecoveryState` 跟踪状态 | LLM 调用包 `with_retry` + 外层 try/except + max_tokens 分支 |
| **s12** Task System | **文件持久化任务图 + blockedBy 依赖**。`Task` dataclass 存 `.tasks/task_*.json`；`can_start` 查依赖全完成（missing 当阻塞）；`claim_task` 设 owner+in_progress，`complete_task` 报告解锁的下游。5 个任务工具。**本章故意省略 s11 的完整错误恢复，专注任务系统** | 循环保持简单（`try` 里错误处理被简化）；任务工具走 `TOOL_HANDLERS` |
| **s13** Background Tasks | **线程异步执行 + 通知注入**。`threading.Thread` 守护线程跑慢操作；`should_run_background`（模型显式 `run_in_background` 优先，否则 `is_slow_operation` 启发式：install/build/test 等）；完成后用 `<task_notification>` XML（**不复用 tool_use_id**）注入下一轮 user 消息；`background_lock` 保线程安全 | 循环里工具执行前判断是否后台；结尾合并 `results` + background 通知为一条 user 消息 |
| **s14** Cron Scheduler | **独立守护线程 + 队列**。四层：调度线程(1s 轮询 `cron_matches`) → `cron_queue`(解耦) → 队列处理器(idle 时 `agent_lock` 唤醒) → consumer(`agent_loop` 注入 `[Scheduled]`)。5 字段 cron，DOM/DOW 同时受限用 **OR 语义**；日期感知 marker 防每日任务跨日重复；durable 持久化到 `.scheduled_tasks.json` | 循环开头 `consume_cron_queue()` 注入；外层 `main` 用事件循环 + 队列处理器线程 |
| **s15** Agent Teams | `MessageBus`（文件邮箱 `.mailboxes/*.jsonl`，**读即销毁** read+unlink）+ `spawn_teammate_thread`（后台线程跑自己的简化 loop，**最多 10 轮**，只有 bash/read/write/send_message）。Lead 工具：`spawn_teammate`/`send_message`/`check_inbox`。`inbox_poller` + 事件队列把 teammate 结果和 background 结果喂给 Lead。`peek` 非破坏性检查 | `main` 改成 `input_reader` + `inbox_poller` 双线程喂一个 `events` 队列 |
| **s16** Team Protocols | **请求-响应协议 + request_id 关联 + 状态机**。`ProtocolState`(pending/approved/rejected)；`match_response` 用 request_id 关联并校验类型；shutdown 握手、plan 审批；teammate 改用 **idle loop**（等 inbox 而非退出）。`consume_lead_inbox` 统一收件（协议响应先路由）。**注：plan 是协议级而非代码级 gate** | teammate 线程内改成 idle 等消息循环；Lead 加协议工具 |
| **s17** Autonomous Agents | **自组织——teammate 主动从任务板认领**。`scan_unclaimed_tasks` 找无主且依赖完成的 pending；`idle_poll`(60s 内每 5s) 找 inbox/任务板/协议消息，返回 work/shutdown/timeout。**无需 Leader 一个个分配** | teammate 线程内把退出改成 `idle_poll` 循环 |
| **s18** Worktree Isolation | **git worktree + 任务-目录绑定 + 事件日志**。`Task` 加 `worktree` 字段；`create_worktree`(`git worktree add -b wt/name`) + `validate_worktree_name`(防路径遍历)；`remove` 前安全检查（未提交改动需 `discard_changes=true`），**不自动完成任务**；`events.jsonl` 记录生命周期；teammate 绑定后在其 worktree cwd 跑命令 | 加 3 个 worktree 工具；execute 时按绑定切 cwd |
| **s19** MCP Plugin | `MCPClient`(**教学用 mock**) 发现并调用工具；工具命名 `mcp__{server}__{tool}`，`normalize_mcp_name` 过滤非法字符；`assemble_tool_pool` 把 builtin + 所有 MCP 工具**合并成一个池**；readOnly/destructive 注解；`connect_mcp` 连接发现工具；模拟 docs/deploy 两个服务器 | 循环每轮 `tools, handlers = assemble_tool_pool()`（动态池，无 prompt cache） |
| **s20** Comprehensive | **所有机制回到一个循环**。动态工具池 + `prepare_context` 每轮过压缩管线 + `call_llm`(with_retry + max_tokens 升级 + reactive compact) + 注入 cron/background 通知 + todo nag + hooks。这是终点章，把 s02-s19 拼回去 | 循环主体整合所有步骤 |

## "看似 bug 的简化"是刻意的（不要去"修"）

CONTRIBUTING.md 明确要求保持教学代码最简。以下是真实存在的、**故意为之**的简化，**修改前先问**：

- s12 / s13 / s14 / s15 等章的 docstring 直白声明：**为聚焦新主题，省略了 s11 的完整错误恢复**（`RecoveryState`/退避/升级/响应式压缩/fallback）。它们的循环只保留最简 `try/except`。
- s15 teammate **硬上限 10 轮**（真实 CC 用 idle loop）。
- s16 plan 审批是**协议级而非代码级 gate**：teammate 提交后线程继续跑，靠模型"等批准再动手"，代码层并不阻塞工具派发。
- `MessageBus` **没有文件锁**（真实 CC 用 `proper-lockfile`）；读即 `read_text` + `unlink`。
- s19 MCP 是 **mock**（不连真实传输/OAuth，两个假服务器）。
- s05–s09 的 s08/s09 与后续章的压缩函数**签名不同**（`snip_compact` 的 `max_messages=` vs s09 的 `mx=`），测试 `test_compaction_tool_pairs.py` 正是用这个差异来分别驱动两套实现——**不要统一**。

## 运行代码（最重要的坑）

**永远从仓库根目录运行章节，不要 cd 进章节目录：**

```sh
python s07_skill_loading/code.py        # 正确
# 错误：cd s07_skill_loading && python code.py   # 会找不到 skills/
```

原因：每章在 import 时设 `WORKDIR = Path.cwd()`，然后相对它解析 `skills/`、`.tasks/`、`.teams/`、`.mailboxes/`、`.worktrees/`、`.transcripts/`、`.task_outputs/`。从子目录运行会**静默**破坏技能加载、任务持久化和 worktree 隔离。

### 环境变量（必填）

```sh
cp .env.example .env   # 然后填两个必填项
```

- `ANTHROPIC_API_KEY` —— 必填。
- `MODEL_ID` —— 必填（如 `claude-sonnet-4-6`）。
- `ANTHROPIC_BASE_URL` —— 可选，用于 Anthropic 兼容提供商（GLM/智谱、Kimi/月之暗面、MiniMax、DeepSeek）。**一旦设置，每章都会 `pop` 掉 `ANTHROPIC_AUTH_TOKEN`——这是故意的，不要去"修"**。
- `FALLBACK_MODEL_ID` —— 可选，s11/s20 错误恢复连续 529 时切换。

`.env` 已 gitignore，不要提交。

## 测试

```sh
python -m pytest tests -q                       # 全部；CI 在 Python 3.11 上跑的就是这条
python -m pytest tests/test_compaction_tool_pairs.py -q   # 单个文件
```

- **测试不需要 API key**：它们 stub 掉 `anthropic`/`dotenv`/`yaml`，用 `importlib` 把模块加载进临时目录并 `chdir` 到那里。不要加真实网络调用。
- 测试横跨两条赛道：`test_agents_smoke.py` 对每个 `agents/*.py` 做 py_compile；`test_compaction_tool_pairs.py`、`test_todo_write_string_input.py`、`test_s_full_background.py` 加载并驱动现行 s05–s09、s20 + 旧线 `s_full`。
- **没有 Python 的 lint/format/typecheck 步骤**。验证 = 测试 +（web 侧）build。

## CI（`.github/workflows/`）

- `test.yml → python-smoke`：`pip install -r requirements.txt pytest` 然后 `python -m pytest tests -q`。
- `test.yml → web-build` 和 `ci.yml`：在 `web/` 里 `npm ci` 然后 `npm run build`（会触发 `prebuild` 钩子）。

## web/（Next.js 16 + React 19，TypeScript）

- dev 和 build 都跑 `pre*` 钩子：`npm run extract` 执行 `tsx scripts/extract-content.ts`，它扫描**仓库根目录**（旧 `docs/`+`agents/`，以及根 `sNN_topic/` 章节）并重新生成 `web/src/data/generated/`。
- **永远不要手改 `web/src/data/generated/`**——它会被 extract 步骤覆盖。要改就改源章节。
- 用 `web/package-lock.json`；根目录的 `/package-lock.json` 已 gitignore。

## 依赖与文件清单（根清单）

- 唯一的 Python 清单是 `requirements.txt`：`anthropic`、`python-dotenv`、`pyyaml`。
- **没有** `pyproject.toml`/`setup.cfg`/`ruff`/`mypy`/`conftest.py`/`tox.ini`。
- `skills/` 下有 4 个真实技能（`agent-builder`、`code-review`、`mcp-builder`、`pdf`），供 s07 加载；每个是 `<name>/SKILL.md`，YAML frontmatter（`name`/`description`）+ 正文。

## 约定（CONTRIBUTING.md 强制）

- **保持教学代码最简。** 不要给章节加生产级硬化、防御解析、错误处理层、抽象或测试框架，除非这一章本身就在讲这个。
- **三语同步是硬要求。** 任何对章节 `code.py` 或 README 的改动，必须同步到该章的 `README.md`(中) + `README.en.md`(英) + `README.ja.md`(日)，且三份里的代码块完全一致。
- **改现行赛道**（`sNN_topic/`），不要改旧镜像。
- PR 要对应具体 issue、只解决一件事、声明 AI 协助。
- 某处"像 bug"的简化，先假设是故意的——改之前先问。

## 运行时产物（自动生成、已 gitignore）

章节会把状态写到运行目录（正确运行时即仓库根目录）的这些目录/文件里，删除它们即可重置一次会话：

`.tasks/`（s12+ 任务 JSON）、`.task_outputs/tool-results/`（s08 大结果）、`.teams/` 与 `.team/`（旧线/新线团队）、`.mailboxes/`（s15+ 邮箱 JSONL）、`.memory/`（s09 记忆）、`.worktrees/`（s18 git 工作树 + `events.jsonl`）、`.transcripts/`（s08 压缩存档 JSONL）、`.scheduled_tasks.json`（s14 cron 持久化）。

---

## 学习者工作流（本仓库所有者定制，重要）

本仓库正被一位**完全的初学者**系统学习，目标是从零搭建 agent 知识体系（Python 基础也有限）。后续会话在被要求"讲解这个项目"时，必须遵守以下长期要求：

1. **详尽讲解，不要跳步。** 学习者请你解释某段代码或某节时，要逐行/逐概念走。**不要假设**学习者懂 Python 语法、常见库、或 agent 内部概念。术语第一次出现时要给出定义。
2. **每节交付四部分：** (a) 详细的代码讲解；(b) 提炼这一节的核心思想（一句话/几句话）；(c) 背后的设计理由 / "为什么这样设计"；(d) 引申思考 / 相关延伸，拓宽理解。
3. **产出学习笔记** 并存入 Obsidian 仓库：
   `/mnt/d/ProgramData/Obsidian-Note/Harold笔记仓库/learn-claude-code/`
   - 按 s01 → s20 章节顺序，**每次学习只做一章的笔记**。
   - 用**中文**撰写，Obsidian 友好的 Markdown 风格（善用 `[[ ]]` 双链、标题层级、代码块）。
4. **搭知识体系，而不是堆孤立事实。** 把新概念与前面章节、以及恒定的核心模式（agent loop）串起来。让"每章都在循环某处加了什么"这个递进显式化，让后一课强化前一课。

如果上述要求在某个具体请求下有歧义（比如：笔记确切存成哪个文件名、讲到多深），**动手前先问**。

讲解时，下表的"循环怎么变"列是天然的讲解骨架——每章先讲新机制，再定位它插在循环的哪个位置。
