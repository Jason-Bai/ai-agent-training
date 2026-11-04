# F4-O1：平台常量与 JSON 工具整理——代码实现导读

> 状态：验收通过。对应[设计](design.md)、[测试与实际回执](test-plan.md)、[实施步骤](implementation-plan.md)。原生 Claude Code 第2轮APPROVED，本项不新增真实 Provider 验证。

## 1. 问题与行为变化

原先字符数、Schema 资源上限和固定探测预算散落在调用处，HTTP 状态使用裸数字；共享 JSON 工具直接放在源码根。现在平台限制集中为 10 项常量，HTTP 状态使用已有框架名称，JSON 工具移入通用 utils 包。

所有限制数值、错误文案、接口、Prompt 内容、模型 YAML、计数版本、调用次数和 Trace 语义保持不变。Schema 数值仍精确处理，不把合法小数全部改成正整数要求。

## 2. 核心调用链与组件

`HTTP DecimalRequest → Contract → GatewayService → Prompt/Schema/模型预算 → Provider → JSON/Schema 验收 → HTTP 返回`。

- constants 只提供不可变值，不读文件、不装配依赖；Contract/Prompt/Schema/Gateway 各自消费对应限制。
- utils.json_codec 只负责精确 JSON 表示；HTTP、Schema、计数器、Responses 出站共用，不能授予权限或编排调用。
- http_status 只替换 HTTP 数字；原错误归一化、HTTP 包装与 Trace 责任不变。

## 3. 建议阅读顺序

| 顺序 | 文件与函数 | 读完应能回答的问题 |
| --- | --- | --- |
| 1 | [constants.py](../../../../../src/constants.py) | 哪些是平台限制；相同值为何保留不同名称？ |
| 2 | [contracts.py](../../../../../src/contracts.py)：UserMessage/LLMRequest | 原始输入在哪里检查；OpenAPI 如何引用限制且保持原文本？ |
| 3 | [prompts.py](../../../../../src/services/prompts.py)：validate_selection/build_messages/DEFAULT_TEMPLATES | context 上限和最终 User 上限如何分别生效？ |
| 4 | [validation.py](../../../../../src/services/validation.py)：_raw_resources/_schema_nodes/_expanded_resources/prepare_schema | 字节、两种深度和两种节点上限分别在哪一层检查？ |
| 5 | [gateway.py](../../../../../src/services/gateway.py)：probe/_complete | 探测的 16/10 为什么不是模型 YAML 的默认预算？ |
| 6 | [utils/json_codec.py](../../../../../src/utils/json_codec.py)：loads/dumps | 如何保持数值类型、极大整数、精确小数及迭代深层输出？ |
| 7 | [routes.py](../../../../../src/api/routes.py)、[计数器](../../../../../src/services/token_counters/deepseek_v41.py)、[Responses 适配器](../../../../../src/providers/openai_compatible.py) | 同一工具如何贯穿 HTTP、计数和实际出站字节？ |
| 8 | [providers/base.py](../../../../../src/providers/base.py)、[api/errors.py](../../../../../src/api/errors.py)、[middleware](../../../../../src/middleware/request_context.py) | 上游状态与对外 HTTP 状态怎样保留原映射，如何避免 status 名遮蔽？ |
| 9 | [HTTP 边界测试](../../../../../tests/integration/test_f4o1_boundaries.py)、[Schema 测试](../../../../../tests/unit/test_f4_validation.py) | 如何用独立固定预期保护上下限，并证明零/一次调用与 Trace？ |

## 4. 关键设计与取舍

同值限制不合并：原始 User 与最终 User 均 20,000 字符，但检查位置不同；原始与展开节点均 256，但计算对象不同。命名表达职责，避免今后维护时误改另一层限制。

HTTP 采用 `starlette.status as http_status`，现有局部变量 `status` 继续代表运行时整数；422 使用当前依赖支持的 `HTTP_422_UNPROCESSABLE_CONTENT`，保留数值 422 且不引入弃用警告。

JSON 工具按原字节移动，空 `utils/__init__.py`，旧文件删除，不设置兼容代理。Prompt 内容、Tokenizer 摘要/版本、Schema 白名单与模型 YAML 不属于本项通用常量。

## 5. 成功、拒绝与失败定位

例如三版 `context_answer` 的 context 长度 10,000 字符可通过变量检查，10,001 字符在 Provider 前以 invalid_prompt_variables 拒绝；最终 User 包含标签与输出约定，超过 20,000 字符以 prompt_too_long 拒绝。按 `X-Request-ID` 查内存 Trace，前置拒绝为 0 Attempt；本文不新增 Trace 查询接口。

JSON/Schema 输出不合规仍在一次 Provider 调用后失败，数值精度问题先看 utils.json_codec，再看 validation，出站协议问题看适配器。重构没有引入自动重试、裁剪或新的权限。

## 6. 验证与边界

原完整离线基线 836 通过/6 live 跳过；补齐边界后，旧源码快照上 148 个特征测试通过，迁移后完整离线 878 通过/6 live 跳过。迁移前后 OpenAPI、模板指纹/输入计数、Schema 与 JSON 特征快照完全一致，JSON 工具摘要一致；Python 3.12 关键集 268 通过，与完整回归重叠、不累加。逐组映射与 Claude Code 报告以[测试方案](test-plan.md)中的实际记录为准。

[第1轮](review-r1.md)发现 README 目录树和状态同步遗漏，补充目录树核对后先复现、再修正并复测；[第2轮](review.md)APPROVED，无P0–P2。两项P3的来源标注/归档核实已处理，65份源码/测试/默认配置在两轮间及最终核对均未变，审查后没有改代码或冒充重跑产品测试。

单元、Mock、回环真实 SDK/客户端和本地单 worker HTTP 分别证明各自边界；本次未发起新的真实模型调用，也不声称多实例或生产验证。后续 Streaming/恢复能力仍按各 Feature 单独讨论。
