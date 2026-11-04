# F1-O1：应用组装、目录命名与环境配置优化——测试方案

> 状态：验收通过。2026-10-31 用户确认设计与本测试方案后完成实施，115 个测试及 Claude Code 第 1 轮独立审查通过。
>
> 关联基线：[F1 测试与验收](../../test-plan.md)，历史记录为 5 unit、64 integration、1 smoke，共 70 个用例通过。本项拥有独立的执行与审查结论，不覆盖基线记录。

## 1. 范围与通过标准

证明 Middleware 抽取与 services 迁移保持 F1 行为，两个配置项正确加载和校验，ENV/Provider 独立，显式实例注入及工厂启动正确，自动化测试不受开发者配置影响。

分层：单元测试检查配置、构造与无副作用；进程内 HTTP 检查真实路由、Gateway、Middleware、Trace 与 Mock 调用；子进程使用实际源码和回环 HTTP 检查启动/启动失败。所有模型调用均为 Mock，不连接外部模型，不读取真实密钥，不证明真实 Provider、SSE、取消、恢复、持久化或生产能力。

通过标准：全部必测场景通过；原 70 个回归场景无删减或断言放宽；配置错误为零模型调用，HTTP 结果与 Trace/尝试一致；源码有效引用、启动命令和文档一致；Claude Code 完整只读审查无 P0–P2。新增测试的参数化展开数以实施时实际执行为准，不提前承诺总数量。

## 2. 用例

### 2.1 公共准备与共同断言

- 合法 HTTP 输入沿用 F1：`general-primary`、`basic_text@1.0.0`、一条内容为 `Explain TTFT` 的 user 消息；默认 Mock 返回 `MOCK_REPLY`，未知实际模型与 Usage 均为 null。
- Provider 边界仅替换实际调用实现；保留真实 `create_app`、配置加载、Provider 工厂、路由、Middleware 和 Gateway。统计构造工厂调用数与模型 `complete` 调用数，二者不得混淆。
- 启动/配置单元场景尚无请求，Trace 与 HTTP 信封不适用，不伪造记录；合法应用在请求前应无模型调用、无请求/尝试记录。
- HTTP 场景共同检查四字段信封、UUID 格式 `X-Request-ID`、正文不含 request_id；完整已确认 System 在前，原 user 文本在后。错误体不暴露私有异常或完整配置，失败尝试与错误类别一致，无重试。
- 自动化测试不依赖本机 `.env`、进程 ENV 或 Provider 设置。除专门验证来源优先级的用例，均显式注入完整测试配置，且禁用 dotenv 加载；进程变量用隔离 fixture 修改并恢复。
- 临时 `.env` 从本项实施后实际 `.env.example` 复制，按场景修改内容；使用 `tmp_path` 和显式路径，不改项目根 `.env`。

### 2.2 逐例矩阵

下表每行都是必测场景；列出的多个输入按参数化分别执行。

| 编号 | 对应设计、准备与调用 | 预期响应/错误 | Provider 与其他副作用 | Trace 断言 | 层级 |
| --- | --- | --- | --- | --- | --- |
| O1-01 | 3.2：隔离两项进程变量，指定不存在的临时 `.env`，加载配置并创建应用 | env=development、provider=mock，成功创建 | 默认工厂恰好 1 次；模型调用 0 次 | 请求/尝试均为空 | 配置/组装单元 |
| O1-02 | 3.1：复制示例为临时 `.env`，将 ENV 改为 test，正常加载并创建应用 | env=test、provider=mock | 创建 Mock；模型调用 0 次；不改模板/模型 | 请求/尝试均为空 | 配置/组装单元 |
| O1-03 | 3.2：文件 ENV=test，进程 ENV=production；加载后反向设置文件 production/进程 test 再执行 | 各自以进程值为准 | 不访问网络；测试后恢复进程变量 | 不适用，无请求 | 配置单元 |
| O1-04 | 3.2：文件 Provider=不支持值，进程 Provider=mock；再以文件 mock、进程不支持值执行 | 前者有效 mock；后者校验失败，不能回退 | 前者按需创建 Mock；后者工厂/模型调用均为 0 | 无请求/尝试 | 配置/组装单元 |
| O1-05 | 3.1：ENV 分别为 develop、TEST、空字符串、全空白；其余配置合法，分别创建应用 | 配置校验失败，指出 ENV 字段；不隐式纠正 | 工厂与模型调用均为 0 | 无应用请求记录 | 配置/组装单元 |
| O1-06 | 3.1：Provider 分别为 deepseek、openai_compatible、空字符串、全空白；ENV 合法 | 启动前配置校验失败，指出 Provider 字段 | 工厂与模型调用均为 0，不静默变 mock | 无应用请求记录 | 配置/组装单元 |
| O1-07 | 3.1：分别使用 development/test/production 与 mock 创建应用并提交相同合法请求 | 全部 HTTP 200，相同 F1 响应语义 | 各恰好 1 次 Mock 调用，相同模型/System/user；ENV 不改 Provider | 各 1 成功请求、1 成功尝试 | 单元 + 进程内 HTTP |
| O1-08 | 3.2：清空两项进程变量，临时 .env 只有 ENV=test 或只有 Provider=mock | 缺项使用默认值；已有项按文件取值 | 配置读取不执行模型调用 | 不适用 | 配置单元 |
| O1-09 | 3.2：改变 cwd 为不相关临时目录，检查默认配置路径；另用显式路径加载两个临时项目 .env | 默认路径仍定位实际项目根；显式路径加载各自内容 | 默认路径检查不读取开发者 .env；内容验证只读临时文件 | 不适用 | 配置单元 |
| O1-10 | 3.2：环境/目标 .env 均无值，仅放其他内容的 .env.example 或 .env.test 在临时配置目录 | 使用默认值，不读取两类非运行文件 | 读取路径探针证明未加载示例/.env.test；模型调用 0 次 | 不适用 | 配置单元 |
| O1-11 | 2.2：显式构造完整 Settings(test,mock)，外部 ENV/Provider 设非法值；传给 create_app，配置加载函数设为调用即失败 | 应用成功，使用传入配置；不调用外部加载函数 | 工厂 1 次，模型调用 0 次；不读 .env | 请求/尝试均为空 | 组装单元 |
| O1-12 | 2.2：注入同一个指定 Mock(text=INJECTED)，完整测试配置；工厂设为调用即失败，提交合法请求 | HTTP 200，text=INJECTED；app.state.provider 与注入对象是同一实例 | 工厂 0 次；该 Mock 恰好 1 次调用，模型和消息准确 | 1 成功请求、1 成功尝试 | 进程内 HTTP |
| O1-13 | 2.2：配置合法，Provider 工厂构造时抛私有标记 RuntimeError | 创建应用失败，异常传播，不伪装配置成功/HTTP 成功 | 工厂 1 次、模型调用 0 次，无备用实例 | 不适用，无请求 | 组装单元 |
| O1-14 | 2.2：在全新子进程，外部配置值非法，仅 import main；实际工厂设调用即失败或监听其调用，检查模块定义 | 导入成功、未调用 create_app/配置加载/Provider 工厂；无模块级 app | 不创建 Provider，不启动监听、不访问网络 | 无应用/请求/尝试 | 隔离导入/子进程 |
| O1-15 | 2.2/3.2：同进程创建两个不同 ENV 的应用，各发合法请求 | 配置各自正确，响应不受前一应用污染 | 默认 Provider、TraceStore 为独立实例；各模型调用 1 次 | 记录互不共享、请求 ID 不同，各收口 1 次 | 进程内 HTTP |
| O1-16 | 3.2：从临时 ENV=test 文件创建应用 A，改文件为 production，先检查 A 再创建 B | A 保持 test，B 为 production；创建后不自动更新 | A 接收请求时不重新读取文件；各模型调用按请求计数 | 每个完整请求各 1 条成功记录 | 配置 + 进程内 HTTP |
| O1-17 | 2.3：抽取后的 Middleware 接收非 HTTP ASGI scope，由记录型下游处理 | scope、receive、send 透传，不增加 HTTP 行为 | 模型调用 0 次 | 不创建 HTTP 请求记录 | Middleware 单元 |
| O1-18 | 2.3：通过真实应用执行正常请求、非法 JSON、未知模型/模板、404、405 | 沿用 F1 的 200/422/400/404/405 与错误码、请求头 | 合法请求 1 次；所有入口拒绝/未知路径 0 次 | 模型入口有请求记录；拒绝零尝试；404/405 无模型 Trace | 进程内 HTTP/原回归 |
| O1-19 | 2.3：分别注入 ProviderCallError、未预期 RuntimeError 的 Mock；另在模型调用前注入程序错误 | 分别 502/provider_error、500/internal_error、500/internal_error；统一安全错误体 | 前两种各 1 次、无重试；调用前错误 0 次 | 前两种 1 失败请求/1 失败尝试；调用前错误零尝试 | 进程内 HTTP/原回归 |
| O1-20 | 2.3：沿用 F1 中断用例，Provider 抛 CancelledError；另用中间件下游发送 response.start 后抛错 | 中断传播，未完成响应不记成功；已开始后不补发第二个 response.start | 中断尝试已开始 1 次，无重试；下游故障不新增模型调用 | 未确认记录保持 running，不新增取消终态；不重复收口 | 进程内 HTTP + Middleware 单元 |
| O1-21 | 2.3：沿用两个交错请求、调用方自带 ID、原文保留与 Trace 隐私用例 | 消息/请求 ID 各自正确；F1 响应与脱敏断言不变 | 每个合法请求各 1 次，完整 System/user 不串扰 | 各自关联且仅收口一次，不保存消息/回答 | 进程内 HTTP/原回归 |
| O1-22 | 3.3：在隔离运行目录复制实际 src 与示例配置，生成同名 .env，使用 main:create_app --factory 启动；提交合法、非法 JSON、未知路径 | 200/422/404；合法文本 MOCK_REPLY、统一信封及请求 ID；启动不依赖开发者文件 | 单 worker、本地回环；不连接外部模型；结束后清理进程/套接字 | 该层不读取服务内存，内部副作用由前述用例证明 | 真实进程 HTTP 冒烟 |
| O1-23 | 3.2/3.3：隔离目录 .env 分别写非法 ENV/Provider，按工厂入口启动子进程 | 正常有界时间内非零退出，指出配置字段，不输出完整配置；不出现成功启动 | 无模型调用；工厂前失败由 O1-05/06 证明，不能只靠退出码推断 | 不声称从外部读取 Trace | 启动失败子进程 |
| O1-24 | 2.1/3.3：检查实际目录、源码导入、monkeypatch 字符串、当前启动命令和文档链接 | service/ 不再存在，services/ 与 middleware 文件就位，所有现行入口用工厂；历史报告保留旧快照 | .env.example 内容正确、.env 被排除；不创建/覆盖用户 .env | 不适用 | 文件/文档检查 |
| O1-25 | 4：运行原 70 个 F1 回归用例，路径与装配按新设计调整 | 所有既有场景与断言保持通过，包含字符边界、严格字段校验与 HTTP 冒烟 | 调用次数和参数断言不放宽，不换成固定响应路由 | 原有完整性、唯一收口与隐私断言均保留 | 全部原回归 |

原回归行采用既有用例，不为矩阵编号重复实现相同业务测试。新增配置/组装/Middleware 场景在最小必要位置实现；可组合准备步骤，但不得删掉对应设计断言。

## 3. 执行计划、证据与结论

### 3.1 用户确认后的执行计划

- 依赖：项目独立 uv 环境；新增 `pydantic-settings` 并同步 `pyproject.toml`、`uv.lock`。确切版本在实施时锁定；本次不安装或修改依赖。
- 计划文件：`tests/unit/test_settings.py`（来源、路径、校验）、`tests/unit/test_app_factory.py`（工厂/导入/隔离）、`tests/unit/test_request_context.py`（抽取后的 ASGI 边界），复用现有 integration/smoke 文件；需要时在 smoke 增加同类启动失败用例，不新增产品测试接口。
- 测试隔离：普通测试显式提供 Settings(test,mock) 与所需 Mock；配置用例用临时文件和 monkeypatch，作用域结束自动还原进程变量/cwd。修改本地配置文件只能发生在临时目录，根目录实际 .env 的存在性/哈希在实施前后核对，不输出其内容。
- 真实进程：将实际 `src/` 原样复制到临时运行目录以隔离项目根 .env，使用同一虚拟环境解释器启动 `-m uvicorn --app-dir <临时目录>/src main:create_app --factory --workers 1`，绑定回环随机可用端口/传递监听 socket；不替换应用入口、路由或 Gateway。临时 .env 从实际 .env.example 生成。以有界 deadline 等待就绪/退出，失败时保留脱敏日志；finally 终止并 wait，必要时 kill 后 wait，清理临时目录和套接字。
- Red：新增行为在可导入的最小边界上通过断言证明缺失；例如配置来源优先级、显式配置跳过加载、导入不创建应用。不得把不存在的新模块造成的 ImportError 或移动后的漏改导入当作有效 Red。纯路径迁移通过前后回归与行为比对，不凭空新增业务规则。
- Green：仅实现已确认设计以通过同一用例；保留原 HTTP 断言，之后执行新增用例、全部回归及工厂启动冒烟。

实施后在项目根目录执行的计划命令：

```bash
uv sync --locked
uv run python -m pytest tests/unit -q
uv run python -m pytest tests/integration -q
uv run python -m pytest tests/smoke -q
uv run python -m pytest -q
```

命令指向实施后实际测试文件；当前测试目录尚未增加本项用例，运行现有测试不能证明本方案已实现。

### 3.2 独立审查与证据要求

按 [项目工作流](../../../../../AGENTS.md) 和 [Claude Code 调用经验](../../../../references/claude-code-review.md) 提交完整变更、设计及逐例证据，只读审查。每项完整审查最多三轮，记录最终 verdict、进程退出和实际读取范围，P0–P2 阻断；第三轮仍有阻断项才交用户重审。原始报告仅在真实完成后保存到本目录 `review.md`，不预建空报告；发现核实、处置、复测及最终结论写回本文件，不写到原 F1 测试方案。

### 3.3 当前实际状态

| 项目 | 状态 |
| --- | --- |
| 用户确认整体优化设计 | 已确认 |
| 用户确认本测试方案 | 已确认（2026-10-31） |
| 修改源码、测试、依赖或实际 .env | 源码、测试、依赖已完成；用户实际 .env 未创建或修改 |
| Red、Green、配置/启动测试与 F1 回归 | 已执行，115 passed；逐例映射和证据见下文 |
| Claude Code 实现审查 | 本项第 1 轮 APPROVED，P0/P1/P2 为 0，2 项 P3 已记录处置 |
| 本项验收 | 验收通过：本项测试、原 F1 回归及独立审查均通过 |

本地实现已按下述映射验证；文件检查只证明对应静态边界，Mock/本地 HTTP 结果不代表真实 Provider 或生产能力。独立审查的最终 verdict 与处置已记录于 3.6。

模板回看：本项补充独立优化编号、关联基线和原回归保护的记录位置；不将两个环境配置名或固定场景数推广到通用模板。基线保护和测试隔离已有本项证据；后续 Feature 的模板效果仍未验证，本次不因场景数量增加就宣称通用质量提高。

### 3.4 实际执行记录

- 实施前：`uv run python -m pytest -q` 为 70 passed in 0.58s，退出码 0，无警告。原 F1 三份文档和用户实际 `.env` 的存在性/哈希已留存到临时取证目录。

- Red 1（无导入副作用）：`uv run python -m pytest tests/unit/test_app_factory.py -q` 为 1 failed；真实子进程打印 `1 True`，期望 `0 False`，证明导入时构造了一个 Provider 且存在模块级 app；不是 ImportError。
- Red 2（配置读取）：在可导入最小 Settings/load_settings 边界上执行 `uv run python -m pytest tests/unit/test_settings.py -q` 为 11 failed、8 passed；文件中的 ENV=test 被忽略、非法文件值未拒绝。实现文件读取后同文件 19 passed。
- Red 3（Provider/应用工厂）：运行 `test_default_provider_factory_returns_uncalled_mock` 与上述导入用例，2 failed；最小工厂返回 None 而非 Mock，导入仍有副作用。实现工厂、配置注入并移除模块级 app 后，同一用例及相关单元/集成为 108 passed。
- Middleware：迁移前四个 ASGI 边界用例 4 passed；抽取后连同目录迁移回归 112 passed（不含 smoke）。这是行为保持验证，不把迁移后的导入错误伪报为 Red。
- 工厂启动：隔离目录使用实际 src 副本及同名 .env，从不相关 cwd 启动；正常 HTTP 与两种非法配置退出共 3 passed。默认测试不连接外部模型。
- 依赖已锁定：pydantic-settings 2.16.2、python-dotenv 1.2.4；`uv sync --locked` 退出 0。

| 实际命令（项目根目录） | 结果 | 退出码 |
| --- | --- | --- |
| `uv run python -m pytest tests/unit -q` | 48 passed in 0.44s | 0 |
| `uv run python -m pytest tests/integration -q` | 64 passed in 0.18s | 0 |
| `uv run python -m pytest tests/smoke -q` | 3 passed in 0.82s | 0 |
| `uv run python -m pytest -q` | 115 passed in 1.28s，无警告 | 0 |

总计原 70 个场景保留，新增 45 个展开用例；测试目录的层级保留历史位置，应用工厂测试中使用进程内 HTTP 的用例不因此宣称为纯逻辑验证。源码比对确认：Middleware 类 AST 完整一致，其他原源码除 main 组装外仅改服务导入/路径；原 21 个测试函数及全部 assert AST 保留（参数化展开为原 70 个场景），只调整导入、显式配置注入及 smoke 工厂启动装配。原 F1 三份文档哈希不变，项目根 .env 的存在性/哈希不变。

### 3.5 必测编号到实际结果

以下函数位于 `tests/`，同一函数的参数化变体全部执行；原回归场景直接复用，未复制成重复测试。

| 编号 | 实际证据位置 | 结果 |
| --- | --- | --- |
| O1-01 | `unit/test_settings.py::test_missing_file_uses_defaults`；`unit/test_app_factory.py::test_default_app_constructs_provider_once` | 通过，默认配置/构造次数与空记录正确 |
| O1-02 | `unit/test_settings.py::test_env_file_is_loaded`；应用工厂配置/HTTP 用例及 smoke 示例复制 | 通过，示例复制为临时 .env 后正确加载 |
| O1-03 | `unit/test_settings.py::test_process_environment_overrides_file` | 两个方向均通过 |
| O1-04 | `unit/test_settings.py::test_invalid_provider_does_not_fall_back`；应用配置拒绝用例 | 两种优先级输入均通过，非法高优先级不回退 |
| O1-05 | `unit/test_settings.py::test_invalid_values_are_rejected`；`unit/test_app_factory.py::test_invalid_settings_fail_before_provider_construction` | ENV 四种非法值通过，工厂/模型均零次 |
| O1-06 | 同上两个参数化函数 | Provider 四种非法值通过，注入实例也不能绕过配置校验 |
| O1-07 | `unit/test_app_factory.py::test_environment_does_not_change_provider_or_http_contract` | 三种环境全部通过，模型、消息、HTTP 与 Trace 一致 |
| O1-08 | `unit/test_settings.py::test_partial_file_uses_defaults_for_missing_fields` | 两种缺项输入均通过 |
| O1-09 | `unit/test_settings.py::test_default_path_does_not_depend_on_cwd`；smoke 从不相关 cwd 启动 | 通过，默认定位与显式临时路径正确 |
| O1-10 | `unit/test_settings.py::test_only_selected_env_file_is_read` | 通过，真实文件打开探针观察选中 .env，非运行文件未读取 |
| O1-11 | `unit/test_app_factory.py::test_explicit_settings_skip_external_loading`；`unit/test_settings.py::test_explicit_settings_ignore_invalid_external_values` | 通过，非法外部值不影响显式配置，外部加载不执行 |
| O1-12 | `unit/test_app_factory.py::test_injected_provider_skips_factory` | 通过，同一实例、工厂零次、模型一次、请求完成 |
| O1-13 | `unit/test_app_factory.py::test_factory_error_aborts_application` | 通过，工厂一次并传播失败，无回退 |
| O1-14 | `unit/test_app_factory.py::test_import_main_does_not_create_provider_or_app`、`test_import_main_does_not_load_settings_or_call_factory` | 两个隔离子进程通过，未创建模块级应用 |
| O1-15 | `unit/test_app_factory.py::test_applications_keep_independent_settings_and_traces` | 通过，配置/Provider/记录独立且各调用一次 |
| O1-16 | `unit/test_app_factory.py::test_configuration_is_fixed_until_next_application` | 通过，旧应用不重读，新应用取得新文件值 |
| O1-17 | `unit/test_request_context.py::test_non_http_scope_is_passed_through` | 通过，无 HTTP 记录副作用 |
| O1-18 | 原 integration 成功、非法 JSON、未知资源、404/405 用例 | 全部通过，响应与 0/1 次调用、记录一致 |
| O1-19 | 原 `test_call_errors_keep_single_failed_attempt`、`test_program_error_before_call_has_no_attempt` | 通过，502/500 分类与尝试一致，无重试 |
| O1-20 | 原 `integration/test_trace_integrity.py`；`unit/test_request_context.py` 的 start 后异常、最终 body 发送失败及完整 body 收口用例 | 全部通过，未确认工作不记成功、不补发第二响应 |
| O1-21 | 原 integration 交错请求、请求 ID、原文保留与 Trace 隐私用例 | 全部通过，断言未放宽 |
| O1-22 | `smoke/test_http_server.py::test_single_worker_real_http_server` | 通过，工厂入口下真实回环 200/422/404，子进程已回收 |
| O1-23 | `smoke/test_http_server.py::test_invalid_configuration_exits_before_serving` | 两种配置通过，非零退出，私有值未泄露 |
| O1-24 | 实际目录/源码扫描、文档链接、依赖锁及保护文件指纹检查 | 通过；本项记录与当前 README 同步，原 F1 保留历史快照 |
| O1-25 | 原 `unit/test_core.py`、integration 与原 smoke 成功函数；AST/函数清单比对 | 原 70 个场景全部通过，无函数/断言删除 |

### 3.6 独立审查

直接调用本机 Claude Code 完成本项第 1 轮只读审查：**VERDICT: APPROVED，P0=0、P1=0、P2=0、P3=2**。正常退出 0、`is_error=false`、`stop_reason=end_turn`；全部 23 个源码/测试文件均在实际读取范围内，源码/测试/配置示例/忽略规则/依赖清单审查前后 SHA-256 一致。原文见 [review.md](review.md)，真实分层测试、审查元数据和审查后锁依赖核验回执见 [evidence.json](evidence.json)。审查者未运行测试，测试结论来自执行者，不把静态核验当独立运行证据。

| 审查项 | 核实与处置 |
| --- | --- |
| P3：状态标签不一致 | 已核实并统一到“验收通过”，同时同步设计、README、AGENTS、架构与进度；只改文档，不改已审查代码或契约。 |
| P3：历史 Red/同步输出未单独归档 | 缺口属实：早期 Red 命令与结果见 3.4 和当次工具回执，但未保存独立原始日志。已把真实存在的最终分层测试输出保存到 evidence.json，并保存审查后重新执行的 uv sync 回执，明确它不是历史 Red 输出；不补造旧日志、不重跑 Red 冒充此前证据。后续执行时同步保存关键原始回执，规则回填测试模板。 |
| 非发现提示：httpx2 是否有意保留 | 当前安装的 Starlette testclient.py 第 26/29 行优先 `import httpx2 as httpx`，旧 httpx 分支有弃用提示；该依赖供 TestClient 间接使用，原 F1 已记录原因。保留现有依赖，不按“项目未直接 import 就未使用”的静态推断删除。 |

额外隔离验证：以 `ENV=invalid LLM_GATEWAY_PROVIDER=invalid uv run python -m pytest -q` 运行，115 passed in 1.50s，退出码 0，证明自动化测试不依赖两项外部进程设置。用户根 `.env` 的存在性/哈希及原 F1 三份文档在实施前后保持不变。

结论：F1-O1 验收通过，原 HTTP/System/Trace 契约与 70 个回归场景保留，新增配置/组装/ASGI/启动验证通过。审查后仅补充取证和文档状态，受审源码、测试与依赖均未再改变；因此不为了无代码变化的 P3 文档处置开启第二轮审查。真实 Provider、F2–F8、生产部署及不同平台环境仍未验证。

### 3.7 用户验收反馈

2026-10-31，用户在本次优化交付后反馈：“F1: 我测试过，通过了”。记录为当前 F1 的用户测试通过反馈，与上面的自动化测试和独立审查证据分别保留；未补充具体测试步骤、环境或 Provider 信息，不据此扩展已验证范围。F2 仍未开始。
