# F12实施步骤

> 设计与28组测试方案均获用户确认（2026-11-03）。使用writing-plans、TDD及项目允许的独立任务分工推进；无Git前提，不创建worktree或提交。测试、配置与普通/流式执行链统一集成，最终一次自检及最多三轮全新上下文独审，不重复开展同目的评估。

**目标**：以受控模型组选择端点，保持统一入口、恢复、并发、计费、取消和续传闭环。

**结构**：Registry冻结三层定义；Router返回端点专属消息/计数及元数据；Gateway/F7消费同一计划；流式保存并核对相关配置；Store/Trace持久化解释。Python3.12、FastAPI、Pydantic、现有双协议Adapter、PostgreSQL及pytest。

## 1. 固定内部接口与文件所有权

- 配置任务：`src/services/models.py`及`config/gateway.example.yaml`、新增`tests/unit/test_f12_config.py`。`EndpointConfig`为端点定义；`ModelGroupConfig`为组定义；`ModelRegistry(endpoints=..., providers=..., model_groups=..., concurrency=..., recovery=...)`；使用`.endpoints`、`.model_groups`、`.get_endpoint(id)`和`.get_group(name)`。允许`ModelConfig = EndpointConfig`保留既有Adapter类型注解名称，但不接受旧字段或旧YAML。恢复字段改为`max_attempts_per_endpoint`，旧格式拒绝。
- 历史测试迁移任务：只改既有`tests/`文件和必要的测试侧转换辅助，不修改生产源码/配置/AGENTS或历史工件。旧fixture输入可由测试侧助手拆分成新端点/组，生产解析器不新增兼容回退；旧断言按真实语义保留，变更节点建立映射。
- 新增F12验收任务：只写新的`tests/*/test_f12_*.py`及`tests/f12_cases.py`、`tests/f12_scenarios.py`，不改配置任务的新配置测试或旧测试。首条业务Red由集成者建立后移交，断言保持“停用主选、只调用备用”。
- 统一集成者：其余生产源码（routing、Gateway、Streaming、主入口、TokenCounter类型接线、恢复、持久化/Trace）和文档。测试作者不得写产品开关或替换被测Router/计费/准入为恒定成功。
- 公共输入：组名与端点ID可以不同；旧fixture可保留同名ID以稳定历史节点，新F12必须用不同ID验证没有直接绑定残留。新增测试basename唯一。
- Router计划的每个合法候选包含`endpoint_id/endpoint/provider/messages/input_token_count/counter_version`；公开routing元数据严格按设计。执行快照记录版本、相关配置摘要、全部候选安全配置及每候选消息/计数，不含密钥值。

## 2. 基线及首个Red

- [x] 保存安全源码/测试/配置示例/文档快照、摘要及原pytest收集清单，排除凭据、.env、密码、虚拟环境和历史二进制证据包。
- [x] 编写通过现有业务入口观察的停用主选用例，运行并保存因主选仍被调用导致的正确失败；导入/准备错误不算业务Red。
- [x] 确认配置/历史迁移/新验收任务的接口和所有权后分工；禁止共同文件并发修改。

## 3. Registry与路由（T01—T12）

- [x] 配置任务先真YAML解析Red，再实现严格三层定义、引用/预算/协议校验、不可变注册表和安全示例；旧顶层及恢复字段拒绝。
- [x] 新验收先写有序选择、停用/能力/预算筛选及阶段耗尽错误Red。
- [x] 实现`src/services/routing.py`，逐候选构建真实Prompt、生成约束及计数，按授权最大输出预检；返回不可变有序计划，不调用上游。
- [x] 接普通/结构化入口，观察同一首Red转Green；执行两种结构化模式/计数/精确十进制与相关回归。

## 4. 恢复、流式及记录（T13—T25）

- [x] 恢复以端点ID计次，保留总3/端点2及F7错误边界；每个实际Attempt使用自己的消息、计数、Provider、价格，F11只取得原请求一份名额。
- [x] 流式POST存版本化计划；首次GET核对相关配置，关联变化或旧pending不等价则明确failed；重连、历史读取及取消不重新路由。
- [x] Store保存routing元数据；Trace新增nullable routing并保留历史null、兼容skipped投影；普通响应不扩展内部选型字段。账务沿用每Attempt实际取价/预留/结算。
- [x] 健康接口只选首个启用端点、至多一次、无fallback；healthz与只读接口不触发路由调用。验证价格/余额/存储失败及清理退出副作用。
- [x] 执行Mock公共步骤、受控双协议、PG/进程/屏障专项及定向历史回归，记录正确Red/Green及修复来源。

## 5. 集成与真实验收（T26—T28）

- [x] 历史测试迁移保持原场景/断言语义，收集清单核对无删节点/skip/xfail掩盖失败。
- [x] 更新必要README/架构/契约/配置迁移说明，保留维护者当前Mock/真实绑定和所有密钥；AGENTS不修改。
- [x] 按测试方案执行真实矩阵，分别记录Mock、受控故障、混合链及真实业务成功/故障证据。
- [x] 稳定版本完整执行`uv run --python 3.12 python -W error -m pytest --run-live -ra -q`，包含四档及600秒真实容量。按已确认分层标准记录原始返回码/失败，不把外部故障自动豁免。
- [x] 形成28组/实际节点映射、完整原始回执、代码导读及一次契约/副作用/文档自检；需要补验说明版本边界。
- [x] 冻结完整安全基线与真实差分，执行前保存实际提示词，首轮全新上下文Agent只读三维独审；缺陷复现修复及相关复测后同一审查者复核，最多三轮。
- [x] 满足标准后报告F12验收与真实成功率/未验证边界，F13仅在F12完成后另行讨论，不提前实现。

当前已完成全部步骤：修复后完整2508通过（42个真实节点、零跳过），真实容量16784笔全部完成、20并发600秒通过；累计第2轮独立复核APPROVED，F12验收通过。首轮2497及两项P2的原始记录保留，F13仅交接主题与已验证基础，不提前实现。
