# F11：两层请求并发——代码实现导读

> 状态：实现已接入；首轮完整2153通过/5个真实节点失败，旁路上游HTTP503，整体验收阻塞；[首轮独审](review-r1.md)代码P0—P3为0，整体验收BLOCKED，审查不得替代缺失真实验证。对应[设计](design.md)、[23组测试方案](test-plan.md)及[实施步骤](implementation-plan.md)。本次仅全局/Agent请求额度，不含上游组。

## 1. 问题与行为变化

以前配置和调用预算有效就会进入执行，Gateway没有统一的本地并发准入。现在首次执行前原子检查全局和可信agent_id额度，满额返回HTTP429/concurrency_limited，零Provider调用、零Attempt/费用预留。有效请求使用一份RequestPermit同时占两层额度。

重试、退避、结构化修复、输出增额和fallback都在同一请求生命周期内，不重复申请。流式POST仅创建pending，首次有效GET才准入；满额保持pending。重连、订阅数量、已提交终态不决定生成名额。名额在本地执行及清理实际停止后归还；未知Usage仍按旧账务规则处理。

## 2. 调用链及组件

普通/探测：API依赖注入应用共享Limiter → Gateway确定性校验/完整预算 → acquire → prepare/各Attempt → 结果验收及旧调用清理 → finally释放。普通路径的最终存储写入仍由原_recorded_completion处理；已停止的执行不因后台补写永久占名额。

流式：POST保存pending → GET归属/游标校验 → request_id认领锁 → acquire → claim_stream → 注册后台Task并交接凭证 → 原StreamingExecution执行全部Attempt及清理 → _release_permit。没有在执行器每次stream调用处加新准入。

清理超时：_generate读取执行器现有cleanup_tasks，将凭证登记到尚未结束的清理Task及当前生成Task。RequestPermit.release_when_done逐个观察实际完成，最后一个Task退出才释放。Task.cancel只提出中断，不当作结束。首次Task尚未运行就取消时，_task_done回调负责收回凭证；认领失败/ACK不确定/建Task失败时也撤回。执行器原清理实现不为本功能重写。

## 3. 阅读顺序

| 顺序 | 文件/入口 | 读完能回答什么 |
| --- | --- | --- |
| 1 | [models.py](../../../src/services/models.py)：ConcurrencyPolicy、ModelRegistry、load_registry | 为什么YAML必须显式提供两项严格正整数，且Agent<=global；Provider定义为什么不变。 |
| 2 | [concurrency.py](../../../src/services/concurrency.py)：ConcurrencyLimiter.acquire | 如何在单worker事件循环中无挂起地检查两层并一次占用；为什么拒绝无部分获取。 |
| 3 | 同文件RequestPermit.release/release_when_done | 为什么用所有者凭证防重复释放；如何在取消后仍持有，避免提前空出容量。 |
| 4 | [main.py](../../../src/main.py)、[dependencies.py](../../../src/api/dependencies.py) | 应用只建一个Limiter，为什么每次创建GatewayService不会获得一套新额度。 |
| 5 | [gateway.py](../../../src/services/gateway.py)：_complete | 准入放在整个恢复循环外，为什么满额期间原请求仍能重试成功；验证错误为什么优先。 |
| 6 | [streaming_manager.py](../../../src/services/streaming_manager.py)：subscribe、_generate、_task_done、_release_permit | 名额如何从GET交给后台；断线为什么不释放；Task未启动及迟到清理怎样回收。 |
| 7 | [routes.py](../../../src/api/routes.py)及既有错误/Trace | 哪些接口声明429，为什么POST创建、取消、读Trace/账务/事件不做生成准入。 |
| 8 | [生命周期专项](../../../tests/integration/test_f11_lifecycle.py)、[普通恢复专项](../../../tests/unit/test_f11_ordinary_lifecycle.py) | 如何复现重试期间其他请求被拒绝、原请求成功，以及取消/存储/清理故障下的真实占用。 |
| 9 | [共用步骤](../../../tests/f11_scenarios.py)、[测试进程屏障](../../../tests/f11_support.py) | 同一业务断言怎样分别执行Mock/真实Adapter；屏障为何不伪造Provider输出或并发控制。 |

## 4. 配置及历史测试迁移

运行/示例YAML仅合并concurrency.max_requests=20及max_requests_per_agent=5；未覆盖已有Provider绑定，不读取/修改Key。旧YAML缺块会明确启动拒绝，需要维护者迁移。没有数据库迁移、新表或公共管理接口。

ModelRegistry程序内构造需显式策略，GatewayService/StreamingManager需注入Limiter。历史测试因此补显式策略、测试身份及YAML；不删旧节点或业务断言。历史F10测试Environment默认显式64/64，以容纳原20并发及600秒负载；这只是测试条件，实际运行配置仍20/5。新治理场景用1/1、2/1验证边界。

测试新增入口避免同basename导入冲突：unit/test_f11_concurrency.py、unit/test_f11_config.py、unit/test_f11_ordinary_lifecycle.py、integration/test_f11_lifecycle.py、integration/test_f11_public_acceptance.py、live/test_f11_real_acceptance.py。公共integration本身使用独立进程/TCP/PG，不另建重复smoke。

## 5. 验证、定位与边界

- 正确Red：满额仍进入第二次HTTP调用，实际200、期望429；组件另取得容量、活跃清理及异常观察反例。配置缺块原实现未拒绝也先失败。
- 当前已运行：组件13通过、配置/旧解析批次249通过、生命周期及恢复相关130通过、公共Mock6通过、真实6通过；各批次有重叠，不相加当最终全量。
- 开发中两个测试假设已修正：Task首次执行前取消后的SQL由原1秒后台写入收口，测试等待实际终态；非协作关闭反例可同时有多条清理调用，检查仍活跃时持有、全部退出后释放，不错误假定永远只有一个aclose。
- 首轮完整2153通过/5个真实节点失败，全部非live2124通过；后续真实八例8失败，旁路上游503。容量仅完成单并发阶段，后续档位/600秒未执行；[来源及边界](test-plan.md#6-首轮完整回归与当前阻塞)保留，整体验收未通过。

定位时先区分concurrency_limited与provider_rate_limited。前者查看request_id对应日志的scope/limit/in_use：global是全局已满，agent是同一可信Agent已满；被拒绝普通/探测Trace无Attempt和费用。pending GET拒绝只留访问诊断，不污染原生成。名额未释放先找生成及cleanup Task是否真的done；本地已停后仍占用才追查凭证交接。

供应商正式账单、长期生产容量、多实例协调未因F11得到验证；两条真实协议仍同属DeepSeek。历史Trace/事件保留策略及其增长边界保持，不把本地名额释放当作供应商已停止生成或计费的证明。

第1轮后的复测记录：最小旁路已返回200，但原八个真实节点仍6通过/2失败，未达到最终门槛。当前实现、配置及原始审查输入保持，后续按[测试方案第7节](test-plan.md#7-首轮独审与审查后复测)补齐真实验证。

## 6. 分层验收补证与测试阅读顺序（2026-11-03）

用户确认把供应商成功率与Gateway正确性分开。产品源码和运行配置没有因此修改，不新增重试、熔断或路由；原首轮审查与回执保持原文。最新状态见[第二次诊断](diagnosis-r2-20261103.md)及[测试记录第9节](test-plan.md#9-用户确认分层验收后的完整执行)。

测试侧新增[原始HTTP观察器](../../../tests/upstream_observation.py)及[严格证据分类](../../../tests/upstream_availability.py)，分别覆盖真实传输/解压内容与typed文本、上游状态、读取结束，和raw/typed/downstream、deadline、Usage、资源及SQL核验的关联。对应单元测试保留缺失观察、漏读终态/正文、错请求、错误资源和费用的Red。原真实夹具只增加留证，不删除成功断言；F10只有在独立SQL/费用/账户检查及排空后才评价失败证据，未支持类型仍返回未分类。

完整回归原始结果2242通过/23个真实失败，全部2231非live通过，原四档和600秒均执行；600秒真实成功率24%，不能宣称供应商稳定。上游HTTP503确实有两次尝试，原body/SSE读取未在总期限内完成也已记录；未知Usage仍pending，不假零。源代码/配置未改后有界补验6个F11真实场景全部通过，附最终SQL/账务及名额快照；补证不改写旧失败。[第2轮独立复核](review-r2.md)已APPROVED、P0—P3均为0，F11按用户确认的分层标准验收通过，范围与留证限制见[最终处置](test-plan.md#10-最终验收与第2轮处置)。
