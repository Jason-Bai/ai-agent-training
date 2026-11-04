# F5-O1：统一持久化基础——测试方案

> 状态：验收通过。最终串行全量1228通过/14 live跳过、Python3.12关键312通过；原生Claude Code第2轮APPROVED，无P0–P2。用户已于2026-11-02单独确认本测试方案并授权实施。
>
> 对应[完整设计](design.md)已由用户于 2026-11-02 整体确认。以下为已确认验收要求；实际执行结果单独记录，不将最初计划命令冒充运行证据。

## 1. 范围与通过标准

### 1.1 基线和范围

关联 [F5 原验收](../../test-plan.md)、[F4 原验收](../../../F4/test-plan.md)及 [跨功能原则](../../../../test-plan.md)。原 F5 离线 1099、Python 3.12 关键 235 和真实接入记录是历史证据，不冒充本项结果。原测试意图保留；只有完成判定等本项明确变更的断言按设计更新，并逐项记录原因，不静默删测或降低标准。

覆盖普通文本、Structured Output、现有 POST 直接 SSE 的 PostgreSQL 持久化、入口拒绝、事务/并发、故障恢复、迁移、部署、配置隔离及相关回归。O1 不提供 202 创建协议、GET 补读、断连继续生成、主动取消、重试/fallback、计费或清理；不测试这些接口成功，也不据此提前实现它们。

### 1.2 分层证据

| 层次 | 证明什么 | 不能据此证明什么 |
| --- | --- | --- |
| 单元/故障替身 | 配置、错误分类、事务调用边界和定时分支 | PostgreSQL 实际回滚、并发和持久性 |
| 真实 PostgreSQL 存储测试 | 迁移、字段/键、跨连接可见性、原子提交、回滚、并发和恢复 | HTTP、真实模型是否正常 |
| MockProvider + 真实 PostgreSQL + 本地 HTTP | 响应、实际调用/读取/关闭次数与数据库记录一致 | 云端模型能力及供应商计费 |
| 独立 Gateway 进程 + TCP + 真实 PostgreSQL | 提前增量、断连、进程崩溃和重启、资源释放 | 多 worker、多实例、生产可靠性 |
| 显式真实 Provider + 真实 PostgreSQL | 两种协议的真实文本/结构化/流式调用确实落库 | 通用 Token 计数一致性、供应商停止计费、生产压测 |

真实 PostgreSQL 是事务和恢复测试的必需依赖，不以 SQLite 或内存存储替代。MockProvider 只替代模型服务。用另一条独立数据库连接检查已提交结果，不以生产 Session 的未提交对象或内存 Trace 代替落库证据。

### 1.3 通过门槛与共同断言

全部必测组及列出的参数化变体通过；相关历史回归通过；最终源码/配置与审查对象一致；原生 Claude Code 三维独立审查无 P0–P2 阻断项。P3 记录处置，完整代码审查最多三轮。未执行不计通过，不用覆盖率或通过百分比替代必测规则。

共同断言：

- request_id 由 Gateway 产生、可解析为 UUID；HTTP、SSE、请求表一致。内部 id/request_pk 不对外暴露。
- 普通成功/错误仍为 success/code/data/message；公开 data 不增加 actual_model、attempts、正文 request_id。SSE 仍为 F5 的事件和 data，不因内部 seq 增加公开 id/seq 字段。
- 根据 request_id 查到唯一请求，再以内部主键关联 Attempt/事件；并发不依赖列表顺序。入口拒绝有 failed 请求、0 Attempt、0 Provider 调用。
- 已知 Usage 精确保存，未知为 null；保存本地计数不等于供应商 Usage，不填虚假 0。
- 心跳不入库、不占 seq；业务事件按原内容保存，终态互斥。运行用例后事件仍在，不触发自动清理。
- 管理的 Provider Key、数据库密码、带密码 DSN、异常堆栈不进入响应或落库错误信息；日志不输出配置密码或完整执行快照。合成用户输入正文按已确认快照规则保存，不假设存在自动敏感信息识别。
- 无自动模型重试、隐式 fallback 或测试凭据回写；探测不新增三表记录，404/405 只保留原错误外形，不建立模型请求记录。

## 2. 用例

各行按“准备 → 调用 → 断言”执行。多变体分别参数化，最终报告记录实际收集项；表中组数不是已实现的测试函数数。

### 2.1 配置、迁移与数据模型

| 编号 | 准备、调用与输入 | 预期响应/错误 | Provider/其他副作用 | 数据库/Trace 断言 | 层次 |
| --- | --- | --- | --- | --- | --- |
| C01 | 临时完整 YAML：database + 两条 Mock 模型；从项目目录及其他工作目录分别启动 | 配置正确加载，应用启动 | 上游 0 次 | database 不被 ModelRegistry 二键旧约束拒绝；密码路径始终相对 config | 单元 + PG |
| C02 | 分别缺 database/host/port/name/username/password_file；额外字段、空地址、端口字符串/布尔/0/65536 | 启动配置错误，安全说明 | 上游 0 次、不创建虚假请求 | 不降级内存或自动默认到维护者数据库 | 单元 |
| C03 | 测试专有密码文件缺失、空白、不可读；用带特殊字符的合成密码构造连接 | 非法文件拒绝；合法密码可连接，不因 URL 拼接损坏 | 不读取真实 postgres.password/credentials.yaml | 响应/捕获日志无合成密码和带密码 DSN | 单元 + PG |
| C04 | 空库显式执行迁移，再次执行；查询 Alembic 版本和三表 | 首次建立，第二次无重复建表 | 上游 0 次；应用自身不运行迁移 | 版本正确，原数据不被清空 | PG |
| C05 | 分别使用未迁移库、版本落后/不匹配库、无法连接库启动 | lifespan 启动失败，不接受正常流量 | 上游 0 次 | 不通过 create_all 自动修复，不退回内存 | PG + 进程 |
| C06 | Compose 建立专有测试实例、写入哨兵数据，正常停止/重建同项目容器并保留卷 | 数据库重新健康，哨兵仍在 | 不接触开发数据库/容器/卷 | 实际挂载符合 PostgreSQL 18 布局；本机端口绑定；无匿名卷丢失数据 | Compose + PG |
| C07 | 查询数据库字段及索引；插入两条合法请求和关联子表 | 标识与约束符合设计 | 不调用 Provider | 请求 id 为 BIGINT Identity，request_id 为原生 UUID UNIQUE NOT NULL；子表联合主键及 FK 正确 | PG |
| C08 | 分别插入重复 request_id、重复 Attempt 编号/事件 seq、孤儿子行、seq/attempt_no 为 0 或负值、非法状态、负 Usage/总数不符 | 数据库或存储入口拒绝，相关事务不留下半成品 | 无模型调用 | 请求级唯一性、引用完整性及用量约束可观察；Identity 可跳号，不误要求全局连续 | PG |
| C09 | 同请求追加事件，强制中间事务回滚后再次追加；另一个请求也追加 | 各请求独立从 1 递增 | 心跳不占号 | 回滚事件及计数一起回滚；last_event_seq 与实际最大 seq 一致 | PG |
| C10 | 通过存储入口写入执行快照，事务提交前由独立连接读取，提交后再读 | 提交前不可见，提交后可见 | 无模型调用 | 不能以同 Session 对象证明落库 | PG |
| C11 | 五条测试连接均被有意占用，再发起存储操作；分别在获取连接、SQL 执行和提交处阻塞 | 共享操作预算 5 秒内触发受控存储失败；更短请求剩余预算优先 | 不多建溢出池、不调用额外模型 | 释放后可继续连接，事务/连接无泄漏；时间断言含明确调度容差 | PG + 屏障 |

### 2.2 普通调用、结构化与入口拒绝

固定普通输入：basic_text@1.0.0、user 为“订单 A123 是什么商品？”；默认 Mock 文本“订单 A123 的商品为充电器。”，已知 Usage 为 input=10/output=3/total=13。无 Usage 是独立变体，不用 0 代替。

| 编号 | 准备、调用与输入 | 预期响应/错误 | Provider/其他副作用 | 数据库/Trace 断言 | 层次 |
| --- | --- | --- | --- | --- | --- |
| H01 | 普通合法请求，脚本 Mock 返回固定正文和已知 Usage | 原成功 JSON，正文/Usage 正确 | complete=1，stream=0；模型/消息/预算与装配一致 | 唯一 completed 请求、1 completed Attempt；无事件；完整快照与实际出站消息一致 | HTTP + PG |
| H02 | H01 改为 usage=None | 成功 usage=null | 1 次 | Attempt 三个 Token 字段均 null，未用本地计数补齐 | HTTP + PG |
| H03 | context_answer@1.2.0，精确原始 JSON 中 Schema minimum=1.0000000000000001；Mock 返回 x=1.0000000000000002 | text/parsed/usage 保持原结构及精度 | 1 次，生成约束和 Schema 正确 | input_snapshot 原始 SQL TEXT、规范化解码及实际出站值均无 float 舍入 | HTTP + PG |
| H04 | H03 的候选改为 x=1.0；另测非法 JSON、字段缺失、未知字段 | 原 schema_validation_failed / invalid_json 分类，data=null | 各 1 次，无修复 | failed 请求和 Attempt，已知 Usage 保留，未生成流式事件 | HTTP + PG |
| H05 | 分别非法 JSON、空 messages、空白内容、system 角色、未知模板/版本/模型、缺/多变量、字符超限、Token 超限、非法/超限预算 | 保留原 400/422 与稳定 code | 0 调用、0 Attempt | 每次唯一 failed 元信息；完整快照为空，阶段未取得字段为 null；不保存 raw body | HTTP + PG |
| H06 | 分别脚本上游认证、限流、连接、Provider 超时、截断、拒绝；另抛未预期 Python 异常 | 沿用原错误分类；编程错误仍 internal_error | 各最多 1 次，无重试/fallback | Request/Attempt 失败，已验证上游信息/Usage 不丢失，不被统一伪装成存储错误 | HTTP + PG |
| H07 | 同一成功调用在 Provider 返回前、终态提交前设屏障，独立观察响应和记录 | 完成事务提交前不返回成功 JSON | 1 次 | Request 与 Attempt 同步终态；普通回答不增加结果缓存字段 | HTTP + PG |
| H08 | 探测、healthz、错误路径/方法；同时生成一条普通调用作对照 | 原接口外形和语义 | 探测按原规则 1 次；healthz/404/405 为 0 次 | 只有目标普通调用新增三表记录；探测保留原用途记录，不被伪装成持久化证明 | HTTP + PG |
| H09 | 输入含标签、引号、换行和 SQL 样式字符串；使用静态 Few-shot 模板 | 正常装配，不执行输入中的 SQL 指令 | 1 次 | snapshot 的 System/Few-shot/User 边界、指纹、Schema 和非敏感配置一致；无受控凭据 | HTTP + PG |

### 2.3 流式保存、完成点与资源

固定增量脚本：“订单 ”、“A123”、“ 已签收。”，最后 completed；独立变体包含空格/换行、空 running 事件、仅空白正文、末事件携带 delta。

| 编号 | 准备、调用与输入 | 预期响应/错误 | Provider/其他副作用 | 数据库/Trace 断言 | 层次 |
| --- | --- | --- | --- | --- | --- |
| S01 | 正常增量，Mock 在首段后等待受控释放 | POST 仍直接 SSE，首段在上游结束前到达；最后 completed | stream=1，顺序读取，单流关闭 1 次 | delta/终态顺序及 payload 正确，completed/Usage/Attempt 原子落库；线上不公开内部 seq | TCP + PG |
| S02 | 在首段事务提交前暂停，以独立连接及客户端同时观察 | 提交前客户端看不到该段；提交后才可发送 | 暂停期间不提前读取后续段 | 无未提交事件可见；提交成功与实际发送分开证明 | ASGI 屏障 + PG |
| S03 | 连续空 running 或上游暂不产文本，缩短测试心跳时钟；观察多个空闲周期 | 心跳继续发送且不延长原 B 总期限 | 无忙等、无重复 anext | 无心跳行，不占 seq，不改变正文/TTFT/Usage；保留正常 15 秒常量核对 | 定时 + TCP + PG |
| S04 | 先存 delta 后阻塞其 ASGI send，分别断连/超时；completed 尚未提交 | O1 停止生成，不误记 completed | 不继续读上游；迭代器关闭，共享客户端不关闭 | failed、已有 delta 保留，恢复失败事件至多一条 | ASGI 故障 + PG |
| S05 | completed 事务明确提交后，在完成帧发送处抛 OSError/阻塞至超时；另测普通成功 JSON 发送失败 | 不追加相反失败终态 | 无额外调用，清理有界 | Request/Attempt 保持 completed，已提交完成事件唯一；发送错误单独留诊断 | ASGI 故障 + PG |
| S06 | 上游 completed 同时带末 delta，分别正常发送与末 delta 提交后/完成事务前断连 | 保留先 delta 后终态处理；完成未提交时允许失败 | 1 次，关闭 1 次 | 不强加合批要求；区分上游观察完成与平台提交完成 | ASGI + PG |
| S07 | 上游 failed 带末 delta/已知 Usage，及缺结束信号、非法尾帧、仅空白正文等既有失败脚本 | 原失败码及原文保留，不输出 completed | 1 次，不重复消费/关闭 | failed 唯一，已知上游元信息保留；输出失败和远端失败来源可区分 | HTTP/协议 + PG |
| S08 | 完成提交后才取得发送/清理/总耗时；清理期间再次取消宿主任务 | 终态稳定，清理不会把请求改成另一终态 | 无遗留消费/写入任务 | 只补记已知诊断；指标观察点不改，未知不猜；不以总耗时减 TTFT 冒充模型时间 | ASGI + PG |
| S09 | 多个普通/流式并发，用屏障使后进入的请求先完成 | 各自响应正确 | 每个请求恰好一次对应模式调用 | id/UUID、Attempt、seq、快照及 Usage 不串；Session 独立，不依赖集合顺序 | HTTP + PG |

### 2.4 存储失败、提交未知与恢复

故障必须注入真实事务边界；包装真实 commit/rollback 只用来制造确认丢失，不用假存储成功替代 PostgreSQL。故障组使用专有测试实例并串行运行，不能停止并发回归共享的数据库。

| 编号 | 准备、调用与输入 | 预期响应/错误 | Provider/其他副作用 | 数据库/Trace 断言 | 层次 |
| --- | --- | --- | --- | --- | --- |
| F01 | 合法请求在快照保存、开始尝试提交前发生连接/SQL 错误 | SSE 前/普通调用前 503/storage_unavailable | 0 Provider 调用；回滚的 Attempt 不存在 | 不留半份快照或孤儿子行；无内存回退 | HTTP + PG |
| F02 | 入口本已非法，同时其失败记录保存出错 | 原入口 4xx/code 保留 | 0 调用 | 不声称已保存 failed；安全日志记录落库失败，不含 raw body | HTTP + 故障 |
| F03 | 增量插入/last_event_seq 更新任一步后强制 SQL 错误再回滚 | 该段不对外发送，停止生成 | 调用 1 次，next/close 计数正确 | delta 和计数一起回滚；连接恢复后下一事件不重复、不跳过回滚计数 | PG + ASGI |
| F04 | Request、Attempt、终态事件写入各阶段制造异常，commit 前失败 | 不宣告成功 | 1 次，不重调 | 整体回滚，无 completed 请求配 failed Attempt、无缺失终态；后续条件失败收口 | PG |
| F05 | 真实 commit 已成功后，在调用方收到确认前抛模拟连接丢失 | 普通请求不返回成功；SSE 关闭，未核对前不发 response.failed | 1 次，不重调 | 独立连接确认原 completed/事件存在；核对不覆盖、不复制事件 | PG + HTTP/ASGI |
| F06 | 真实 rollback 后制造与 F05 同类确认丢失 | 停止/关闭，核对前不猜回滚 | 1 次 | 核对后条件 failed，追加唯一失败终态，已有数据不重复 | PG + HTTP/ASGI |
| F07 | 执行中使专用 PG 失联，确保上游被关闭；之后恢复连接 | 不继续正常生成/发送，无无限 HTTP 等待 | 1 次、关闭有界 | 数据库失联期间允许暂未持久化终态；恢复后正确收口而非永久 running | PG 故障 + 进程 |
| F08 | A 已停止执行且保存待核对，B 仍被 Mock 屏障保持活跃；恢复任务运行 | 仅恢复 A，B 继续并可正常完成 | 各 1 次 | B 的 running/事件不被批量改 failed；恢复重复执行不重复终态 | PG + 并发屏障 |
| F09 | 两个不同终态收口操作并发争抢同一运行中请求；重复执行相同收口 | 只有先成功提交者生效 | 不新增 Provider 尝试 | Request/Attempt/终态事件一致，至多一个终态；本项不引入取消接口 | PG 并发 |
| F10 | 进入请求期限末尾时阻塞数据库操作，另测事务提交后才到期限 | 期限耗尽停止生成，不额外延长 SSE 正常发送；已提交终态不覆盖 | 1 次，后台核对不重新生成 | 存储预算取剩余期限更小值；关闭原响应后仍可核对最终数据库状态 | 定时 + PG + ASGI |

### 2.5 进程、历史回归与真实接入

| 编号 | 准备、调用与输入 | 预期响应/错误 | Provider/其他副作用 | 数据库/Trace 断言 | 层次 |
| --- | --- | --- | --- | --- | --- |
| P01 | 独立 Gateway 生成中保存若干 delta 后强制退出，重启同库；普通请求也做一例 | 应用恢复后可接新请求 | 重启不自动调用模型 | 原 running 及 Attempt failed，原因 execution_interrupted；流式旧事件保留并追加一次失败事件 | 真实进程 + PG |
| P02 | 测试夹具构造 pending、canceling、三个终态，再启动应用两次；恢复提交中另注入一次回滚 | pending 保持，canceling failed，既有终态不变 | 0 自动调用 | canceling 失败事件唯一；恢复事务回滚后重试仍一致；不为夹具新增产品取消/创建接口 | PG + 进程 |
| P03 | Attempt 建立后、实际调用 Provider 前强制退出 | 重启失败收口 | 原实际计数可为 0，重启计数仍 0 | 遗留 Attempt 不被当成已获供应商接受/收费证明，不伪造 Usage | 进程 + PG |
| P04 | 执行正常关闭、数据库中断后关闭、测试故障退出 | 进程/后台恢复任务/连接池有界退出 | 共享 Provider 仅应用退出时释放 | 请求结束和 idle 等待不长期占事务；数据库会话/测试子进程不遗留 | PG + 进程 |
| R01 | 跑原 unit/integration/protocol/smoke，按设计变更更新必要的持久化断言 | 旧契约全部保留，明确变更有逐项对照 | 零调用和单调用边界继续核对 | 不再只读旧内存 Trace 证明普通/流式成功；旧报告不改写 | 回归 |
| R02 | Python 3.12 运行受影响关键测试、DeprecationWarning 视为错误；全目录实际收集 | 无模块同名冲突、无异步资源警告 | 不访问外部服务 | 新文件用不同 basename；错误导入不算有效 Red | 回归/收集 |
| R03 | 默认非 live 模式设置真实配置/凭据的禁止读取哨兵，观察网络和文件访问；结束比对保护摘要 | 所有普通测试独立运行，不依赖开发配置 | 仅连接本次自有回环服务/PG，无真实 Provider 调用 | 用户 .env、gateway.yaml、Provider/DB 凭据、开发库/卷及原历史验收工件未改 | 隔离 |
| L01 | 主 Chat、备 Responses 各一次普通文本调用，max_output_tokens=128 | 真实成功且正文非空，外形保持 | 每条恰好 1 次真实调用 | completed 请求/Attempt，真实 Usage 已知，模型/协议正确，实际消息/预算与快照对应 | 显式 live + PG |
| L02 | 两条接入各一次 Structured Output，使用原受控模板和小 Schema，max_output_tokens=128 | 严格验收后成功，parsed 合法 | 每条 1 次真实调用 | Schema/快照/指纹/Usage 持久化，未新增回答缓存 | 显式 live + PG |
| L03 | 两条接入各一次流式，max_output_tokens=128，总期限30秒 | 真 SSE delta + completed | 每条 1 次真实调用，迭代关闭 | 事件/顺序/终态/Usage 对应实际回执；一次样本不代表通用可靠性 | 显式 live + PG |

## 3. 执行计划、证据与结论

### 3.1 环境与隔离

- Python 项目支持的版本及 Python 3.12 关键回归；依赖按实施步骤解析并锁定，SQLAlchemy/psycopg/Alembic 不能因本轮写文档而提前安装。
- 测试夹具以唯一 Compose project 名启动自有 PostgreSQL，使用临时合成密码、独立库、命名卷和随机本机端口；不复用开发者的 15432 端口、数据库或 postgres.password。常规持久化测试必须有真实 PG；缺 Docker/迁移等前置条件时报失败，不静默 skip。
- 会话级环境夹具只在需要 PG 的测试时启动；纯配置/纯逻辑测试不强制拉起数据库。故障破坏性用例使用独立实例，串行运行。
- 用例之间使用独立测试库或可证明隔离的测试数据命名空间。HTTP 已独立提交的数据不能靠测试线程外层 rollback 假装清理；只清理本次拥有的测试数据/库/卷，先校验所有权。产品本身无清理功能。
- 不新增 .env.test。显式传入测试 Settings、Mock 模型注册表、临时 config_dir 和数据库组件，子进程也使用测试专有配置。运行配置当前绑定真实 Provider，不因此改回 Mock。
- 真实 PostgreSQL 观测连接独立于产品 Session，读取后及时结束事务。数据库连接主机/端口须校验为夹具拥有的回环服务；不能只依赖 Python socket monkeypatch 阻止 psycopg 底层连接。
- 默认禁止读取维护者的 credentials.yaml、postgres.password 和 .env，且不访问真实 Provider。授权 live 时仅受控读取现有 Provider Key，数据库仍为测试专用，不读取开发数据库密码。
- 屏障、故障包装、进程控制均置于测试夹具/临时 bootstrap；不增加产品测试路由、请求开关或绕过校验的运行配置。

### 3.2 测试文件与目标命令

拟新增文件使用唯一名称，避免 F5 既有跨目录模块命名问题：

```text
tests/unit/test_f5o1_configuration.py
tests/postgres/test_f5o1_persistence.py
tests/integration/test_f5o1_http.py
tests/smoke/test_f5o1_process.py
tests/live/test_f5o1_acceptance.py
tests/postgres_support.py
```

必要夹具和历史断言随实现建立。以下是确认时规划的目标命令，当前已有对应文件；实际运行及结果以执行记录为准，不用计划命令代替证据：

```bash
uv sync --locked
uv run python -m pytest --collect-only -q
uv run python -m pytest tests/unit/test_f5o1_configuration.py tests/postgres/test_f5o1_persistence.py tests/integration/test_f5o1_http.py tests/smoke/test_f5o1_process.py -q
uv run python -m pytest -q
uv run --python 3.12 python -W error::DeprecationWarning -m pytest tests/unit/test_f5o1_configuration.py tests/postgres/test_f5o1_persistence.py tests/integration/test_f5o1_http.py tests/smoke/test_f5o1_process.py -q
uv run python -m pytest tests/live/test_f5o1_acceptance.py --run-live -x -q -o junit_family=xunit1
```

Python 3.12 同时纳入原受影响的关键回归，最终记录实际命令和收集项，不以本段新文件列表代替全部关键场景。默认全量测试跳过 live；显式 live 缺 Key、PG 或能力则失败，不能静默跳过。

### 3.3 TDD 与故障方法

先记录当前实际基线和受保护摘要，再按配置/迁移 → Request/Attempt → 普通/结构化 → 事件/终态 → 故障/恢复/进程推进。每个行为先构造正确失败，再最小实现并复跑同一场景，最后相关回归。

新模块尚不存在导致的导入错误、新依赖没安装、夹具启动失败都不是有效业务 Red。有效 Red 应是缺少记录、原子性不成立、调用计数错误、终态被覆盖等目标行为失败；必要的环境/测试接入先准备好并单独记证据。

用真实事务前后屏障检验先存后发；对 commit 成功后确认丢失与 rollback 后确认丢失分别包装真实操作；使用显式 SQL 故障、连接关闭及专有测试实例停启验证故障分类。受控时间用于精确分支，TCP/进程测试使用握手和观测点，不能凭 sleep、客户端暂不读取或最终结果收齐来证明真实阻塞/提前到达。所有人为缩短的测试期限与正常配置值核对分开记录。

### 3.4 显式真实调用

本项拟执行 L01–L03，共 **6 次真实模型请求**（两种协议各文本/结构化/流式一次），会产生供应商费用。测试方案获确认后，执行者可在本地 Key 已配置的条件下显式运行，不要求用户再次发送 Key；不顺带重跑旧 8 个 live 样本，除非实际回归问题需要。

用合成订单 A123、低输出预算和已确认模型；保存 request_id、适配器/协议、实际模型、状态、Usage、事件数量和实际命令，不打印 Key/密码或完整快照。真实结果与 Mock/本地 PG 结果分别记录。不能用一次输入计数差值为 0 推断全局计费准确，也不能用云端回执替代数据库事务故障证据。

### 3.5 执行记录与审查

全部命令由实现者运行，原始回执归档到 [evidence.json](evidence.json)，代码阅读入口见 [implementation-summary.md](implementation-summary.md)。原生 Claude Code 的静态审查不是重新运行这些测试。

| 阶段 | 实际结果及边界 |
| --- | --- |
| 切回 Mock 后稳定基线 | 1099 passed / 8 live skipped；切换前真实绑定引发的失败不作为基线 |
| 早期全量 | 1157 passed / 8 skipped，后续又增加用例，不是最终验收数字 |
| 第一版完整收尾回归 | 1186 passed / 14 skipped，Python 3.12；之后补足逐组直接 PG 断言 |
| 首轮 Python 3.12 关键回归 | 308 passed，DeprecationWarning 按错误处理，87.98 秒；包含 O1、真实进程和原 F4/F5 关键测试 |
| 首轮冻结版本全量 | 1224 passed, 14 skipped in 179.80s (0:02:59)，收集 1238 项；实际命令及退出码见原始回执，默认不调用真实 Provider |
| 本项真实接入 | 6 passed，9.94 秒；主 Chat/备 Responses 各文本、结构化、流式一次；数据库均为隔离真实 PG |
| 受保护文件 | 70 份资产、历史验收及本地凭据摘要核对未改；未自动创建开发 postgres.password |

真实六例的 [JUnit 原始回执](live-results.xml)含 request_id、协议、状态、Usage 与事件数；首次运行有 6 条 record_property/xunit2 报告兼容警告，测试均通过。已将后续命令改为 xunit1，不为修报告格式再次消耗真实调用。六例执行后修改了失败 Attempt 错误码、取消后核对及入口落库失败安全日志；真实成功样本不是这几条故障路径的证明，后续故障和完整离线回归覆盖最终源码。未顺带重复旧 8 个 live，未进行供应商计费或生产可靠性验证。

### 3.6 逐组覆盖索引

U＝[配置单元](../../../../../tests/unit/test_f5o1_configuration.py)，D＝[真实 PG](../../../../../tests/postgres/test_f5o1_persistence.py)，H＝[HTTP/ASGI + PG](../../../../../tests/integration/test_f5o1_http.py)，P＝[真实进程/TCP/Compose](../../../../../tests/smoke/test_f5o1_process.py)，L＝[真实 Provider + PG](../../../../../tests/live/test_f5o1_acceptance.py)。下表为实际节点名；同一节点的参数化变体不是另造测试组。

| 组 | 主要实际节点（省略 test_ 前缀） |
| --- | --- |
| C01 | U database_section_does_not_enter_model_registry、password_path_independent_of_cwd_and_url_escaping、invalid_registry_root_explains_optional_database_section；P 独立进程从临时目录启动 |
| C02 | U required_database_fields、strict_database_fields_safe_errors（各缺字段/类型/范围变体） |
| C03 | U password_must_be_present_and_nonblank、unreadable_password_is_safe_configuration_error；D special_password_authenticates_without_url_corruption |
| C04 | D explicit_migration_creates_three_tables、native_identifiers_indexes_constraints_and_repeat_migration（写入后再迁移，数据仍在） |
| C05 | H unmigrated_database_refuses_startup、unreachable_database_refuses_startup；D start_rejects_unmigrated_or_mismatched_version |
| C06 | P compose_down_without_volume_flag_preserves_owned_data（实际产品 Compose 的安全覆盖副本） |
| C07 | D native_identifiers_indexes_constraints_and_repeat_migration |
| C08 | D native_identifiers_indexes_constraints_and_repeat_migration、duplicate_attempt_event_and_invalid_usage_roll_back（含负序号、孤儿两类子行） |
| C09 | D request_concurrency_and_atomic_event_rollback；H failed_delta_transaction_is_never_sent_or_counted |
| C10 | D uncommitted_prepare_is_invisible_to_independent_connection |
| C11 | D short_deadline_pool_and_sql_wait_releases_resources、default_pool_wait_budget_is_five_seconds、actual_commit_timeout_is_uncertain_and_reconciled |
| H01 | H normal_call_is_committed_before_success（出站消息/预算、快照及 Usage 比对） |
| H02 | H normal_call_is_committed_before_success[None]（三个 Usage 字段均 null） |
| H03 | H structured_precision_survives_database（SQL TEXT、返回小数及实际出站 Schema） |
| H04 | H structured_precision_survives_database 的失败变体、structured_failure_keeps_usage_and_failed_attempt（非法 JSON/缺字段/多字段） |
| H05 | H rejections_have_no_attempt_or_executable_snapshot、token_overflow_rejects_before_upstream |
| H06 | H upstream_error_classification_survives_persistence（认证/限流/连接/超时/截断/拒绝/Python 异常） |
| H07 | H ordinary_success_waits_for_terminal_commit；concurrent_ordinary_and_stream_are_isolated_when_finish_order_reverses 的 Provider 返回前屏障 |
| H08 | H probe_liveness_and_unknown_paths_do_not_persist_model_requests（含普通调用对照） |
| H09 | H snapshot_preserves_fewshot_and_sql_looking_input |
| S01 | P tcp_delta_is_committed_while_provider_waits；H stream_business_events_are_persisted_with_private_sequences |
| S02 | H delta_is_not_sent_before_real_transaction_commit |
| S03 | H heartbeats_are_not_persisted_or_allocated_sequence；原 F5 执行器的连续空事件/不重置期限及默认 15 秒测试作为定时分支辅助 |
| S04 | H delta_delivery_boundary_keeps_database_truth 的独立断连/超时变体 |
| S05 | H completed_delivery_error_is_separate_diagnostic、committed_success_survives_final_delivery_failure 的 SSE 阻塞和普通 JSON 失败变体、final_empty_transport_frame_failure_has_persisted_diagnostic 的两种空帧异常 |
| S06 | H delta_delivery_boundary_keeps_database_truth 的末 delta 与 completed 合并事件正常/断连变体 |
| S07 | H failed_stream_payload_usage_and_private_sequence、invalid_stream_ending_is_persisted_as_failure；原协议测试补上游非法尾帧分类 |
| S08 | D post_terminal_diagnostics_preserve_execution_truth；H repeated_host_cancel_during_cleanup_preserves_committed_terminal |
| S09 | H concurrent_ordinary_and_stream_are_isolated_when_finish_order_reverses（不同输入/Usage、反序完成、独立快照/事件） |
| F01 | H storage_failure_before_upstream_has_zero_calls、real_pre_provider_sql_failure_has_no_call_or_partial_attempt（真实 SQL 失败） |
| F02 | H rejection_record_failure_keeps_original_error_and_safe_log |
| F03 | H failed_delta_transaction_is_never_sent_or_counted（插入后与计数更新后两种故障） |
| F04 | D sql_fault_rolls_back_all_terminal_records（三表写入故障） |
| F05 | D commit_confirmation_loss_queries_actual_state；H unknown_terminal_commit_closes_before_reconciliation、ordinary_unknown_commit_returns_error_then_reconciles 的已提交变体 |
| F06 | D commit_rollback_confirmation_loss_reconciles_failed；H 上述两节点的真实 rollback 后确认丢失变体 |
| F07 | P owned_database_outage_closes_upstream_and_reconciles_only_stopped_execution |
| F08 | P 同上（A 停止/B 活跃，恢复后 B 继续）；D reconcile_only_given_stopped_request |
| F09 | D competing_terminal_transactions_keep_one_winner、events_terminal_usage_and_idempotency |
| F10 | H database_wait_obeys_remaining_generation_deadline、committed_success_survives_final_delivery_failure；D 短期限/真实 commit 超时 |
| P01 | P sigkill_recovers_running_without_recalling_provider（普通/流式两变体） |
| P02 | P startup_recovery_rolls_back_then_is_idempotent |
| P03 | P attempt_committed_before_provider_call_survives_kill |
| P04 | P normal_shutdown_closes_pool_and_owned_provider、数据库离线时 shutdown 变体 |
| R01 | 冻结全量回执；原协议、HTTP、执行器及上述 PG 故障分层核对 |
| R02 | Python 3.12 关键回归与最终 collect 回执，无同名模块冲突 |
| R03 | conftest 离线网络/真实 Provider 与 DB 密码读取哨兵；app_factory 配置/环境隔离用例及 70 份保护摘要 |
| L01 | L real_call_and_committed_records[text-*] 两协议 |
| L02 | L real_call_and_committed_records[structured-*] 两协议 |
| L03 | L real_call_and_committed_records[streaming-*] 两协议 |

S03/S07 的细分协议/定时逻辑与 PG 主链分别验证，不声称每一个既有协议坏帧都重复跑了整个 PostgreSQL/TCP/真实 Provider 组合。所有 O1 主链、提交和恢复断言由独立 SQL 连接读取，不用内存 Trace 代替。

### 3.7 TDD、补测与原断言变化

有效 Red→Green 包含：新 database 根配置旧加载器拒绝；空库旧应用仍启动；HTTP 没有持久记录；缺 FK；编程错误误分类；UTC 剩余期限；终态后补记延迟；外部取消分类；发送故障诊断；失败 Attempt 错误码；已保存快照的确认等待被取消后遗留 running；测试读取维护者 YAML/.env；入口错误记录失败缺少安全日志。各原始失败、修正及同场景通过保留，不删除失败回执。

导入/夹具错误、Docker/Uvicorn 测试接入问题不算业务 Red。早期 storage-schema-green 实际仍失败；首次 env-isolation-red 实际通过；补充有效反例后另存 red-valid，不按文件名谎称红灯。逐组补充验收用例大多首次即通过，如实记补测 Green，不补造先前 Red。

原断言变更仅限已确认行为：普通中断请求从遗留 running 改为 failed；SSE completed 提交后的发送超时保持 completed，failed 提交后的发送超时保持原 Provider 错误；其余阶段仍按总期限失败。历史验收报告未重写。历史测试现在显式注入受控模型/环境和隔离 PG，维护者临时切真实 Provider 不影响测试绑定。

### 3.8 独立审查与验收状态

必须调用原生 Claude Code 按[三维提示词模板](../../../../templates/ClaudeCode审查提示词模板.md)审查需求正确性、实现一致性及系统正确性；每轮保存实际 review-prompt-rN.md、原始报告与运行取证，核对真实工具读取和正常退出。最多三轮完整代码审查；前两轮问题执行者修复并原场景复测，第三轮仍阻断才请用户重审。此前独立设计子代理评估不算代码审查。

最终结论逐项区分已通过、失败、未执行及未验证：本地 PG、TCP/进程、真实 Provider、Python 版本和资源隔离分别有证据，不能只报一个总通过数。

### 3.9 模板回看与下一步

本稿使用项目测试模板和 qa-expert 的准备—执行—断言结构；严重等级及门槛以项目 AGENTS 为准，不另引入通用 QA 百分比、CSV 或范围外检查。

现有模板已要求故障、副作用、事务观察点、配置隔离和原始证据，无需仅为 PostgreSQL 新建通用规则。若实施出现通用缺口，再按项目流程回填模板并说明依据。

用户已经确认本测试方案，当前按实施步骤自主推进 TDD、验证与原生 Claude Code 审查；只在新增需求歧义或第三轮完整审查仍阻断时返回对应用户节点。


### 3.10 第1轮原生审查、反例与处置

[原始报告](review-r1.md)：APPROVED，P0/P1/P2=0、P3=4；CLI 退出0，result.is_error=false、stop_reason=end_turn，75轮会话/72次Read/56个文件，实际工具仅Read、MCP为空。原始会话见 [压缩逐条记录](review-session-r1.jsonl.gz)，[取证](review-audit-r1.json)保存读取区间与95份受审文件前后摘要（均未变）。一次完整差分读取超限后改为分段，3999行全部hunk覆盖。报告自述不代替日志：AGENTS.md及密码示例未被单独Read；密码示例在新增diff中，报告把它归为“未变基线”并不准确；tests/smoke/conftest.py虽被报告列为未重读，实际有Read。以日志和差分为准，工作流要求已随提示词给出。第2轮明确补读这两份文件。

| 意见 | 执行者核实与处置 |
| --- | --- |
| F1：模型配置错误文案漏 database（P3） | 已复现并修复文案；合法字段规则不变，增加错误含义回归 |
| F2：末尾空HTTP传输帧错误未记录（P3） | 两个ASGI反例复现：OSError/TimeoutError后PG已completed但诊断null；现记录独立投递错误。该Trace已由执行器排队，Middleware活跃标识消失后保存同一对象的新诊断；不改终态或追加事件 |
| F3：非StorageUnavailable异常会保留pending并重试（P3） | 不采纳直接pop建议：会丢失尚未收口执行的运行核对线索。当前已确认可达的数据库故障均按StorageUnavailable恢复；编程错误保留安全日志及重启恢复依据，不新增范围外熔断/通用重试策略。静态风险保留，不声称已复现生产故障 |
| F4：JUnit xunit2属性警告（P3） | 原始6例回执保留，后续显式命令已设xunit1；不为报告噪声重复付费模型调用 |

**首轮Approved不代替执行者的反例核对**：额外隔离PG对照中，合成密码前后有有效空格时，psycopg直接认证成功，Gateway因strip删除空格而失败。执行者按P2配置正确性问题处理，原生报告并未发现这一项，不改写报告声称其发现。原始可运行反例先在临时测试执行；第一次因未加载项目pytest配置而导入失败不算Red，随后显式-c pyproject.toml的对照有效失败。已迁入项目现有PG认证参数用例，保留末尾LF处理并保留有效密码字符。空白/缺失/不可读/非法UTF-8仍沿原安全配置错误路径；未改维护者实际密码或API Key。

项目中的修复前用例为4 failed/1 passed（密码空格、配置文案、两种末帧诊断），修复后同组加不可读密码守卫为6 passed。对应原始回执在evidence.json。第2轮审查覆盖整个O1最终行为及这些修订，不能只按首轮Approved判断。


### 3.11 第2轮前最终回归

修订后Python3.12关键回归312 passed。第一次完整回归为1227 passed/14 skipped/1 failed：自有PG重启时127.0.0.1:59844被占，Docker无法启动。执行者此前并行启动完整/关键两套含停启故障的测试，不满足本方案故障组串行要求；不能据此称通过，也不能仅凭端口错误归因为产品。现停止套件并行、单独复跑全量；不改Gateway业务实现或降低断言。最终回执与第2轮独审结果在后续记录。


串行完整回归实际结果：**1228 passed / 14 skipped，182.63秒**；Python3.12严格关键回归**312 passed，91.23秒**。默认跳过真实调用；六次真实Provider成功仍沿用前述有成本样本，不把修订后的故障验证冒充真实模型验证。第2轮实际提示词、130行差分与95份源码/测试清单已归档，和首轮仅6个文件不同（89个不变）。第2轮完整独审已完成，最终结论及取证如下。


### 3.12 最终验收与第2轮处置

[原始第2轮报告](review-r2.md)（[最新报告入口](review.md)）：APPROVED，P0/P1/P2=0，P3=1。新增P3仅为3.6索引缺少配置文案/最终空帧两个测试名，已分别补进C01/S05；只改文档，受审源码及测试未再改变，不另开完整审查。首轮F1/F2修复、F3保留核对线索而不直接pop的取舍、F4警告处理均经第二轮核实。

[第2轮取证](review-audit-r2.json)及[原始会话](review-session-r2.jsonl.gz)：CLI退出0、result.is_error=false、stop_reason=end_turn，43轮会话/42次Read/38文件，实际工具仅Read、MCP为空，无读取错误。95份源码/测试/安全配置审查前后摘要相同；比首轮恰有6份改变。原始报告称根AGENTS与docs/AGENTS由system/hook上下文提供，日志没有这两份文件的独立Read；不能把此自述计为独立工具读取。其工作流要求已在实际提示词中明确，本轮设计、实现、测试和证据的关键核对有实际Read记录；范围说明以取证为准，不改写工具原文。密码示例本轮有直接Read。

最终代码证据：串行完整1228 passed/14 skipped、严格Python3.12关键312 passed；本项真实主备六例成功（采集版本及后续修订边界见3.5）。修复前后的密码对照、配置文案与两种末帧诊断有有效Red→Green。资源/保护核对：70份历史、资产、维护者凭据摘要不变；测试专有PG容器/卷和Gateway子进程已退出；未创建开发数据库密码，原已运行服务不由测试重启。

**验收通过**：普通/结构化/直接SSE持久化及恢复闭环完成。仍不提供O2续传/202/GET、F6取消、重试、计费、多实例或生产可靠性证明；下一项按独立新功能路径讨论，不自动开工。
