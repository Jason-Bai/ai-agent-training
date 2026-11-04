# F4-O1：平台常量与 JSON 工具整理——测试方案

> 状态：验收通过。源码整理、完整离线与 Python 3.12 关键验证完成，原生 Claude Code 第2轮APPROVED；原始回执与审查处置如下。
>
> 对应[设计](design.md)。整理方向与本文件测试范围均已获用户确认，按[实施步骤](implementation-plan.md)执行。

## 1. 范围与通过标准

保护 [F4 原验收](../../test-plan.md)以及 F1–F3-O1 的当前有效回归：只整理常量和工具位置，不改变数字限制、HTTP/OpenAPI、Prompt/指纹、预算、精度、Provider 转换或 Trace。

验证层次：纯逻辑、注入 Mock 的 Gateway/HTTP、回环协议服务、单 worker 实际 HTTP 进程、静态引用/文档检查、原生 Claude Code 独立审查。协议测试通过实际 SDK/客户端请求检查出站字节，不用直接构造的请求字典替代。

不测试新能力，不默认运行真实 Provider。既有真实六例不是本项证据；本项不声明供应商可用性或生产可靠性。

通过条件：下列必测组均完成且相关回归通过；迁移前后固定行为一致；实际调用次数与 Trace 符合基线；历史证据、计数资源及用户配置未改；Claude Code 无 P0–P2 阻断项。没有执行的项目明确记为未验证。

## 2. 用例

### 2.1 共同准备与断言

- 所有期望数值、边界样本和黄金结果来自已确认契约/原固定用例，测试不得导入新常量后自动生成同一个期望，避免实现与断言一起漂移。
- 注入完整测试 Settings/Registry、固定计数器或真实本地计数资源、脚本 Mock；不得把 Gateway、校验器、JSON 工具或实际出站序列化替换成固定成功响应。
- 每个 HTTP 用例核对四字段信封、data 形状、`X-Request-ID`、安全错误内容；按 request_id 关联 Provider 调用和 Trace，不假定并发完成顺序。
- 调用前拒绝须 0 调用/0 Attempt；调用后失败须 1 调用/1 失败 Attempt；成功仅一次调用和互相一致的终态。单元/静态检查不产生调用和 Trace时记为不适用。

| 编号 | 准备 → 调用 / 输入 | 预期结果及副作用 | Trace / 其他证据 | 层级 |
| --- | --- | --- | --- | --- |
| O1-C01 | 检查设计中 10 项常量与消费者，扫描 HTTP 裸状态码及旧导入 | 名称/值/单位与设计一致；消费者使用正确语义的名称，原始/渲染限制及两类节点限制不误合并；无新配置或依赖 | 静态核对，不以可导入替代行为证据 | 静态 |
| O1-C02 | 原始 User 长度 19,999/20,000/20,001；空串/全空白；保留原内容 | 前两者通过字段长度校验；20,001 与非法空白为 422/invalid_request；HTTP 有效样本配足模型预算；拒绝 0 调用 | 不截断、不改变原字符串；相应终态与调用数一致 | 单元 + HTTP |
| O1-C03 | context 长度 9,999/10,000/10,001，逐一选择三个现有 context 模板版本 | 前两者通过变量校验；10,001 为 400/invalid_prompt_variables，0 调用；原缺失/额外/保留变量回归 | 失败前未生成的指纹保持原未知语义 | 单元 + HTTP |
| O1-C04 | 先计算固定标签/输出约定占用，再构造最终 User 为 19,999/20,000/20,001 字符；含多字节中文 | 前两者通过字符限制；20,001 为 400/prompt_too_long，0 调用；不把字符数改为 UTF-8 字节数 | 成功按实际完整消息取指纹；失败字段按原阶段记录 | 单元 + HTTP |
| O1-C05 | A 组：固定 Schema 注释内容使规范化 UTF-8 达 16,383/16,384/16,385 字节，包含中文；B 组：超大注释或大量 default 元素，在规范化器设置未调用哨兵 | A 组前两者在其他规则合法时允许，16,385 为 400/invalid_response_schema、0 调用；B 组在完整序列化前被资源预检拒绝，哨兵未触发 | 分别证明最终大小与提前预检；预检和最终字节检查使用同一上限 | 单元 + HTTP |
| O1-C06 | 用 default 内嵌数组构造原始容器深度 31/32/33；另用嵌套业务对象构造展开后容器深度 7/8/9 | 按原容器计数约定，前两者允许，后一者 invalid_response_schema；HTTP 拒绝 0 调用 | 两类深度样本隔离，其他限制配足 | 单元 + HTTP |
| O1-C07 | 原始 Schema 节点 255/256/257；用共享引用另构造原始合法、展开后 255/256/257 | 各自前两者允许、257 拒绝；不把共享定义只计一次或改错比较符 | 原始节点预检先于展开检查，原始 257 样本不要求展开合法；独立计数预期及原引用/循环/节点回归 | 单元 |
| O1-C08 | 注入 Mock 执行两逻辑模型主动探测 | 固定消息不变；参数精确 16 Token/10 秒；一次调用，返回仍仅 latency_ms | provider_probe 用途、预算和成功/失败终态保持原规则 | HTTP |
| O1-H01 | 回归所有原 HTTP 状态/错误码矩阵：入口 400/422、404/405、内部 500、上游归一化 502/503/504；上游 401/402/429/400/404/422/5xx | 状态整数、映射、安全文案、校验优先级与 OpenAPI 响应声明不变；保留各场景零/一次调用 | 对应终态与错误码一致；没有因 status 命名遮蔽产生新 500 | 单元 + HTTP + 协议 |
| O1-J01 | 对新路径运行原 JSON 工具测试：精确小数、极小/极大指数、负零、超长整数、Unicode、排序和数组顺序、深层/循环容器 | 保留原 loads/dumps 类型、值、字节或原异常；迭代序列化不被改为递归；原文件内容迁移核对一致 | 新旧行为特征/固定黄金，不为迁移改动数值期望 | 单元 |
| O1-J02 | HTTP 使用原始 JSON 字节提交 decimal minimum/const；Mock 返回 1.0000000000000001 等边界数值，另测合法金额小数及 1.0 的整数语义 | 精确判定通过/失败；不先 round/float，不强制所有数值为正整数；不把 Decimal 输出为 JSON 字符串 | 返回原始 JSON 数值；调用后失败保留失败 Attempt | HTTP |
| O1-J03 | 重复键、NaN/Infinity、非法/多个 JSON 值、Schema 不匹配、length/refusal 等原故障 | 入口拒绝与 invalid_json/schema_validation_failed/Provider 失败仍按原层次区分；无静默修复/重试 | 错误脱敏；调用前零次，调用后一次失败 | 单元 + HTTP + 协议 |
| O1-J04 | 相同固定消息/Schema经过计数、Chat 请求与 Responses SDK 出站；包含精确小数 | 指纹、规范化字节、输入计数、规则版本及两种协议参数与固定黄金一致；无额外 System；每次请求零隐式重试 | 协议服务捕获实际请求字节和调用次数；Decimal 仍为数字 | 单元 + 协议 |
| O1-R01 | 原单元、集成、协议与单 worker 进程冒烟；Python 3.12 关键子集 | 成功、拒绝、调用后失败、并发隔离及资源关闭均通过；原测试场景/断言不放宽 | 实际命令/退出码/数量/耗时与逐组映射 | 全离线 |
| O1-D01 | 核对源码/测试新导入、当前文档链接、README 目录树/非链接路径及状态；历史工件/固定计数资源/运行配置摘要对照 | 无有效旧模块引用、无旧兼容文件、无循环依赖；目录树与实际源码一致，当前导读和状态匹配；历史工件和用户配置未改 | 历史记录保留旧路径是允许的，不作盲目全库替换 | 静态 + 文档 |
| O1-V01 | 按三维审查模板，由原生 Claude Code 只读需求确认记录、完整变更和证据；独立推导反例 | 核对需求正确性、实现一致性、系统正确性；最多三轮；无 P0–P2 且无关键验收证据缺口，P3 有核实处置 | 归档每轮实际提示词、三维覆盖/反例/未验证项、正常退出及最终结论；子代理/文档检查不替代代码审查 | 独立审查 |

边界样本必须隔离其他约束，例如节点/深度样本用短键并控制在字节上限内；长度样本不得因模型上下文预算不足被错误判成长度回归。优先复用既有样本，缺少的覆盖在实施时只补必要行为断言。

## 3. 执行计划、证据与结论

### 3.1 环境与测试入口

沿用 `uv.lock`、Python >= 3.12 与现有依赖；不修改依赖声明或锁文件。测试只用临时目录、显式注入配置和回环服务。用户实际 `.env`/凭据/模型 YAML 均不改写、不输出内容。

在项目根目录执行，计划命令：

```bash
uv sync --locked
uv run python -m pytest -q
uv run python -m pytest tests/unit/test_f4_json_codec.py tests/unit/test_f4_validation.py tests/unit/test_f4_contracts.py tests/unit/test_f4_prompts.py tests/integration/test_f4_gateway.py tests/integration/test_f4_schema_red.py tests/protocol/test_f4_providers.py -q
uv run python -m pytest tests/protocol tests/smoke/test_http_server.py -q
uv run --python 3.12 python -W error::DeprecationWarning -m pytest tests/unit/test_f4_json_codec.py tests/unit/test_f4_validation.py tests/unit/test_f4_contracts.py tests/unit/test_f4_prompts.py tests/integration/test_f4_gateway.py tests/protocol/test_f4_providers.py -q
```

第一条全量 pytest 在迁移前建立新基线、迁移后作最终回归。关键子集用于迁移过程定位；通过后不无故重复所有测试。默认 live 用例跳过且不访问真实 Provider；这里不计划 `--run-live`。

### 3.2 迁移与证据顺序

1. 先记录源码/固定黄金、历史 F1–F4 工件与计数资源摘要、用户配置摘要（不记录内容），运行迁移前基线。若原环境已有失败，先区分原因，不将其归因于尚未发生的重构。
2. 对照本表查既有覆盖；缺少的行为先补特征断言并在原实现验证。此次外部行为不变，预期这些特征测试原本就通过；纯文件迁移不编造业务 Red，也不把新模块尚不存在造成的导入错误记为有效 Red。
3. 若特征断言暴露已有契约缺陷，保留真实失败并走 Bug 路线；不在本项顺便改变行为。结构目标（常量归属、旧文件移除、新导入）通过静态清单核对，不为文件位置编写镜像实现的永久测试。
4. 提取常量和 HTTP 引用、迁移原样 JSON 工具，每步运行相同相关回归。涉及路径迁移时，测试仅变更导入/必要监视位置，不放宽行为预期。
5. 完成最终离线、3.12 关键验证、实际 HTTP/协议证据和摘要/文档核对；在本目录记录原始回执和逐组映射，再产出 `implementation-summary.md`。
6. 按[Claude Code 操作经验](../../../../references/claude-code-review.md)及[三维提示词模板](../../../../templates/ClaudeCode审查提示词模板.md)执行完整只读代码审查，执行前在本目录归档每轮实际提示词。审查者要求的最小实验由执行者运行并回传原始结果；修复须原场景复测，第三轮仍阻断则按工作流交用户讨论。文档阶段的结构核对不声称完成该审查。

原始命令/输出、退出码、测试数量和未验证项只在本项目录的实际证据与本文件记录，不覆盖历史回执。临时 HTTP 服务/子进程/客户端在完成后释放。

### 3.3 当前执行状态与结论

| 项目 | 当前状态 |
| --- | --- |
| 用户设计方向确认 | 已确认；具体整理清单见设计 |
| 本项测试方案确认 | 已确认（2026-11-01） |
| 新基线、特征测试与迁移 | 已完成，见 3.4 |
| 完整离线 / Python 3.12 / 协议 / 实际 HTTP | 已完成，878 离线通过/6 live 跳过；3.12 关键 268 通过 |
| 原生 Claude Code 代码审查 | 第1轮 BLOCKED；修正文档后第2轮APPROVED，P0–P2=0，两项P3已核实处置 |
| 本项真实 Provider 验证 | 不在本次计划内，未验证 |
| 验收结论 | 全部本项必测通过、原生独审无阻断，验收通过；本项真实 Provider 未验证 |

文档阶段核对（2026-11-01）：本项两份方案及 README、项目 AGENTS、进度共 5 份文档的 96 个本地链接与计划命令中的测试文件路径均存在；原 `src/json_codec.py` 仍在，目标 `src/constants.py` 与 `src/utils/json_codec.py` 尚未建立。本次核对只证明文档入口和阶段状态，不代表项目测试或独立代码审查通过。

只读文档一致性复核未发现阻断项；按建议补充 C05 两类字节样本、C06 深度构造方法及 C07 节点预检先后关系。该复核不是 Claude Code 代码审查，不占实施后的三轮审查。

模板回看：初次检查确认原模板已覆盖独立优化基线、边界两侧、跨边界字节、副作用与证据。随后用户提出并确认三维审查框架，已同步项目工作流、提示词/测试模板与本项 O1-V01；该次回看时本项尚待测试方案确认。当前确认及执行状态以上表为准。无行为变化的特征验证方法在此说明，不把导入失败伪造成 TDD 成果；新审查框架的实际效果待验证。

### 3.4 实施中的实际回执

- `uv sync --locked`：成功，沿用现有锁文件。
- `baseline`：完整离线 836 passed / 6 live skipped，52.04 秒。
- `characterization-before`：补齐原始/展开 Schema 两侧边界及三版模板字符预算后，迁移前 139 passed。
- `characterization-baseline-snapshot`：对旧源码快照追加 HTTP Schema 边界时，9 failed / 139 passed；根因是临时快照未复制相对路径加载的计数资源。此为验证环境缺口，不是产品 Bug 或有效 TDD Red。
- `characterization-baseline-complete`：只补齐临时快照的原固定 assets，原场景复测 148 passed；未修改产品行为。
- 迁移前已保存 OpenAPI、全部模板消息指纹/输入计数、Schema 与 JSON 精确数值特征；迁移后以同一脚本比对。

- `final-offline`：878 passed / 6 live skipped，53.86 秒；包含协议替身、单 worker 子进程 HTTP、并发/Trace 与 F1–F4 历史行为回归。本项比原基线增加 42 个参数变体/用例，原行为断言未放宽。
- `python312-critical`：268 passed，5.82 秒，弃用警告视为错误。使用独立临时虚拟环境，避免切换项目默认 `.venv` 的解释器；该子集与完整回归重叠，不累加测试总数。
- OpenAPI/全部模板消息指纹与计数/Schema/JSON 特征快照前后摘要均为 `b193239ee52ef7c99ae359c8e90cd1a4ae77ec399c11f58140332c62f857ae0a`；迁移前后 JSON 工具摘要均为 `07910b5a075b7c2c4c12e2b18a325d8777d154817bfbc7e18c4ec26980b8998c`。
- 43 份配置、锁文件、固定计数资源和历史工件摘要未改；原 F4 实现导读只修正失效源码链接并标注迁移来源，不修改其验收记录。

原始命令、输出与摘要记录在 [evidence.json](evidence.json)。原生 Claude Code 第1轮已完成并给出 BLOCKED，后续处置见 3.6；真实 Provider 不在本次计划内，未调用。

### 3.5 用例与实际验证映射

| 用例 | 实际覆盖 / 证据 | 当前结果 |
| --- | --- | --- |
| C01 | 常量值/消费者、HTTP 裸数字、旧导入、codec 字节与依赖静态核对 | 已核对，证据记录实际输出 |
| C02–C04 | integration/test_f4o1_boundaries.py 三个字符边界测试 + 原 F1/F2 空值/变量/消息黄金 | 通过，三版模板及调用/Trace 断言保留 |
| C05–C07 | unit/test_f4_validation.py 五个边界测试及 large_shallow_annotation 哨兵；integration/test_f4o1_boundaries.py 的 Schema HTTP 边界 | 通过；原始资源与展开资源独立构造 |
| C08 | protocol/test_f3_gateway_probe.py 固定探测/真实客户端回环 + integration/test_f3_gateway.py 的探测用例 | 完整离线通过，16/10 不变；不是云端探测 |
| H01 | unit/test_f3_provider_results.py、test_f3_result_boundaries.py；integration/test_http.py；protocol/test_providers.py/test_f3_gateway_probe.py；OpenAPI 前后快照 | 通过，状态/映射/回执未变 |
| J01 | unit/test_f4_json_codec.py 全部原场景 + 文件摘要 | 通过，工具只改位置 |
| J02–J03 | unit/test_f4_validation.py 精确数值/严格 JSON；integration/test_f4_gateway.py 的原始 wire/decimal literal/round_trip/allowed_extra_json；原拒答与截断用例 | 完整离线及 3.12 关键路径通过 |
| J04 | unit/test_f4_budget.py、test_f3_token_budget.py 黄金；protocol/test_f4_providers.py 的 structured_outbound_contract/sdk_emits_exact_schema_numbers；前后特征快照 | 通过，实际客户端出站字节和指纹不变 |
| R01 | final-offline / python312-critical | 878/6 skip 与 268 通过，非相加 |
| D01 | 配置/资源/历史摘要、有效路径与链接核对 | 按实际核对记录，不重写历史验收 |
| V01 | [第1轮提示词](review-prompt-r1.md)/[原文](review-r1.md)、[第2轮提示词](review-prompt-r2.md)/[原文](review.md) | 第2轮APPROVED，首轮发现及二轮P3已闭环 |

### 3.6 原生 Claude Code 第1轮与处置

实际使用原生 CLI，沿用现有配置，仅 Read；进程正常退出 0、is_error=false、stop_reason=end_turn。原文见 [review-r1.md](review-r1.md)，P0=0/P1=0/P2=1/P3=1，结论 BLOCKED。读取覆盖与工具回执以 evidence.json 的实际事件核对为准，不只采信报告自述。

- P2-1 成立：README 自称当前源码的目录树仍列根目录 json_codec，遗漏 constants/utils。此前静态检查只验链接，漏掉树内非链接路径。先扩充临时静态核对并实际失败于 `src/json_codec.py` 不存在（readme-tree-red），再最小修正目录树；原场景复测见最新回执。
- P3-1 成立：设计、进度、README、AGENTS 的阶段标签滞后；已统一为待独立审查，与实际状态一致。
- 两项均仅改文档；源码、测试和默认配置相对首轮审查的 65 份摘要未变，原完整离线与 3.12 回执继续适用，无须无故重跑产品测试。
- 闭环回看：该遗漏可推广到目录迁移，已给测试模板补充目录树/非链接路径/示例导入及进度状态核对要求，并补足本项 D01。验证脚本与真实源码逐项比较 README 树，不新增镜像实现的产品测试。
- 首轮结束时尚未验收；原场景文档复测通过后提交第2轮，不增加用户确认节点、不重置轮次。最终结论见 3.7。

### 3.7 第2轮与最终验收

原生 CLI 第2轮正常退出0、is_error=false、stop_reason=end_turn，[原始报告](review.md)结论APPROVED：P0=0/P1=0/P2=0/P3=2。本轮18项必读全部显式读取，另抽读3份源码，读取错误0；对未变代码引用首轮静态审查，逐项核对65份摘要、重读差分全文并抽查当前文件，明确未重读边界，没有冒充重跑测试。两轮实际提示词及原始报告均保留。

第2轮P3处置：

- P3-1 成立：原临时 review2-evidence 摘要未在顶部明确区分沿用回执与本轮新增执行。现提供[持久化来源标注版](review-evidence-r2.md)，明确878/268为首轮前已执行的产品回执，本轮仅新增目录树失败复现/文档修复复测；原摘要内容保留，不把新增标注冒充当时已存在。
- P3-2 为待验证风险，经核实未发生：evidence.json 的 runs 已持久保存 readme-tree-red（exit1、明确目录树断言失败）和 review-document-fixes（exit0、26条源码路径匹配）的完整原始命令/输出。已实际加载 JSON 并检查两条记录、退出码与关键输出，非只依赖临时目录；报告原文不改写。

最终结论：878个离线测试通过/6 live按默认规则跳过，Python3.12关键268通过（重叠不相加）；前后行为快照与工具摘要一致，43份配置/资源/历史工件保持原样，65份源码/测试/默认配置在首轮后及二轮后均未改变。模板、当前目录树/路径、状态、导读和证据已同步。按确认方案与两轮原生三维审查，F4-O1验收通过；本次未新增真实模型调用，不声称多实例或生产验证。

### 3.8 用户验收反馈

2026-11-01，用户明确回复“验收通过”，确认 F4-O1 验收结果。本条为用户反馈记录，不增加新的自动化测试或真实 Provider 调用证据；既有回执与两轮审查原文保持不变。
