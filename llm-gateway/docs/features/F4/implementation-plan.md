# F4 最小实施步骤

> 用户已确认完整文档并于 2026-11-01 授权开始。按 writing-plans、test-driven-development 执行；项目既有工作流优先，不创建 Git 分支、提交或额外批准节点。独立验收统一使用原生 Claude Code，最多三轮。

目标：交付已确认的 Schema/Prompt/预算/Chat 与 Responses/验收闭环；架构和契约以 [设计](design.md)、用例以 [测试方案](test-plan.md) 为准。新增 services/validation.py；用户后续确认完整十进制保真后增加四处复用的 json_codec.py，测试按原目录分层。细粒度记录保存在同目录测试方案与 evidence.json，不复制整份实现代码到计划。

## 1. 基线与 Schema 验收

- [x] 运行 `uv run python -m pytest -q`，保存当前基线；记录源码/配置/历史工件摘要，敏感文件仅取摘要不输出内容。
- [x] 先增加 `/v1/llm` 带空 Schema 应为 400 的测试，看到当前 422 的目标失败；导入错误不算 Red。
- [x] 增加 tests/unit/test_f4_validation.py，覆盖 S01–S22/N01–N03/V01–V08；新增 services/validation.py 的 prepare_schema、validate_output。先限原始资源，再验证关键字/引用/展开资源，最后规范化；无网络引用解析。
- [x] 添加并锁定 jsonschema 4.x；运行该单元集及 Python 3.12 关键兼容。跨组件集成后用最初 HTTP 场景验证 Green。

## 2. 平台请求、配置、模板与计数

- [x] tests/unit/test_f4_contracts.py、test_f4_prompts.py、test_f4_budget.py 与集成测试先写目标失败。
- [x] contracts.py 增加严格可选 response_schema、独立 StructuredLLMData，SuccessResponse 联合类型保证普通文本不增加 parsed。
- [x] models.py 增加必填 api_protocol/mode 与受控组合校验；只迁移本项必要 YAML/既有夹具，不改变历史验收工件。
- [x] prompts.py 增加支持标志与完整 1.2.0 Prompt，保留旧三份资产；预检模板变量与装配分开，供结构化流程按正确阶段调用。
- [x] token_budget.py/计数策略增加可选原生 Schema，使用同一份规范化内容与独立参考黄金，更新计数规则版本，保留文本编码结果；验证窗口/字符临界值。

## 3. 统一 Provider 契约与协议

- [x] 协议测试先验证 SDK 需要 /responses、原生 text.format 及状态解析，记录当前 Chat 出站导致的失败。
- [x] Provider.complete 增加可选 response_schema；ProviderResult/ProviderCallError 增加受控上游状态字段；Mock 记录 Schema 并保留无 Schema 的原参数形状。
- [x] DeepSeek 只在 Schema 请求加 json_object；OpenAICompatible 改为实际 AsyncOpenAI Responses；分别解析原始状态和 Usage，不使用 SDK 强制转换掩盖异常。
- [x] 迁移既有 protocol 夹具为对应协议，保留错误、超时、并发、无重试、重定向和关闭断言；增加 F4-W01–W09。

## 4. Gateway、Trace 与完整回归

- [x] 集成测试先覆盖前置错误顺序、完整成功、截断/拒答优先、JSON/Schema 失败与原始元数据保留；新增字段前运行相关 Red。
- [x] gateway.py 编排结构化预检/能力/装配/预算/一次调用/结果验收，trace.py 保留指纹与上游状态；api/routes.py 引用完整规则并保留集中错误信封。
- [x] 按 request_id 测试受控并发与共享 Schema 不变异；smoke 的临时单 worker 服务覆盖成功/拒绝/输出失败。
- [x] 运行 unit/integration、protocol、smoke 和完整默认离线集；修复只涉及本项迁移的回归，保留原断言目的；不依赖开发者运行配置。

## 5. 真实、文档与独立验收

- [x] 扩展现有 live 文件为总计六例，默认跳过；按已确认预算与固定 Schema 显式运行，必要复测单独记录，Key 仅本地读取。差值非零先失败并定位，不改余量掩盖。
- [x] 对照测试矩阵逐项记录实际用例/命令/结果，归档安全元数据，更新 README/AGENTS/契约/规则/进度和 implementation-summary.md；回看模板。
- [x] 原生 Claude Code 只读审查完整变更与证据；核对实际 Read、最终报告、正常退出和前后摘要。前两轮阻断自行复现修复，第三轮仍阻断才交用户；工具故障不伪称验收。
- [x] 最终报告离线、协议、实际进程、真实 Provider、独立审查的分别证据及未验证边界。

## 十进制补充与当前交接

用户已确认完整保真。共享编解码、Schema/canonical、计数与真实SDK content字节均已接入，原HTTP舍入Red已Green；最终离线836/3.12关键233/真实六例通过，原始失败及复测均独立保留。Claude Code第1轮已APPROVED，无P0–P2，不创建分支/提交或额外批准节点。
