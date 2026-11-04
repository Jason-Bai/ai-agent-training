# F1-O1：Claude Code 原始独立审查报告

> 本项第 1 轮，直接调用本机 Claude Code，仅 Read；正常退出 0，`is_error=false`、`stop_reason=end_turn`。全部 23 个源码/测试文件在实际读取范围内，源码、测试、示例、忽略规则及依赖文件审查前后 SHA-256 一致。
> 审查清单摘要指纹：`ac933ecd2bf7c0de4e6a702aaaddbac4f4b068b9beef0901da6b39ad31c1d816`（按相对路径排序的 SHA-256 映射 JSON）。本报告为工具原文；核实与处置见 [测试方案](test-plan.md#36-独立审查)。

全部指定文件已读完。以下是第 1 轮独立静态审查报告。

# F1-O1 第 1 轮独立代码审查报告

**审查方式**：只读静态审查（仅使用 Read），未运行任何命令或测试，未读取任何 `.env`/凭据。测试执行结果采信执行者提供的 `test-results.json`（4 条最终 pytest 记录）与测试方案 3.4 的 Red/Green/`uv sync` 叙述；**Red 阶段输出与 `uv sync --locked` 无独立运行证据，仅核对其静态合理性**。stitch MCP 连接失败与本审查无关。

## 一、变更与现状核对（全部通过）

1. **diff 与磁盘文件一致**：`src/main.py`、`settings.py`、`middleware/request_context.py`、`providers/factory.py`、`services/*`、全部测试文件与文档的当前内容均与 diff 的 after 状态逐一吻合，未发现差异。
2. **Middleware 原样抽取**：`middleware/request_context.py:10-49` 与 diff 中旧 `main.py` 内嵌类逐行比对，逻辑、收口条件（`response_completed` 判定、`finally` 收口 `state.get("error_code")`）、非 HTTP 透传、异常兜底完全一致，仅 imports 从 `api.errors`/`services.trace` 引入。
3. **服务迁移完整**：`src/` 与 `tests/` 中无残留 `service.` 导入；`service/gateway.py`、`prompts.py`、`trace.py` 按 diff 删除，`services/` 三个文件内容逐行等于原文件。
4. **无导入副作用**：`main.py:12-23` 无模块级 `app`、无模块级 `load_settings()`/`create_provider()` 调用；两个隔离子进程测试（`tests/unit/test_app_factory.py:171-212`）以非法 ENV 环境变量 + 构造探针/禁入探针双路径证明，方法有效（子进程环境显式替换，不触碰开发环境）。
5. **配置契约**：`settings.py:8` `DEFAULT_ENV_FILE` 基于 `__file__` 计算，与 cwd 无关；`settings.py:14-15` 两个独立 Literal 字段、别名 `ENV`/`LLM_GATEWAY_PROVIDER`、默认值 `development`/`mock`、`frozen=True`、`hide_input_in_errors=True` 与设计 3.1/3.2 一致。优先级 进程环境 > `.env` > 默认值 由 pydantic-settings 语义保证，且高优先级非法值直接失败的用例覆盖（`tests/unit/test_settings.py:32-43`、`tests/unit/test_app_factory.py:59-73`）。
6. **工厂与注入**：`main.py:13-14` 先校验配置再选择 Provider，显式 Settings 跳过 `load_settings`、显式 Provider 跳过工厂、注入也不能绕过配置校验（`tests/unit/test_app_factory.py:43-56、59-73、76-94`），与设计 2.2 流程图一致。
7. **测试隔离**：`tests/conftest.py:12-16` autouse 清理两项进程变量；所有普通测试显式 `Settings(..., _env_file=None)`；涉及文件的测试全部用 `tmp_path` 或 `monkeypatch.setattr(main, "load_settings", ...)` 指到临时路径；smoke 将真实 `src` 复制进临时目录使 `DEFAULT_ENV_FILE` 落到临时 `.env`（`tests/smoke/conftest.py:8-14`）。**静态核验没有一条测试会读取开发者真实根目录 `.env` 或依赖真实进程变量**。
8. **25 组验收映射**：test-plan 3.5 的 O1-01~O1-25 与测试函数一一对应且断言覆盖设计要点（含 O1-05/06 的 8 种非法值、O1-14 两个子进程、O1-20 的 start 后异常/最终 body 发送失败/完整收口三例、O1-23 私有值不泄露）；数量核对：unit 19+20+4+5=48、integration 64、smoke 3，合计 115，与 JSON 输出一致；原 70 场景 = 5 unit + 64 integration + 1 smoke，原 21 个测试函数的 assert 仅做装配性调整（导入路径、`app_settings` 注入、monkeypatch 字符串），未放宽。
9. **依赖与锁文件**：`uv.lock` 中 pydantic-settings 2.16.2、python-dotenv 1.2.4 与 test-plan 3.4 记录一致；`pyproject.toml` 与 `uv.lock` 静态核对相互一致（各 specifier 均被锁版本满足），`uv sync --locked` 成功的结论静态上成立。
10. **文档同步**：README（结构图、启动命令、配置说明）、AGENTS.md（新增边界条款、状态行）、架构表、progress 均与实现一致；`.env.example` 内容与设计 3.1 完全一致；`.gitignore` 新增 `.env`；原 F1 三份文档保留历史快照（`docs/features/F1/design.md` 中的 `main:app`/`service/` 引用属有意保留，符合 O1 设计 3.3 的说明）。`services/prompts.py` 的 SYSTEM_PROMPT 与 F1 设计文档中确认文本逐字一致。
11. **HTTP/Trace 契约保持**：`api/routes.py`、`api/errors.py`、`contracts.py` 仅改导入；四字段信封、`X-Request-ID` 仅响应头、0/1 次调用、404/405 无模型 Trace、running 不收口为成功等断言全部原样保留。

## 二、发现（P0–P3）

**P0：无。P1：无。P2：无。**

**P3（不阻断，记录处理结论）：**

1. **状态标签不一致**（本次引入，文档）— `docs/features/F1/optimizations/O1/test-plan.md:3` 头标为「状态：执行中」，而同文件 3.3 表、`design.md:3`、`progress.md:11`、README:5 均表述为「本地验证完成，待独立审查」；且 `docs/AGENTS.md` 第 5 条定义的状态词表为「讨论中、待测试方案确认、开发中、待独立审查、验收通过、阻塞」，「执行中」不在词表内。建议改为「待独立审查」，审查完成后按约定翻转为「验收通过」。

2. **证据工件不完整**（证据记录）— 提供的 `test-results.json` 仅含 4 条最终 pytest 命令；Red 三阶段输出与 `uv sync --locked` 结果只存在于 test-plan 3.4 的叙述中，无原始输出工件。项目 AGENTS 要求「每次结论写明命令、结果、环境和未验证项」——叙述已覆盖命令与结果，满足最低要求；若能把 Red/同步输出的原始文本并入工件将更可核验。不影响验收。

## 三、现有问题 / 范围外提示（不属本项变更，不计阻断）

- `pyproject.toml:18` 的 dev 组含 `httpx2>=2.16.0`（锁文件对应 httpx2/httpcore2/truststore/httpx2-jsfetch）。diff 未触及 dev 组，属既有依赖；本项目测试仅导入 `httpx`，未使用 httpx2。与本项验收无关，建议后续单独确认是否有意保留。

## 四、未验证项（如实声明）

- 本审查为纯静态，**未独立运行**：115 个测试、三个 Red 阶段、`uv sync --locked`、真实进程冒烟均为执行者提供的证据，其真实性未经本次运行复核。
- 未能静态证明的事项已逐一说明依赖执行者证据：Red 2「11 failed 8 passed」与 Red 3「2 failed」的具体输出、子进程冒烟的实际退出码与日志。
- 未检查项目根实际 `.env`、用户配置目录或任何凭据（按指示排除）。

## 结论

实现与已确认设计、测试方案逐项相符：25 组验收场景均有对应可观察断言且映射准确；Middleware AST 原样抽取、services 迁移完整、无导入副作用、配置优先级/非法拒绝/显式注入/实例隔离均有双路径验证；测试不触碰开发者真实配置；依赖锁与文档同步一致。未发现违反已确认契约或项目约定的 P0–P2 项；2 项 P3 均为不影响验收的记录/文档问题，建议在后续轮次一并处置。

**VERDICT: APPROVED**（P0–P2 阻断项为 0，P3 记录处置：test-plan.md:3 状态标签改为「待独立审查」；证据工件可补充 Red 与 `uv sync` 原始输出）
