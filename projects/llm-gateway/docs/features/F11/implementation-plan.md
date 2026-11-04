# F11：两层请求并发实施步骤

> 设计及更新后的23组测试方案均已获用户确认（2026-11-03）。按TDD执行，使用独立任务分工；共同入口由主执行者集成，不依赖Git或worktree。当前运行结果另写test-plan.md，不以计划代替通过。

目标：首次执行原子取得全局/Agent请求名额，同一请求所有重试、退避、修复、fallback及清理持续持有，最终仅释放一次。初始值20/5，Provider配置不增加治理字段。

## 1. 基线及共同接口

- [x] 保存安全文件快照、摘要和原2086测试节点；排除Key、数据库密码、.env及缓存。原Feature/BUG历史工件和AGENTS只读。
- Limiter接口：ConcurrencyLimiter(policy)，同步acquire(agent_id, request_id)返回RequestPermit；检查/递增之间不挂起，单worker事件循环原子。满额抛GatewayError(concurrency_limited, HTTP429)，安全日志记录scope/limit/in_use/request_id。
- RequestPermit.release()幂等；release_when_done(tasks)在全部指定本地Task真正结束后才归还，用于清理超时后的所有权交接。Limiter.snapshot()只供内部诊断/测试，不新增HTTP入口。
- ConcurrencyPolicy为严格配置，两正整数max_requests、max_requests_per_agent，后者<=前者。ModelRegistry显式持有该策略，YAML必填concurrency块。

## 2. 独立任务与文件归属

| 所有者 | 可修改范围 | 依赖与验证 |
| --- | --- | --- |
| 组件任务 | 新services/concurrency.py、tests/unit/test_f11_concurrency.py | 只依赖已确定策略属性；先准备最小接口再取得真实容量争抢Red，不把ImportError当业务Red；覆盖原子、归属、重复释放、清理持有。 |
| 配置/迁移任务 | services/models.py、安全YAML；既有tests中的Registry/YAML及直接Gateway构造迁移；新tests/unit/test_f11_config.py | 不改main/gateway/streaming_manager/streaming入口；历史断言和节点名不删减；配置Red→Green；普通历史夹具显式足额，F10保留20并发/600秒。 |
| 公共验收任务（组件稳定后） | 新tests/f11_scenarios.py、新tests/f11_support.py及integration/test_f11_public_acceptance.py、live/test_f11_real_acceptance.py | 不修改历史夹具/产品文件；使用真实Limiter与Gateway/PG；Mock与真实共用场景，受控故障独立验证。 |
| 主执行者 | main.py、api/dependencies.py、api/routes.py、services/gateway.py/streaming_manager.py/streaming.py、必要的test标记排除与文档 | 统一接线、已有任务/取消/清理所有权；测试先行；不同时写其他任务文件。 |

历史测试迁移中GatewayService直接构造需显式注入Limiter；主链由create_app建立一个共享实例。F10测试Environment增加测试侧concurrency参数供新场景配置，默认显式足额；这不是产品绕过开关。配置迁移任务统一维护tests/f10_support.py，公共验收任务只消费。

## 3. 实施与集成顺序

- [x] 两层配置有效/非法、名额边界/并发争抢/释放各取得Red，保留输出，再最小实现Green。
- [x] 普通/结构化/探测：校验后、prepare/首次费用预留前取得名额；整个恢复循环不再申请；执行停止后释放。写真实Provider进入屏障，验证满额零调用、退避中原请求仍能成功。
- [x] 流式：首次pending认领前取得，Task注册成功交接；认领ACK不确定/建Task失败/Task首次运行前取消回收。request_id锁保证首次唯一。断连/多订阅不重复占位。
- [x] 流生成终止后观察execution.cleanup_tasks；若仍活跃，将凭证交给这些Task和生成Task的完成回调。清理超时不能提前释放；实际退出后回收。取消、重启、存储补写保持原终态/账务归属。
- [x] 接通HTTP429文档与错误元数据，保留pending拒绝后的状态、游标、账户不变；记录安全诊断，不新增本地满额型SSE终态。
- [x] 目标单元→Mock接口/受控双协议→PG/进程→真实共用场景串行集成。组件独立测试可并行，宿主数据库停启/故障套件不得并行。
- [x] 核对C01—C23实际用例映射、原2086节点保留，完善导读及操作示例。配置只合并两项上限，不覆盖维护者Provider绑定或凭据。
- [x] 完整执行已完成，零跳过/筛除，含原四档及600秒；最新原始2242通过/23个真实失败，不代表全部pytest通过。用户随后确认的分层验收按逐项证据复核，另有一次有界六例真实补验通过，详见测试记录第9节。
- [x] 自检、冻结及独审完成：R1使用全新上下文、代码无问题但证据BLOCKED；用户确认新分层口径并补齐实际运行后，同一审查者R2已APPROVED、P0—P3均为0，未超过三轮。README/进度/1.7计划同步；保留原完整23个失败及真实成功率。

## 4. 验证与留证

开发命令与最终命令见[test-plan.md](test-plan.md)。每个任务在临时工作证据目录保存Red/Green命令、完整输出及返回码；由主执行者统一归档。没有真实前置条件时报失败，不偷换Mock；费用不作为跳过理由。若新证据要求改变契约，返回需求讨论；普通实现/测试失败自主闭环。

本计划沿用项目一次合理性自检及一次完整独审门槛，不为各任务叠加多轮完整评估。AGENTS、模板和历史Feature验收工件保持原样。

第1轮后的历史续跑位置：首轮独审代码P0—P3均为0、整体验收BLOCKED；最小旁路已恢复200，但真实八例最新仍6通过/2失败。源代码及门槛不变，待真实流/备用结构化路径恢复后完成未执行容量及全量，见[test-plan.md](test-plan.md#7-首轮独审与审查后复测)。

## 5. 用户确认分层验收后的续跑（2026-11-03）

用户确认“Gateway正确处理外部故障即可按故障处理验收，供应商成功率单列；未归因/Gateway缺陷仍阻断”。定向诊断已在首个请求捕获原始传输字节、解压字节、行、帧、typed增量、下游事件、Trace/SQL/账务及资源：上游仅继续keep-alive、无更多正文或结束，Gateway按30秒总期限正确收口。补充原始记录，不覆盖首轮审查或诊断。

最小续跑步骤：

1. 测试侧增加独立、严格的上游故障证据判定；先写能阻止仅凭错误码/阶段放行的负例和已证实上游停顿的正例，运行Red再实现Green。校验文本/终态/Usage对应、恢复边界、资源/账务，不改产品协议与期限。
2. root负责tests/upstream_observation.py及其单元测试、F10临时bootstrap/Environment/容量接线，以及旧真实fixture留证；独立任务仅负责tests/upstream_availability.py及tests/unit/test_f11_upstream_evidence.py，接口先固定。两者无共享文件写入；集成由root处理。
3. 保留原历史节点和成功路径断言，不把自然故障当成功内容；最终全量保留真实原始pytest结果，分类依据落到逐request_id证据。原四档及600秒不删减。
4. 冻结当前测试变化与未变产品摘要，完整回归后交原独立审查者复核新差分和证据，计第2轮，旧覆盖用摘要复用；必要记录/docs同步，不改AGENTS。

本轮执行与版本边界：产品和运行配置相对R1未改；最终完整2265节点已执行，原始23个真实失败保留；随后只读数据复核、受控探测补写实验/两条真实健康及一次六例治理补验均已留证。补验无项目源码变化，临时插件仅记录最终SQL；不因数据复核重跑相同600秒实验或将1并发改称20并发。第2轮独立复核已APPROVED，F11按新口径验收通过，历史样本边界不改写。
