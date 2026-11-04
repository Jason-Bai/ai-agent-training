# F7 代码实现导读

> 状态：代码第2轮独立审查APPROVED，首轮3项P2全部闭环；最终完整真实全量1776通过（含16真实，0跳过/0未选中），验收通过。依据[设计](design.md)、[52组测试方案](test-plan.md)及[实施步骤](implementation-plan.md)。

## 1. 行为变化和阅读顺序

此前一次Attempt失败就结束请求；现在同一请求可在严格次数、时间及输出授权内恢复。公开入口和最终外形保持原协议，只新增initial_output_tokens。每次Attempt独立保存，Request只收口一次。

| 顺序 | 文件/函数 | 看什么、为什么需要 |
| --- | --- | --- |
| 1 | [contracts.py](../../../src/contracts.py)：LLMRequest/StreamRequest | 首次预算是可选严格正整数，max仍授权上限；调用方不能传恢复策略 |
| 2 | [models.py](../../../src/services/models.py)：RecoveryPolicy/ModelRegistry | 固定候选、引用/环校验、3/2额度及受控配置；没有这一层会出现无效候选或隐性调用预算 |
| 3 | [recovery.py](../../../src/services/recovery.py)：RecoveryState | start扣共享次数，decide区分网络/修复/增长，delay及等待域防止换别名绕过Retry-After；纯决策不调用SDK/DB |
| 4 | [gateway.py](../../../src/services/gateway.py)：_complete/_ordinary_attempts | 主模型预检、冻结候选、逐候选装配/计数，再循环调用；修复追加assistant候选+受控user，不改System/Schema |
| 5 | 同文件：complete/_budgets/_aggregate_usage | 按max预留，initial≤max；普通最终提交也受总期限；所有Attempt Usage聚合，未知保留null |
| 6 | [streaming.py](../../../src/services/streaming.py)：_work/_single_attempt/_read_events/_emit | 失败后清理并确认落库，首非空delta提交后永久禁止恢复；每次调用而非只首次受策略约束 |
| 7 | [streaming_manager.py](../../../src/services/streaming_manager.py)：_restore/_admit_provider | 还原冻结计划并检查配置变更；旧模型快照先补默认字段，真实变更仍拒绝；每次新调用复用F6取消准入锁，GET与订阅不能另开执行链 |
| 8 | [providers](../../../src/providers/base.py)及[errors.py](../../../src/errors.py) | 只单次调用，归一化transient/HTTP状态/Retry-After；Gateway无需识别Chat或Responses原始协议 |
| 9 | [store.py](../../../src/persistence/store.py)：begin_attempt/finish_attempt/finish | Attempt完成与Request完成分开；旧Attempt不随请求保存错误重写，状态/事件事务仍唯一收口 |
| 10 | [f7_001](../../../migrations/versions/f7_001.py)、[tables.py](../../../src/persistence/tables.py) | 原三表增加Attempt输入/预算/原因/进入证据；显式迁移，不改旧迁移，不额外建表 |
| 11 | [main.py](../../../src/main.py)：flush_pending和[middleware](../../../src/middleware/request_context.py) | 期限耗尽后延迟保存，不在错误响应前重新等一次提交；补写不把真实成功Attempt改成请求失败 |

## 2. 一条恢复链怎样走

普通A123请求：预检主模型及完整授权→冻结主/备及策略→B起总期限→保存Request→保存Attempt1→Provider连接失败→结束Attempt1但Request仍running→退避→Attempt2→仍失败→备用重新使用其准确消息/计数→Attempt3成功→聚合Usage→唯一Request completed。

结构化格式错误只进入一次修复：原输入+失败assistant数据+固定反馈重新计数，窗口不足直接保留格式错误；修复后无论错误类型都不派生下一恢复链。截断只有预算尚低于授权上限时同模型直接增至上限，不拼接。全部共享3/2次数。

流式先POST创建pending，首次GET才启动。重试等待期间订阅独立发心跳；第一次非空delta已提交就构成内容屏障，即使无订阅者也不能重发。取消接受使用F6锁协调每次Provider启动；取消/存储未知/清理失败后禁止再调用。

## 3. 数据与定位

- 入口错误：查统一code、request_id和确定性预算；零Attempt是预期，不能拿备用纠正非法请求。
- 重复调用或备用未启动：查RecoveryState总/候选次数、Attempt.reason、恢复停止原因、候选能力/窗口及等待域；不要通过改SDK重试解决。
- 格式错误：查schema_fingerprint、原Schema、format_repair Attempt准确输入、实际计数与授权；Schema不保证事实与业务权限。
- 存储故障：Request与Attempt分层看；模型已成功但Request提交超时可以Attempt completed、Request failed。先核对真实PG提交事实，不重调模型来补记录。
- 空白/半回答：对照已提交delta/seq与终态，空白提交也禁止恢复；完整回答是否成功仍需明确completed。
- 耗时：timeout_seconds是整个请求，单次取模型max和剩余较小值。TTFT含恢复等待，Generation只对应最终生成Attempt。实际Provider进入证据不等于供应商已接受/计费。
- 有效输入快照授权保存在PG；瞬时组装输入不进入内存Trace的dataclass元信息，公开HTTP/SSE不含密钥或堆栈。

## 4. 取舍及证据边界

配置示例为[安全Mock文件](../../../config/gateway.example.yaml)，实际gateway.yaml、凭据及AGENTS保持原样。单级候选不是动态Router，模型Token相加不是费用；同服务不同适配协议不代表独立供应商。仍限定单实例单worker/隔离开发环境。

关键Red→Green、实际节点、原始回执及最终审查以[test-plan](test-plan.md)和完成后归档的证据为准。R1前严格全量1748通过/16 live跳过；三项独审修复后最终相关1463通过/16 live跳过/297未重跑（均exit 0）。当前339个新增F7节点与52组映射见测试方案。验证包含Mock、真实PG、本地两协议SDK及TCP/子进程；16个真实供应商调用用例已在最终完整运行中验证；生产容量、多实例和账单未验证。旧测试显式单次策略保留历史错误路径，新F7测试单独证明默认共享额度；旧异步测试补lifespan用于真实归还连接，不过滤警告。

第1轮独审发现并修复3项P2：Provider单次计时排除登记/准入与最终保存；普通失败保存同样受总期限；F6旧pending快照补默认后比较。单次Provider计时自实际启动建立固定截止点，不随delta重置，整次请求期限继续覆盖存储/等待。原始报告及最新证据均在测试方案3.7链接，第2轮独审已APPROVED、P0–P3均0，最终结论见测试方案3.8。

最新完整验收：用户要求全部重跑后，最终代码以--run-live完整执行，1776项全部通过，原297个未重跑项已逐项核对本次JUnit；DeepSeek Chat与OpenAICompatible Responses各8个真实用例通过。详见测试方案3.9及evidence-all-live.json/xml。代码与R2审查版本摘要相同，日常Mock配置及AGENTS未改，未对用户数据库自动执行迁移。
