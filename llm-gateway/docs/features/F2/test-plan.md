# F2：受控 Prompt 模板装配与版本追踪——测试方案

> 状态：验收通过。2026-10-31 用户确认本测试方案与[设计](design.md)后完成 TDD，223 个测试与 Claude Code 第 1 轮独立审查通过；F3 Provider/Token 讨论保持后续范围。
>
> 回归基线：[F1-O1 验收与用户反馈](../F1/optimizations/O1/test-plan.md)，历史 115 个用例通过；此历史结果不代表 F2 已实现。本项所有原始证据和结论在本目录记录，不改写 F1 历史。

## 1. 范围与通过标准

- 证明：三个受控模板版本及完整正文、单条用户问题与额外变量来源、静态示例的 2/2/6 条消息装配、固定 System、严格校验与两层预算、可预测错误分类、只读版本与初始化校验、最终消息指纹及元数据分离、请求/尝试记录与零调用拒绝。
- 不证明：真实模型语义、Few-shot 实际效果、注入抵抗效果、订单/政策真实性、实际业务动作、真实 Provider、F3–F8 和生产部署。
- 层次：纯模板/注册/指纹单元测试；MockProvider + 真实 FastAPI/Gateway 的进程内 HTTP；使用实际源码的独立进程回环 HTTP；文件/文档检查。外部网络默认关闭，不加载真实密钥。
- 通过条件：全部必测场景和原 115 个回归通过；输入与消息逐字符一致，调用次数/Trace/指纹符合设计；无未处理阻断项，Claude Code 完整独立审查无 P0–P2。新增参数化展开数在实施时以实际收集结果为准，不先承诺总数。

## 2. 用例

### 2.1 确定输入、黄金资产与共同断言

1. 默认使用 `general-primary` 与 MockProvider(text="MOCK_REPLY")；完整注入 Settings(env="test", provider="mock")，禁用 dotenv 加载，按既有 fixture 隔离外部进程配置。模板集合为独立、经过真实注册器校验的对象，不通过修改全局共享索引制造场景。
2. 黄金正文以 [设计 3.3](design.md#33-完整受控模板资产) 为准，按固定正文和换行逐字符核对；测试不从被测注册表复制文本后自比。保留原 F1 System 的文档黄金基线，新增资产在测试中维护独立预期值。
3. basic_text 的确定用户输入为 `Explain TTFT`。context_answer 的确定用户输入为 `订单 A123 的商品是什么？`，context 为 `调用方提供的资料：订单 A123 的商品为充电器。`，分别选择 1.0.0/1.1.0。
4. 成功 JSON 完全保持 F1 五个 data 字段及四字段信封，响应头有 UUID `X-Request-ID`；正文不新增 request_id、指纹、模板或变量。末条 User 原文由模板精确拼装，System 与示例来自固定资产。
5. 每个成功请求 complete 恰好 1 次，收到准确逻辑模型与最终消息；Few-shot 不产生额外调用。成功请求和尝试完成；Provider 故障只有 1 次失败尝试，无重试。
6. 调用前拒绝有 1 条失败请求、0 次模型调用、0 条尝试；结构/变量/渲染/哈希失败时指纹为 null。成功装配后未知模型则有指纹但仍为零调用。Trace 不保留消息、变量值、完整示例、输出或密钥。
7. 错误共同断言：真实 HTTP 状态、四字段信封、success=false、data=null、安全 message 与请求 ID；不含 PRIVATE_INPUT_MARKER、PRIVATE_ERROR_MARKER、完整正文或堆栈。未知键的任意文字不在错误中回显。
8. 初始化失败无 HTTP 请求，模型调用为零，不伪造请求 ID 或 Trace；通过 Provider 工厂 spy 验证资产定义错误不会进入模型侧构造路径。实例构造次数与 complete 次数分别断言，不靠状态码推断零副作用。

预期指纹固定如下，按设计规定的 JSON 字节和 SHA-256 从文档资产独立计算；这是确定的预期测试数据，**不是 F2 实现已运行的结果**：

| 模板与上述固定输入 | 消息数 | 预期 prompt_fingerprint |
| --- | --- | --- |
| basic_text@1.0.0 | 2 | `37e862387da7d344b9276e6edaf43c1cd9f6ad5f3ae10be486f0b265a1afb83d` |
| context_answer@1.0.0 | 2 | `e17a281a18c58372850707381c6590cf0278e92c475ab92f116d7a4b19c4091b` |
| context_answer@1.1.0 | 6 | `980afec5fdd5cd3e5fb6733cb12aabd5d7e38b517f2b8dd54f3e5614b4e740b2` |

context_answer 的 User 模板固定文字/换行共 73 字符（不含两个变量值）。总长边界使用 context=`中`、message=`问` 重复 19,926 次得 20,000；message 再增加一字符得 20,001。两种输入均未超单字段上限，能单独证明总预算检查。

### 2.2 必测矩阵

每行均为必测；列出的多种输入分别参数化执行。共用断言在 2.1，不能只报通过状态而略过调用与记录检查。

| 编号 | 场景、准备与调用 | 预期响应/错误 | Provider/其他副作用 | Trace 断言 | 层次 |
| --- | --- | --- | --- | --- | --- |
| F2-01 | basic_text 原请求，分别省略 variables 或传 {} | 均 200，原响应和用户正文不变 | 各 1 次，原 System + user 两条 | 成功请求/尝试，原版本和有效指纹 | 进程内 HTTP |
| F2-02 | context_answer@1.0.0，使用 2.1 确定输入 | 200、MOCK_REPLY | 1 次，两条消息，变量精确填充，管理元数据不发给模型 | 成功，名称/版本与黄金指纹准确 | 单元 + HTTP |
| F2-03 | context_answer@1.1.0，相同输入 | 200、同一响应协议 | 1 次、6 条，角色顺序 system/user/assistant/user/assistant/user；4 条示例逐字符准确 | 成功，新版本指纹准确 | 单元 + HTTP |
| F2-04 | variables 分别为 null、字符串、列表、数字 | 422/invalid_request | 0 次，渲染不执行 | 失败、零尝试、指纹 null | HTTP |
| F2-05 | context 值分别为数字、布尔、null、列表、对象 | 422/invalid_request，不转字符串 | 0 次 | 失败、零尝试、指纹 null | HTTP |
| F2-06 | 请求提交 system/assistant 角色、第二条消息，或在根/prompt/message 对象增加模板正文、few_shots、change_note 等未声明字段 | 422/invalid_request | 0 次；调用方不能注入示例/规则 | 失败、零尝试、指纹 null | HTTP/原回归 |
| F2-07 | 未知名称、未知版本、latest、范围字符串，各保持结构合法 | 400/unknown_prompt_template，无版本回退 | 0 次 | 失败，保留请求标识，零尝试/指纹 null | HTTP |
| F2-08 | context_answer 省略 variables、传 {}；分别选择两个版本 | 400/missing_prompt_variable，可指出受控键 context | 0 次，不补默认资料 | 失败、零尝试/指纹 null | HTTP |
| F2-09 | 已有 context 再传额外键；另给 basic_text 任意非空变量 | 400/invalid_prompt_variables，不忽略多余项 | 0 次；未知键私有标记不回显 | 失败、零尝试/指纹 null | HTTP |
| F2-10 | variables 填 message，分别为同值或不同值；context 可有可无 | 400/invalid_prompt_variables，保留键覆盖优先拒绝 | 0 次，不覆盖原用户问题 | 失败、零尝试/指纹 null | HTTP |
| F2-11 | context 为 "" 或全空白（空格/换行/制表符） | 400/invalid_prompt_variables | 0 次 | 失败、零尝试/指纹 null | HTTP |
| F2-12 | context 分别使用 ASCII/中文，长度 10,000 与 10,001，问题使用短字符串 | 前者 200；后者 400/invalid_prompt_variables | 前者 1 次且原文不裁剪，后者 0 次 | 前者完成有指纹，后者失败无指纹 | 单元 + HTTP |
| F2-13 | 按 2.1 构造最终 User 为 20,000 与 20,001，分别测试 ASCII/中文等长输入 | 前者 200；后者 400/prompt_too_long | 1/0 次；标签/换行计数，无截断 | 完成有指纹/失败无指纹 | 单元 + HTTP |
| F2-14 | basic_text 原 user 为 20,000/20,001；context_answer 原 user 为 20,000 且短 context | basic 前者 200、后者 422；context 400/prompt_too_long | 分别 1/0/0 次 | 对应指纹和尝试正确，原字段预算不变 | HTTP/原回归 |
| F2-15 | 问题/资料含首尾空白、缩进、换行、JSON 花括号、`${message}`、闭合标签或“忽略 System” | 合法长度内 200，单次替换、原文保留 | 1 次，System/静态例不变；变量仍在末条 user，不变成角色/额外消息 | 指纹对应实际消息，正文不留存 | 单元 + HTTP |
| F2-16 | 组合错误：结构非法+未知模板；未知模板+坏变量+未知模型；保留键+缺变量；缺变量+多变量；长 context+总超限 | 依次 422、unknown_prompt_template、invalid_prompt_variables、missing_prompt_variable、invalid_prompt_variables | 全部 0 次，按设计优先顺序拒绝 | 失败、零尝试/指纹 null | HTTP |
| F2-17 | 在合法模板/输入下，渲染边界注入私有 RuntimeError | 500/internal_error，不伪装变量错误 | 0 次 | 失败、零尝试/指纹 null | 服务 + HTTP |
| F2-18 | 消息已合法装配，指纹计算边界抛私有 RuntimeError | 500/internal_error，安全错误体 | 0 次 | 失败、零尝试/指纹 null，不写半成品 | 服务 + HTTP |
| F2-19 | 模板/变量合法，model=not-registered | 400/unknown_model | 0 次，渲染与指纹已完成 | 失败、零尝试，但保存正确消息指纹 | HTTP |
| F2-20 | 合法装配后 Mock 分别抛 ProviderCallError/RuntimeError | 原 502/provider_error 或 500/internal_error | 各 1 次，无重试 | 请求/尝试失败，指纹保留且准确 | HTTP/原回归 |
| F2-21 | 合法装配后 Provider 抛 CancelledError；保留最终 body 发送失败用例 | 中断传播，未完成响应不记成功 | 已调用 1 次，无重试，不增加取消终态 | 未确认记录 running，已生成指纹不丢失 | HTTP/原回归 |
| F2-22 | 两个 context_answer 请求用不同问题/资料，通过受控 Event 交错完成 | 各响应正确、ID 不同 | 共 2 次，消息彼此不串扰 | 各自请求/尝试/版本/指纹匹配，只收口一次 | 异步 HTTP |
| F2-23 | 在 context、user、私有异常中设置标记，检查响应及序列化 Trace | 响应/错误不泄露私有信息 | 正常 1 次/拒绝 0 次 | 只保留原元数据与指纹，不含正文、示例/变量对象或输出 | HTTP |
| F2-24 | 检查默认三份模板的所有属性与设计完整正文，逐版本装配 | 原 System/User 保留；两个 context 版本只有版本、示例及说明存在已声明差异 | 仅检查资产时模型 0 次；模板文案黄金断言独立 | 不适用；请求映射由 01–03 证明 | 单元/资产黄金 |
| F2-25 | 独立集合含相同 name/version 两次（同正文、不同正文）；另构造空标识或非法受控版本 | 定义拒绝，应用初始化失败，不覆盖前项 | Provider 工厂与模型 0 次 | 无请求记录或伪造指纹 | 注册/应用单元 |
| F2-26 | 模板缺 message、未声明占位符、声明未使用变量、`${}`、属性/索引/格式表达式、错误美元符；另验证合法命名占位与 `$$` | 非法定义初始化拒绝；合法字面转义正确；不执行表达式 | 非法不进入 Provider 构造/模型；合法纯渲染无网络 | 不适用 | 注册/渲染单元 |
| F2-27 | variable_specs 重定义 message、变量名不合法、max_chars 为 0/负数/字符串/布尔；System/User/change_note 空白或非字符串 | 定义拒绝，属于平台资产错误 | Provider 工厂/模型 0 次 | 无请求记录 | 注册/应用单元 |
| F2-28 | Few-shot User 或 Assistant 空白/非字符串，错误对结构或任意角色定义；另以含占位符字面的合法静态示例装配 | 错误结构拒绝；合法示例字面保持，不做运行时替换 | 非法初始化 0 模型；纯装配 0 模型 | 不适用，内容顺序另由 03 验证 | 注册/渲染单元 |
| F2-29 | 尝试修改注册模板字段、变量预算、示例集合/索引；独立测试集合加入额外合法 fixture 模板 | 修改拒绝或与原对象隔离；不污染其他应用/default 三份资产 | 不通过共享全局修改造场景 | 独立应用各自记录，不泄露模板对象 | 注册/应用单元 |
| F2-30 | 隔离子进程 import main；另在 create_app 统计初始化校验，创建两应用后发多个请求 | 导入不创建应用；每个应用只初始化一次注册表，请求不重复加载/校验资产 | 合法创建仍无模型调用，发请求后按各请求计数 | 初始化无请求；后续请求各自独立 | 应用单元/原回归 |
| F2-31 | 同时选择已注册 1.0.0 和 1.1.0，再切回旧版；不使用 active/latest 默认 | 旧版始终 2 条，新版 6 条，旧版不被升级覆盖 | 每个合法请求 1 次，明确选择即生效 | 名称/版本准确，各自正确指纹 | HTTP |
| F2-32 | 相同模板/输入重复装配与请求，改变请求 JSON 字段排列 | 最终消息与 64 位小写十六进制指纹相同，ID 可以不同 | 各合法请求 1 次 | 指纹相同，记录独立 | 指纹单元 + HTTP |
| F2-33 | 独立合法 fixture 分别只改变本次 message/context、固定 System、示例文本或示例顺序 | 每种改变均改变最终消息指纹 | 纯装配 0 次，不篡改已发布真实资产 | 指纹与实际消息对应 | 指纹单元 |
| F2-34 | 独立 fixture 的最终消息相同，只改变名称/版本/说明；应用改变 ENV；合法装配后未知模型 | 指纹不受管理元数据影响，版本/模型另记录；未知模型仍拒绝 | 纯装配 0 次；未知模型 0 次 | 可同指纹、不同元数据，不把有指纹当已调用 | 指纹单元 + HTTP |
| F2-35 | 对 2.1 三个确定输入，用独立黄金消息/固定编码预期值比对 | 指纹逐字等于表中常量；换行/Unicode 原文保留 | 纯渲染 0 次，HTTP 送出的消息与黄金一致 | 有效请求指纹等于实际送出的消息摘要 | 单元 + HTTP |
| F2-36 | 隔离运行目录复制实际 src/.env.example，使用工厂命令启动；请求两个 context 版本、缺变量和非法变量类型 | 分别 200/200/400/422，原信封与请求 ID 正确 | 本地回环、单 worker，结束回收进程；不声称外部直接观察到内存 Trace | 调用/Trace 副作用由前述进程内用例证明 | 真实进程 HTTP |
| F2-37 | 完整运行原 115 个场景；允许仅为新内部依赖/取值方式调整装配 | 原请求、配置隔离、导入、错误、ASGI 与 70 个 F1 场景不变 | 不删除场景或放宽参数/次数断言 | 原唯一收口/隐私/状态继续成立 | 全部回归 |
| F2-38 | 核对当前路径、已实现/目标状态、黄金资产、引用、实际 .env 与原 F1 文件指纹；检查无新增管理接口/配置/依赖 | 实施后文档与实现一致；历史证据不改写；没有范围外占位 | 不覆盖用户 .env；原始证据按执行时保存 | 不适用 | 文件/文档检查 |

对于 25–30、33–34 等定义/指纹单元场景，使用独立合成 fixture 测试注册器和渲染器；不得修改三个真实已发布标识的正文来绕过不可变版本规则。它们不是新增交付模板，也不能从 HTTP 上传。

## 3. 执行计划、证据与结论

### 3.1 用户确认后的实施与验证计划

- 环境：复用项目独立 uv 环境与锁文件，不增加第三方模板/版本依赖；ENV 与 Provider 独立，默认 Mock。用户实际 .env 不读写，测试使用显式配置/临时运行目录。
- 计划测试位置：`tests/unit/test_prompts.py`（资产、注册、渲染、预算与指纹），`tests/integration/test_f2_prompts.py`（真实入口/Gateway/Trace），复用原配置/导入/ASGI 与 smoke 用例。具体分文件可在最小实施计划中收敛，不改变本方案行为。
- 替换边界：Provider 替身记录真实消息并返回/抛出已指定结果；错误注入仅通过内部依赖，不能把真实路由、注册器、渲染器整体替换成固定成功，也不增加产品故障开关。
- 注册：初始化使用真实代码定义，先验重复/内容再建索引。定义错误在 create_app 路径失败，以 Provider 工厂 spy/调用计数核实停止位置，不只依赖异常/退出码。
- Red：在可导入最小边界用断言证明缺失行为，例如当前入口未接受 variables、模板尚不能识别 context 版本、不会装配示例/写指纹；不把缺模块、拼写、依赖或漏改导入导致的收集错误当有效 Red。同步保存关键原始回执。
- Green：仅实现已确认资产/协议/指纹；先复跑相同失败场景，再完成全部新增与原回归。固定正文黄金、预算临界值和确定摘要均不能从被测实现自身生成后自比。
- 网络/资源：沿用 smoke 的临时 src 副本与从 .env.example 生成的同名 .env，原字节复制，不替换产品入口；`--app-dir <临时目录>/src main:create_app --factory --workers 1` 从无关 cwd 启动，回环随机端口/传递 socket，有界 deadline 等待。失败保留脱敏日志，finally terminate/wait，必要时 kill/wait，再清理文件/socket。
- 并发：不同请求使用不同问题/资料，通过 asyncio.Event 和有界等待控制交错，不用任意 sleep 证明并发；分别核对每次 Provider 输入和请求 ID 对应指纹。

计划命令，在项目目录执行；只在测试方案确认且实施后运行本项新增测试：

```bash
uv sync --locked
uv run python -m pytest tests/unit -q
uv run python -m pytest tests/integration -q
uv run python -m pytest tests/smoke -q
uv run python -m pytest -q
```

方案制定时尚无 F2，实现前运行已有测试不能证明本方案通过；本项实施后实际结果见 3.4–3.6。不同层的重复执行只用于实施阶段定位与最终全量回归，不为凑通过次数反复运行。

### 3.2 独立审查与记录

全部必测与回归通过后，按 [项目工作流](../../../AGENTS.md) 和 [Claude Code 调用经验](../../references/claude-code-review.md) 提交完整实现、资产、设计与测试证据，只读审查。核对实际读取范围、正常退出、is_error=false、最终 verdict 与源码/测试前后指纹；工具启动/中断不代表审查通过。

每项最多三轮完整代码审查；P0–P2 阻断，核实后修复并原场景复测再审；第三轮仍阻断才交用户重审。原文在实际完成后保存本目录 review.md，关键原始回执按需保存 evidence.json，结果与处置写回本方案，不写回原 F1 记录。

### 3.3 当前实际状态

| 事项 | 状态 |
| --- | --- |
| 需求与设计、完整资产 | 已确认，已落文档 |
| 本测试方案 | 用户已确认（2026-10-31） |
| 源码、实际测试、依赖与实际配置修改 | 源码和测试已实现；依赖与实际配置未改 |
| Red/Green、F2 测试与 115 个场景回归 | 已执行，223 passed；实际原始回执见 evidence.json |
| Claude Code 实现审查 | 第 1 轮 APPROVED；P0/P1/P2 均为 0，两项 P3 已记录核实处置 |
| F2 验收 | 通过；最终 223 passed，原回归保留，源码与受审版本一致 |

文档阶段的黄金指纹计算只产生预期值；实现后已由独立黄金资产与实际装配结果逐字比对通过。新增测试、原回归与工厂 HTTP 均已运行，实际结果见下文，不把历史文档检查冒充实现运行。

执行后逐编号补充：实际测试位置/参数化输入、命令、成功/失败数、调用/记录副作用、未执行项、审查轮次/发现/处置与最终结论；模板回看依据实际缺口最小回填，不把具体模板名、预算或 SHA 值推广到通用模板。实施暴露并发断言的关联错误，已按实际证据最小回填测试模板，见下文。

文档阶段核对：已检查固定资产、变量来源与两层预算、错误顺序、指纹及其写入时机、38 组用例和 115 场景回归保护的一致性，未发现阻断测试方案确认的矛盾。此项仅为只读文档核对，不是 Claude Code 实现审查，也不是 F2 运行验收。


### 3.4 实际执行与基线保护

原始回执保存在 [evidence.json](evidence.json)，每条包含真实命令、退出码与完整捕获输出；条目按完成时保存，阶段标签标识用途。

- 基线：实施前收集的原 115 个场景为 115 passed in 1.57s，退出 0。
- Red HTTP：新增 `test_context_version_reaches_provider_once` 与 `test_basic_request_records_message_fingerprint`，2 failed：旧入口返回 422 而非 200；原请求指纹为 None 而非固定黄金值。为断言失败，没有依赖/导入错误。
- Red 注册/摘要：在可导入最小类型边界上运行 `test_registry_rejects_duplicate_key`、`test_fingerprint_matches_independent_golden`，2 failed：重复标识未抛 ValueError；摘要为空而非黄金常量。
- Green 核心：实现真实注册、渲染、示例、摘要与应用注入，unit/integration 为 219 passed in 0.99s；同一代表性 Red 用例已在 Green 中通过。
- 增补 ENV 不入消息摘要的场景，沿用原 smoke 成功测试增加两个模板版本、缺变量和错类型的真实 HTTP 检查；全量为 223 passed in 1.59s。
- 分层最终核验：unit 92 passed in 0.38s；integration 128 passed in 0.33s；smoke 3 passed in 0.74s，退出码均 0，无警告。新增 108 个展开用例；原 smoke 函数增加 F2 断言，不删除旧检查。
- 基线保护：原 46 个测试函数及参数化装饰器保留，原 assert AST 按序全部保留；原 F1/F1-O1 文档、审查与取证工件 SHA-256 不变，实际 .env 的存在性/哈希不变，锁文件/依赖/示例环境文件不变。
- 原 System 黄金正文与三份固定消息/摘要通过；仅检查字符预算，真实 Provider/Token 精确计数未实现，按用户要求留 F3 结合其提供的 Provider 信息讨论。

### 3.5 必测编号与实际结果

下表函数路径均相对于 tests/，参数化变体均已执行；原回归直接复用，不重复造测试。

| 编号 | 实际证据位置 | 结果 |
| --- | --- | --- |
| F2-01 | `integration/test_f2_prompts.py::test_basic_request_records_message_fingerprint` | 省略/空变量两种均通过，原请求兼容 |
| F2-02 | `test_full_context_http_goldens[1.0.0]`；`unit/test_prompts.py::test_context_assets_and_messages_match_approved_goldens` | 两条消息及完整正文/预算/说明/黄金指纹通过 |
| F2-03 | `test_context_version_reaches_provider_once`、`test_full_context_http_goldens[1.1.0]` 与上述资产单元 | 六条精确消息、一次调用、黄金指纹通过 |
| F2-04 | `test_invalid_variable_containers` | 四种结构拒绝通过，零调用/尝试 |
| F2-05 | `test_non_string_variables_are_not_coerced` | 五种类型拒绝通过，无转换 |
| F2-06 | `test_caller_cannot_submit_template_assets` 及原角色/额外字段回归 | 全部通过，内部 assistant 没有放开外部角色 |
| F2-07 | `test_unknown_template_or_version_has_no_fallback` | 未知名称/版本/latest/范围全部通过 |
| F2-08 | `test_missing_variables` | 两版本、省略/空映射全部通过 |
| F2-09 | `test_unexpected_variable_is_not_ignored_or_reflected` | basic/context 两种均通过，私有键不回显 |
| F2-10 | `test_message_is_reserved` | 同值/异值、有无 context 均拒绝通过 |
| F2-11 | `test_blank_context_rejected` | 空/全空白拒绝通过 |
| F2-12 | `test_context_character_budget` | ASCII/中文，10,000/10,001 两侧通过 |
| F2-13 | `test_final_user_budget_includes_template_characters` | ASCII/中文总长 20,000/20,001 两侧通过，无截断 |
| F2-14 | `test_valid_raw_user_can_exceed_rendered_budget` 与原字符边界回归 | 原字段预算不变，最终超限为 400，通过 |
| F2-15 | `test_dynamic_text_is_not_a_template_or_message_role`；`unit/test_prompts.py::test_simple_substitution_is_single_pass_and_static_text_stays_literal` | 空白、标签、JSON、占位符及指令文字均保持原文，通过；不宣称真实抗注入效果 |
| F2-16 | `test_error_precedence` | 五种组合错误按规定最先失败阶段拒绝，通过 |
| F2-17 | `test_unexpected_prompt_error_is_internal[render]` | 500、零调用、null 指纹通过 |
| F2-18 | `test_unexpected_prompt_error_is_internal[hash]` | 500、零调用、无半成品指纹通过 |
| F2-19 | `test_unknown_model_keeps_prepared_fingerprint` | unknown_model、零调用但有正确指纹，通过 |
| F2-20 | `test_failed_call_keeps_fingerprint` | 受控/内部错误两类通过，一次失败尝试和摘要保留 |
| F2-21 | `test_interrupted_call_keeps_fingerprint_unconfirmed`、原 Trace/ASGI 发送失败回归 | 中断传播、running 不记成功、摘要保留，通过 |
| F2-22 | `test_interleaved_contexts_and_fingerprints_are_isolated` | Event 受控交错、两次调用与各自摘要通过 |
| F2-23 | `test_trace_does_not_retain_rendered_data` 与共同拒绝断言 | 正文、示例、输出/异常标记不存储或泄露，通过 |
| F2-24 | `unit/test_prompts.py::test_basic_asset_and_default_registry_are_exact`、`test_context_assets_and_messages_match_approved_goldens` | 三份资产黄金与 2/2/6 消息通过 |
| F2-25 | `test_registry_rejects_duplicate_key`、`test_invalid_template_definition_is_rejected`；`integration/test_f2_prompts.py::test_invalid_assets_abort_before_provider_factory` | 同/异正文重复、标识/版本定义拒绝；应用在 Provider 工厂前停止，通过 |
| F2-26 | `test_invalid_template_definition_is_rejected`、`test_simple_substitution_is_single_pass_and_static_text_stays_literal` | 占位符声明及非法表达式拒绝，合法转义/单次替换通过 |
| F2-27 | `test_invalid_template_definition_is_rejected` | 保留名、非法名、0/负数/字符串/布尔预算及空/错类型字段拒绝，通过 |
| F2-28 | 同上；静态文本字面测试 | 错误示例对/角色与空/错类型正文拒绝；静态例不渲染，通过 |
| F2-29 | `test_registry_and_templates_cannot_be_modified_or_share_caller_containers`；`test_injected_registries_are_application_local` | 深层只读、外部容器隔离及独立应用通过 |
| F2-30 | `test_registry_initialization_is_per_app_not_per_request`；原导入子进程增加注册器禁入探针 | 每应用一次，请求不重建，导入不建索引，通过 |
| F2-31 | `test_versions_coexist_and_switch_back` | 旧/新/旧精确选择通过，无自动升级 |
| F2-32 | `test_json_field_order_and_request_ids_do_not_change_fingerprint` 与黄金摘要单元 | 相同消息摘要一致、记录独立，通过 |
| F2-33 | `test_visible_asset_changes_change_fingerprint`、`test_runtime_content_changes_change_fingerprint` | System、示例正文/顺序、问题与资料变化均改变摘要，通过 |
| F2-34 | `test_management_metadata_does_not_enter_message_fingerprint`、`test_environment_metadata_does_not_enter_fingerprint`、未知模型用例 | 名称/版本/说明/ENV/模型不混入内容摘要，通过 |
| F2-35 | `test_fingerprint_matches_independent_golden`、context 黄金单元与 HTTP | 三个固定 SHA 值逐字通过，Unicode/换行不归一化另有用例 |
| F2-36 | `smoke/test_http_server.py::test_single_worker_real_http_server` | 真实工厂下新模板 200/200、缺变量 400、错类型 422 通过，原场景保留 |
| F2-37 | 原 115 场景完整回归及原函数/装饰器/assert AST 检查 | 全部通过，无场景/断言删除或放宽 |
| F2-38 | 文件与文档核对、受保护文件 SHA-256 对比 | 依赖/配置/原 F1 工件不变，当前路径与文档同步，无范围外管理接口 |

### 3.6 实施自检：并发测试关联

审查前全量复跑曾为 1 failed、222 passed，失败发生在新并发测试的 `zip(responses, provider.calls)`：gather 按输入返回响应，Provider 的进入/记录顺序可能不同，位置配对造成误报。产品代码没有改动。

按 systematic-debugging 先用 Event 保证 Provider 先记录第二个请求，再记录第一个请求；原位置关联断言稳定复现失败（`red-test-correlation`）。随后按预期末条 User 内容查找实际调用，逐条检查完整消息，再以请求 ID 查 Trace 并比对摘要；不放宽原断言，额外保留“第二请求先进入”的反序断言。同场景 `green-test-correlation` 为 1 passed in 0.03s；修正后全量 `final-pre-review` 为 223 passed in 1.51s，退出 0、无警告，原始失败和修复回执均保留在 evidence.json。

这证明本次请求关联断言在反序进入时仍正确。已将“按请求/确定输入关联而非假定列表顺序”回填通用测试模板，其他 Feature 的后续效果未验证。

### 3.7 独立审查

2026-10-31 实际调用原生 Claude Code CLI，第 1 轮完整只读审查完成：[原始报告与取证](review.md)。进程退出码 0，subtype=success、is_error=false、stop_reason=end_turn；最终 **APPROVED**，P0/P1/P2 均为 0。38 次 Read 均返回、无工具错误，覆盖全部 26 份源码/测试及设计、测试方案、diff、证据等。审查方未运行测试；执行者在审查后以 `uv run python -m pytest -q` 复跑，**223 passed in 1.53s**、退出 0、无警告，回执为 evidence.json 的 final-acceptance。

P3 处置：

- P3-1（测试 MappingProxyType 与 dict 相等比较可能不兼容 Python 3.12）：未采纳改代码/收紧版本建议。本机已有 Python 3.12.13，实际最小命令验证 frozen dataclass 值的 mappingproxy 相等比较、键视图差集和 string.Template 校验 API 均通过，退出 0。原报告关于“3.12 比较恒为 False”的推断与实测不符；完整命令/输出在 compatibility_probes。此项只验证标准库行为，不代表完整 F2 套件已在 3.12 运行；全量测试环境为 Python 3.14.7。
- P3-2（既有 httpx2 依赖观察）：确认该声明在 F1-O1 基线已存在，本次依赖与锁文件未变，且本次默认测试通过。仅记录后续依赖维护时可核实的观察，不据此认定错误或添加范围外依赖改动。

收口只更新文档与证据；源码、测试及依赖 SHA-256 与受审快照逐项一致。全部 38 组必测、原 115 场景回归与本地真实 HTTP 冒烟已通过，**F2 验收通过**。F1/F1-O1 历史工件和用户实际 .env 未改；真实 Provider、模型回答语义、注入抵抗效果、Token 限制与生产环境未验证，F3 内容留待 F3 讨论。

### 3.8 用户验收反馈

2026-10-31，用户确认：“验证过了，F2通过”。记录为用户验证通过的反馈；用户未提供本次验证的具体命令或场景，不补写测试细节，也不据此扩展为真实 Provider 或 Token 校验已通过。原自动化测试与独立审查证据保持不变。
