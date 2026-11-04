# F10 验收工具导读

F10交付总验收工具、操作手册和报告，未修改产品源码、模型绑定或数据库迁移。基线为F9-B1验收后的2010用例；全部保留，新增测试覆盖真实调用方使用链、容量持续运行和核对工具反例。[设计](design.md) · [测试方案](test-plan.md) · [操作手册](acceptance-guide.md) · [报告](acceptance-report.md)。最终完整回归2039通过，真实容量16935笔完成核验，首轮独立审查APPROVED，无P0—P2，验收通过。

## 1. 建议阅读顺序

| 文件 / 函数 | 作用及需要核对的边界 |
| --- | --- |
| [f10_support.py](../../../tests/f10_support.py) / f10_environment、Environment.start/cli | 测试专属PostgreSQL空库迁移、真实CLI建号/签发/拨款、独立Uvicorn单worker。真实Provider Key仅引用现有本地文件，不打印；配置和账户都隔离于开发库。 |
| 同文件 / BOOTSTRAP、upstream_answer | 在测试启动文件中只读观测活跃流任务、原始上游正文/增量和HTTP失败状态；原解析器返回不变。ContextVar关联当前请求，小数用项目精确JSON工具留证，观察失败单独计数。 |
| 同文件 / call、verified_quality | 真实TCP/Bearer调用；业务计时在收到结果时结束，之后再读取上游证据。所有成功文本要求与原文相同；功能场景检查事实，容量把模型质量单列。 |
| 同文件 / verify、assert_balances | 独立SQL核对互斥终态、连续事件序号、Attempt/账本键、归属、结算和未知预留；精确Decimal逐笔复算金额，账户公式与HTTP总览一致。 |
| [test_f10_acceptance.py](../../../tests/live/test_f10_acceptance.py) | 两协议×普通/结构化/流式六例，首次文本后真实断连续传两例，pending及运行中取消两例，轮换/归属/重启/零预算一例。 |
| [test_f10_capacity.py](../../../tests/live/test_f10_capacity.py) / phase、drained_sample | 闭环四种协议/方式轮转，1/5/10/20并发各60秒，选符合硬标准最高档位连续600秒；停派发后真实收口，每5秒采样，逐请求查库。 |
| [f10_metrics.py](../../../tests/f10_metrics.py) / summarize、validate_records、validate_sustained | 最近秩分位、派发/批次吞吐、质量计数；HTTP/SSE与SQL终态对应。持续阶段仅接受明确Provider阶段的可用性失败，本地故障不通过。 |
| [test_f10_fault_chain.py](../../../tests/smoke/test_f10_fault_chain.py) | 可控上游主失败/备用成功后，通过真实CLI核费、确认零费、平台承担及幂等重放三条路径；不伪称真实供应商账单。 |
| [test_f10_verification.py](../../../tests/postgres/test_f10_verification.py)、[指标](../../../tests/unit/test_f10_metrics.py)、[留证](../../../tests/unit/test_f10_observation.py) | 用故意不一致的PG记录和纯逻辑反例证明观察器能区分坏结果，避免代码与验收共同接受错误。 |
| [conftest.py](../../../tests/conftest.py) | 将F10排除于历史自动测试身份及数据库注入，确保使用真实管理CLI/Bearer/独立进程；其他历史用例行为保持原样。 |

## 2. 三条主链

真实使用：隔离空库 → CLI迁移后建号/签发/拨款 → 独立HTTP客户端 → 真实Gateway → 真实Chat/Responses → 客户端结果/事件 → SQL及账目核对。断连关闭的是订阅，后台生成仍继续；取消依现有接口轮询直至许可终态。

容量：每槽完成再派发 → 四种方式轮转 → 记录客户端延迟与原文对照 → 全部收口 → 独立逐请求核验/账户核验 → 新鲜资源快照 → 按硬标准选档 → 连续600秒。正确处理的上游可用性失败仍记作失败，真实成功率不改写成100%。

未知费用：可控主失败且未得Usage → 备用成功并结算 → 保留主预留 → 管理员提交证据及操作号 → 核费/零费/平台承担 → 同一操作重放 → SQL、总览、明细一致。

## 3. 定位入口与取舍

- 内容不符：先看request_id对应的上游原始正文/帧与客户端文本；上游原样短答属质量统计，Gateway丢字阻断。早期未留原文的单字符回答继续标记归因未知。
- 调用失败：HTTP/SSE错误码、持久化error_code及error_stage必须对应；原始HTTP失败额外留status/code，不留响应头。错误归因未知或本地错误不会被当作可接受的供应商失败。
- 金额不符：从SQL的Attempt Usage/缓存分类、账本价格快照独立手算，再看有效预留和账户公式；无Usage保留pending，不按0计费。
- 活跃资源不归零：drained_sample要求快照采集时间晚于收口开始，并检查流任务/订阅/清理/取消及连接池；进程内历史Trace计数和PG历史数据增长另列。
- 原始回执保存在F10目录，实际命令、结果与历次失败见测试方案和报告；独审前冻结安全基线/差分，凭据和.env排除。

## 4. 证据边界

当前限定单实例单worker；真实正常链和容量访问同一个DeepSeek供应商的两种协议，可控畸形协议/超时/存储故障由已有及新增确定性测试覆盖。固定重复输入短任务的吞吐不等于Gateway极限或生产SLA。CPU为ps报告值，RSS/PG数据含观测开销；后台活跃生成计数仅覆盖流任务，普通在途由HTTP收口和SQL终态核查。供应商正式账单/实付核对未验证。

审查补证按原始Usage/cache、独立谷价时段及配置计算每户费用，并核对流式客户端终态Usage。原始上游到SQL逐请求字段快照没有保留，此边界见报告，不能与SQL内部费用自洽检查混称。
