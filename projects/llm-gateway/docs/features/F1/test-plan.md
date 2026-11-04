# F1：统一入口与普通调用——测试方案

> 状态：验收通过。本地三层验证和 Claude Code 完整独立审查均通过；设计与测试方案均已获用户确认。
>
> 对应设计：[F1 设计方案](design.md)，已获用户整体确认。本方案的确认独立于设计确认；以下用例用于落实已确认的范围与通过标准。

## 1. 范围与通过标准

F1 只验证一次本地 Mock 普通文本调用：固定受控 System 模板、逻辑模型 `general-primary`、唯一一条 `user` 消息、统一响应/错误和最小内存 Trace。入口拒绝时 Provider 调用数为零；调用后失败时有一条失败尝试。F2–F8 的变量渲染、真实 Provider、结构化输出、SSE、取消、恢复和 SQLite 不在本方案内。

通过条件：下列确认后的全部必测用例及相关回归通过；响应、MockProvider 调用参数/次数与 Trace 一致；Claude Code 最多三轮独立审查后无 P0–P2 阻断项。Mock 和本地接口结果只证明相应层次，真实 Provider 在 F1 标为“未验证”。

测试按“准备确定输入 → 执行一次调用 → 断言响应与副作用”组织；各用例创建独立应用、MockProvider 与内存 Trace。Mock 成功文本固定为 `MOCK_REPLY`、Usage 为 `null`；故障分别使用受控 Provider 错误和带 `PRIVATE_ERROR_MARKER` 的程序异常。故障通过测试依赖注入，不能为此增加产品路由、请求参数或 HTTP 开关。HTTP 测试必须经过真实路由、请求校验和错误处理器，不能把 GatewayService 本身替换为固定响应。

## 2. 用例

| 编号 | 场景与输入 | 预期响应/错误 | Provider 调用参数/次数及其他副作用 | Trace 断言 | 测试层级 |
| --- | --- | --- | --- | --- | --- |
| F1-01 | `general-primary`、固定 `prompt`、一条非空 `user` 消息 | HTTP 200；正文只有 `success=true, code=ok, data, message`；`data` 有完整 `text`、`model=general-primary`、`actual_model=null`、`attempts=1`、未知 Usage 为 `null`；`X-Request-ID` 仅在响应头 | 恰好 1 次；受控 System 在前、唯一 `user` 消息在后 | 1 请求、1 成功尝试；请求 ID 与响应头一致，`provider=mock`、实际模型未知、次数一致 | 服务 + FastAPI 本地接口 |
| F1-02 | 未知字段、`variables`、`system`、`assistant` 或多条消息；分别执行 | `422/invalid_request`；四字段错误体，`success=false, data=null`，请求 ID 仅在响应头 | 每例 0 次 | 每例 1 失败请求、0 尝试；ID 与响应头关联 | 服务 + FastAPI 本地接口 |
| F1-03 | 缺必填字段、空消息、空/全空白内容或非法 JSON；分别执行 | `422/invalid_request`；同一四字段错误体及 `X-Request-ID` | 每例 0 次 | 每例 1 失败请求、0 尝试；无法可信解析字段时为 `null` | FastAPI 本地接口 |
| F1-04 | 未知模板、版本或逻辑模型；分别执行 | `400/unknown_prompt_template` 或 `400/unknown_model`；四字段错误体 | 每例 0 次 | 每例 1 失败请求、0 尝试；ID 与响应头关联 | 服务 + FastAPI 本地接口 |
| F1-05 | MockProvider 抛出受控调用故障 | `502/provider_error`，四字段错误体、`data=null`，无成功文本 | 恰好 1 次，不重试 | 1 失败请求、1 失败尝试；Usage 未知 | 服务 + FastAPI 本地接口 |
| F1-06 | Provider 调用中抛出未预期程序错误 | `500/internal_error`，四字段错误体不含异常文本/堆栈 | 恰好 1 次，不重试 | 1 失败请求、1 失败尝试；不记为 Provider 故障 | 服务 + FastAPI 本地接口 |
| F1-07 | 请求不存在的路径或错误 HTTP 方法 | `404/not_found` 或 `405/method_not_allowed`；四字段错误体和 `X-Request-ID` | 0 次 | 不建立 F1 模型调用 Trace | FastAPI 本地接口 |
| F1-08 | `content` 为 20,000 字符或 20,001 字符，分别执行 | 前者成功；后者 `422/invalid_request` | 前者 1 次；后者 0 次 | 前者 1 成功请求、1 尝试；后者 1 失败请求、0 尝试 | FastAPI 本地接口 |
| F1-09 | 合法内容含有首尾空格、代码缩进与换行 | 成功，传递原文 | 恰好 1 次；`user` 内容与输入逐字符一致 | 1 成功请求、1 尝试 | 服务 + FastAPI 本地接口 |
| F1-10 | 在 Provider 调用前模拟程序错误 | `500/internal_error`，统一安全错误体 | 0 次，不重试 | 1 失败请求、0 尝试 | 服务 + FastAPI 本地接口 |
| F1-11 | 连续请求及调用方自带 `X-Request-ID` | 每次返回不同的 Gateway UUID；调用方 ID 不被采用 | 每个合法请求各 1 次 | 每条记录与对应响应头 ID 一致，不覆盖已有记录 | FastAPI 本地接口 |
| F1-12 | 两个请求分别带不同正文，在异步 Mock 中交错完成 | 两个响应均正确、ID 不同 | 共 2 次；每次消息独立，原文不串到另一请求 | 2 请求、2 尝试，各自关联正确且终态只写一次 | 服务 + FastAPI 本地接口 |
| F1-13 | 单 worker 启动本地服务，真实 HTTP 提交合法请求、非法 JSON 和未知路径 | 分别为 200、422、404；四字段结构及 `X-Request-ID` 均正确 | 该层只从外部核验 HTTP，不声称读取到服务进程内 Trace | Trace 副作用由前述进程内测试证明，此处证明启动与实际 HTTP 通信 | 本地进程/回环 HTTP 冒烟 |
| F1-14 | 实施自检回归：调用中受控中断，未完成 HTTP 响应 | 中断继续传播；F1 不增加取消接口/终态 | 已开始 1 次尝试，无重试 | 请求和未完成尝试保持 running，不记成功；取消收口由 F6 交付 | 进程内 HTTP |

共同断言：HTTP 正文顶层字段恰好是 `success/code/data/message`；成功 `data` 的字段及类型符合设计，失败 `data=null`；所有相关 HTTP 响应有 UUID 格式的 `X-Request-ID`，JSON 正文不出现 request_id。失败输出不得原样暴露请求正文、System 文本、密钥标记或 `PRIVATE_ERROR_MARKER`/堆栈；Trace 不存储完整消息或输出。System 正文按设计中已确认文本逐字符断言传入 MockProvider；用户正文中的“忽略 System”等文字仅作为 `user` 内容传递，不改变实际消息角色或模板文本，Mock 测试不声称模型会服从规则。

F1-02/03 使用参数化用例，分别覆盖顶层、`prompt`、消息对象的额外字段；缺字段、错误容器类型、数字/null 代替字符串、非 user 角色、0 条/2 条消息、空字符串和全空白。`model=123` 必须为 422，不自动转成字符串；F1-04 用合法字符串验证未注册资源返回 400，并分别覆盖未知模型、模板名和版本。F1-08 同时覆盖 ASCII 与中文字符，避免将字符限制错误实现成 UTF-8 字节限制。F1-07 只验证统一错误外形，不要求模型调用 Trace。

## 3. 执行与结论

- 计划环境：项目独立 Python 虚拟环境，pytest 为测试入口；服务/HTTP 进程内用例通过 FastAPI 应用与替身依赖执行，启动冒烟使用本机回环 HTTP。测试不连接外部 Provider、不读取真实密钥；仅 F1-13 需要本地监听端口，结束后清理子进程。
- 计划命令（在项目根目录执行，环境建立后使用虚拟环境的 Python）：`python -m pytest tests/unit tests/integration -q` 验证逻辑与进程内 HTTP；`python -m pytest tests/smoke/test_http_server.py -q` 验证实际服务启动。具体依赖版本和测试文件在实施步骤中确定，不改变本方案的输入与预期结果。
- Red：已执行 `uv run python -m pytest tests/integration/test_http.py::test_success_uses_real_route_and_gateway tests/unit/test_core.py::test_request_rejects_numeric_model_without_coercion -q`，2 failed：空 FastAPI 应用返回 404 而非 200；空请求类型未拒绝数字模型名（DID NOT RAISE ValidationError）。均为断言失败，无导入错误，测试先于完整实现。环境 Python 3.14.7、FastAPI 0.162.0、Pydantic 2.14.2、pytest 9.2.0，精确依赖见 uv.lock。
- Green 与相关回归：`uv run python -m pytest tests/unit tests/integration -q` 为 68 passed；`uv run python -m pytest tests/smoke/test_http_server.py -q` 为 1 passed；退出码均为 0，无警告。前述 Red 的同一测试包含在 Green 中。13 组用例展开为 69 个实际测试，含参数化字段/临界值验证；真实进程测试启动并清理单 worker 本地服务。
- 实施自检回归：发现 finally 无条件收口会把未完成响应误记成功。先新增 `tests/integration/test_trace_integrity.py`，运行该文件为 1 failed（completed != running），再增加正向响应完成判定。修复后 `uv run python -m pytest tests/unit tests/integration tests/smoke -q` 为 **70 passed，无警告，退出码 0**（5 unit、64 integration、1 smoke）。这条保护只防止虚假成功，不提前实现 F6 取消语义。
- Claude Code 独立审查：第 1 轮完整审查已完成，最终 APPROVED；P0=0、P1=0、P2=0、P3=5，均不阻断。直接调用本机 Claude Code，只读工具 Read，进程级关闭 inline tools；退出码 0、is_error=false、stop_reason=end_turn。19 个文件已读取，覆盖全部 16 个源码/测试文件，审查前后哈希一致；[原始报告](review.md)已保存。此前工具中断/超时没有 verdict，不伪报为已完成审查。
- 结论：验收通过。最终复跑 `uv run python -m pytest -q` 为 70 passed、无警告、退出码 0；全部必测用例与独立审查已通过。真实 Provider、模型对 System Prompt 的实际遵守效果、F2–F8 和生产部署均未验证。
- 依赖说明：测试初期的 pytest-asyncio 配置警告已通过显式 function scope 修正；Starlette 1.10.0 推荐 TestClient 使用 httpx2，因此 dev 依赖加入 httpx2 2.16.0，HTTPX 0.28.1 保留给 ASGITransport 和回环 HTTP；所有版本由 uv.lock 固定。
- 模板回看：将替身注入边界、确定测试数据、共同断言、分层证据限制及计划/实际结果分开记录的经验回填测试模板。用例的具体字符数、固定标识和路由保留在本方案；未复制到通用模板。模板文档已核对，测试效果待实施验证。
- 实施后回看：F1-14 证明“离开执行路径不等于成功”，已将正向完成证据与提前结束回归的通用要求回填模板；F1 测试已验证此保护，后续 Feature 的改进效果仍待验证。
- 审查调用记录：此前遇到读取输出截断、会话初始化停滞和完整审查超时，均未形成最终结论。只检查 Claude Code 的实际读取/输入、最终审查判断和正常退出，不把工具启动算作审查通过；模型选择与连接参数交由 Claude Code 既有配置，项目不硬编码。

- 工具修复验证：[BUG-001](../../bugs/BUG-001-claude-code-inline-tools.md) 的三文件单变量回归和真实 F1 完整审查都已通过。模型与连接交由 Claude Code 既有配置；项目只采用已验证的兼容调用参数。

## 4. 独立审查 P3 处理结论

这些建议均不影响已确认的 F1 验收条件，源码与测试未因审查建议额外扩范围：

| 建议 | 核实与处理 |
| --- | --- |
| 响应开始后的异常窗口 | 延续 F5 已有的 SSE 失败事件边界；不在 F1 提前实现。报告中“已开始后外层会发送纯文本 500”的推断不采纳：实际 Starlette errors.py 仅在 response_started=false 时发送替代 500，响应已开始时只会重新抛错。 |
| 其他 HTTPException 的通用 code | F1 只约定 404/405，均已验证；新路由在对应 Feature 定义自己的错误码，当前不增加枚举。 |
| 内存 Trace 无上限 | F1 保持本地应用实例生命周期内记录；F8 再设计持久化与保留/清理策略，不把 SQLite 本身称为自动解决内存增长。 |
| 冒烟客户端 0.4 秒超时 | 记录慢机器上的潜在测试波动；当前环境已稳定通过，慢机器尚未验证。本次保留已通过的用例，若复现波动走 Bug 回归修正。 |
| 模板与模型同时未知时的优先级 | 当前执行顺序确定为模板先、模型后，分别错误的用例已覆盖；F2 扩展时记录组合错误优先级，不在当前添加额外行为。 |

模板闭环结果：已将完整 verdict/正常退出证据要求回填测试模板，并把工具兼容故障的复现、单变量修复和真实项目复测记录在 BUG-001；没有把后端模型名或 F1 专有配置推广到模板。

## 2026-10-31 目录重构验证

- 已确认范围：去除源码的项目包目录层，同步所有静态导入、monkeypatch 字符串、Uvicorn 入口与当前目录文档；不改变 API 或业务行为。
- 验证方案：迁移前后运行 `uv run python -m pytest -q`（含独立进程 HTTP 冒烟）；逐文件比对只发生模块前缀替换；扫描源码/测试和当前文档的旧模块引用，检查 Markdown 本地链接。无需为目录迁移新增重复的业务用例。
- 迁移前基线：70 passed in 0.56s，退出码 0，无警告。
- 迁移后验证：`uv run python -m pytest -q` 为 **70 passed in 0.61s**，退出码 0，无警告，含单 worker 真实 HTTP 冒烟。16 个源码/测试文件与迁移前逐文件比对，仅删除模块引用前缀；源码/测试和当前文档无旧模块引用，本地文档链接检查通过。
- README 启动参数验证：独立子进程以 `--app-dir src main:app --workers 1` 启动，不提供 `PYTHONPATH`，回环 HTTP 返回 200、`MOCK_REPLY`、四字段响应和请求 ID；进程已清理。
- Claude Code 独立审查（本次重构第 1 轮）：**APPROVED，P0=0、P1=0、P2=0、P3=1**；正常退出 0、`is_error=false`、`stop_reason=end_turn`。全部 16 个源码/测试文件均被实际读取，审查前后 SHA-256 一致；只读审查未运行测试，测试证据来自执行者。原文见 [审查报告](review.md#2026-10-31-目录重构claude-code-原始报告)。
- P3 核实与处置：审查指出设计文档遗留“文档待同步”表述，确认为已过期；已改为实际同步状态，仅修改文档。未改变源码、测试或已审查的业务契约。
- 审查后最终复跑：`uv run python -m pytest -q` 为 **70 passed in 0.55s**，退出码 0，无警告。
- 结论：目录重构验收通过；原有 70 个测试与 README 新启动入口验证通过，完整独立审查无阻断项。真实 Provider 与 F2–F8 仍未验证。
