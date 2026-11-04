# F6：流式请求主动取消——测试方案

> 状态：验收通过。设计与本测试方案分别确认；最终相关严格542通过，累计第2轮独审APPROVED、当前P0–P3均0。
>
> 对应[完整设计](design.md)已于 2026-11-02 获用户确认。本文件随后单独获得用户确认，已按TDD编写测试并实现。基线 [F5-O2 验收](../F5/optimizations/O2/test-plan.md#39-最终验收)不替代本次回归。

## 1. 范围与通过标准

要证明的是完整取消流程：调用方重复 POST 直到终态；Gateway 持续推进 canceling → 停止/清理 → canceled 或 failed；已 completed/failed/canceled 的请求返回原终态。成功取消必须有本地停止、清理完成及终态事务确认的正向证据。

本项共 **33 组必测场景**，分为接口 A5、生命周期 L5、事件续传 E4、并发竞争 C5、故障 F7、恢复与记录 R4、适配/回归 V3。组内参数化分支逐项执行，组数不等于 pytest 节点数，实际数量以收集结果为准。

| 层次 | 本项证明范围 | 不能单独证明 |
| --- | --- | --- |
| 单元/受控 MockProvider | 状态规则、停止信号、计数、有界清理、可控时序 | 真数据库提交或实际 TCP 断连 |
| 真实 PostgreSQL | 状态/Attempt/事件原子性、并发胜者、seq、真实提交/回滚、恢复 | 调用方已收到响应、上游远端停止计算 |
| 进程内 HTTP/ASGI | 路由、校验、统一外形、应用任务与存储集成 | 真 TCP 断连或进程终止；缓冲后的全量文本不证明实时送达 |
| 本地协议服务 | DeepSeek Chat Completions、OpenAICompatible Responses 两条适配路径实际出入站、流关闭与共享客户端隔离 | 真实供应商行为或计费 |
| Gateway TCP/子进程 | 独立客户端轮询、取消连接断开、多订阅、进程退出/重启 | 多实例可靠性、生产容量、供应商停止计费 |

通过条件：33 组及全部参数化必测分支通过；副作用与 Request/Attempt/事件一致；相关回归与必要全量通过；无新增遗留任务/未归还测试连接；全新上下文独立审查 Agent 无 P0–P2 阻断项，P3 有处置。只验证已确认的单实例单 worker，不新增多实例、限流、计费或付费验收。

### 1.1 每个适用场景的共同断言

- HTTP JSON 恰为 success/code/data/message；成功 data 恰为原 request_id/status，X-Request-ID 为原生成 ID；202 只表示 canceling，200 返回三种终态之一。failed 的查询返回 success=true，不伪称原生成成功。无 actual_model、attempts 或内部堆栈。
- 输入拒绝错误保持统一外形；不创建额外模型 Trace、Attempt，不改变目标或其他请求；无 Key/数据库密码/异常原文泄漏。
- 独立数据库连接读取真实 Request/Attempt/事件；不把内存 Trace 当数据库证据。终态与终态事件对应且唯一；canceling 事件最多一次；seq 为 1..last_event_seq，无洞、无重复，事件均关联原 ID。
- 原生成至多一次 Provider 调用；取消、轮询、续传不增加调用或切换备用。pending 取消必须零调用、零 Attempt；无 Attempt 不补造。已启动请求按实际情况为 0 或 1 次，不把任务登记等同于 Provider 已调用。
- 取消接受后无新 delta 提交；允许已提交历史内容继续发送/重放。停止清理后读取计数不再增加；已在途结果不能成为新 delta。共享 Provider 客户端和其他请求仍可用。
- 已知 Usage、时间和错误如实保存，未知为 null；不因取消归零或伪造 Token/收费证据。内存诊断不得把已持久化 canceled 改成 failed/internal_error。
- 资源测试记录 iterator 关闭次数、读取/工作/取消任务及连接池归还。正常资源路径仅一次清理；故障路径保留具体诊断及任务跟踪，不把 failed 冒充资源全部释放。

## 2. 用例

每组按“准备 → 调用 → 断言”执行。竞争使用 Event、提交前/后的屏障或数据库行锁控制先后，不靠随机 sleep 争取覆盖。未指明的共同断言仍按 1.1 执行；实际执行状态见3.4及证据映射，不能以方案描述代替运行结果。

### 2.1 A：接口与轮询（5 组）

| 编号 | 准备与调用 | 预期响应与状态 | 副作用、记录与层次 |
| --- | --- | --- | --- |
| A1 | 参数化非法 UUID；有效 ID 带 body `{}`/`null`/普通文本/非法 JSON；带未知或重复查询参数 | 422/invalid_request，四字段错误 | 状态、事件、调用数不变；HTTP |
| A2 | 分别查询不存在 ID、普通调用 ID、入口拒绝且无有效快照的 ID | 404/not_found | 不创建请求/Attempt，不取消其他请求；HTTP+PG |
| A3 | 种入或真实执行得到 canceling/canceled/completed/failed，重复 POST；核对响应字节及 OpenAPI | canceling=202，其余=200；data/头准确，不暴露 Provider/内部错误；OpenAPI 200/202 是 JSON，4xx/5xx 是 ErrorResponse | 终态不倒退，无重复事件；HTTP+PG+文档 |
| A4 | 暂停清理，首次 POST 后连续轮询，再释放清理 | 屏障内各次202/canceling，响应不等待清理；释放后200/canceled并持续不变 | 原停止/清理任务只启动一次；不用 SSE 也完成取消；HTTP+Mock+PG |
| A5 | 模拟应用停止或路由未预期异常；与另一个有效请求、healthz 一同访问 | 新取消无法接受时503/execution_interrupted；未预期错误500/internal_error；已有终态不被改写 | 错误脱敏，健康检查和不受影响请求行为一致；HTTP |

### 2.2 L：执行生命周期（5 组）

| 编号 | 准备与调用 | 预期响应与状态 | 副作用、记录与层次 |
| --- | --- | --- | --- |
| L1 | POST 创建 pending，不 GET；在取消接受后、最终保存前设置屏障；POST 取消并轮询 | 实际观察 PG pending→canceling→canceled | 0 Provider/0 Attempt，事件严格 canceling/canceled；后续 GET 只回放不启动；HTTP+PG |
| L2 | GET 启动，Provider 等待首段；POST 取消 | canceling→canceled；SSE 无 delta，最终 canceled | 停止读取、关闭本次流，调用1/Attempt1；Mock+HTTP+PG |
| L3 | Provider 先提交一段“订单 A123”，下一段“不得提交”被屏障阻塞；取消后释放 | 旧段保留，后段不能提交，终态canceled | 关闭一次、读取停止；重放与原已提交内容一致；Mock+HTTP+PG |
| L4 | 参数化任务已登记未进入协程、执行器未建立、Attempt 建立前、Attempt 建立后尚未发起模型调用；各点取消 | canceling→canceled，无 internal_error/execution_interrupted 误分类 | 观察实际调用/Attempt数，不重建任务；未开始调用的路径保持0；单元+HTTP+PG |
| L5 | 无 SSE 订阅者，或所有订阅者已断开；分别在取消接受前/提交中/提交后断开取消 HTTP 调用方 | 未接受且确定回滚的请求不伪报取消；实际已接受的取消独立完成，重连POST取得终态 | 取消任务归应用，不归 HTTP 连接；无请求依赖轮询才能清理；TCP+PG |

### 2.3 E：事件、历史与续传（4 组）

| 编号 | 准备与调用 | 预期响应与状态 | 副作用、记录与层次 |
| --- | --- | --- | --- |
| E1 | 已提交205条delta后接受取消；独立连接尝试追加delta、普通completed/failed及再次接受取消 | canceling 后新delta和普通终态不得写入；取消流程仍能提交唯一终态 | 历史205条不变，canceling/canceled为206/207；同事务状态/事件原子可见；PG |
| E2 | E1数据以 after_seq 省略、0、99、205、206、207读取；页大小跨100；再测负数/空/非数字/重复/未知/超过最大游标 | 按原seq回放；终态且读完204；错误沿用422/400，不触发模型 | 不跳过已提交历史；事件字节/ID一致，无重启模型；HTTP+PG |
| E3 | 同请求两订阅不同游标，其中一个发送阻塞/断开；取消并继续另一个；随后恢复旧订阅 | 两者各自得到完整有序历史及相同唯一终态；旧内容可以晚于取消响应到达 | 订阅失败不覆盖取消结果，不重复事件/调用；受控发送+TCP+PG |
| E4 | 清理/保存等待期间订阅空闲；受控时钟跨15秒心跳，随后终态；反复轮询POST | 心跳无id、不落库、不推进seq；终态后订阅结束；轮询不重置原生成总期限或清理预算 | 记录时钟与事件顺序，持续内部活动不延长预算；单元+HTTP |

### 2.4 C：并发与竞争（5 组）

| 编号 | 准备与调用 | 预期响应与状态 | 副作用、记录与层次 |
| --- | --- | --- | --- |
| C1 | 同一 running 请求并发20个POST，清理用屏障暂停；再轮询至终态 | 返回仅canceling/canceled，最终一致 | 一个接受事务、一条canceling、一次停止清理、唯一终态；HTTP+PG |
| C2 | 20个首次GET与取消交错，明确执行“取消提交先”及“GET领取/注册先”两个顺序 | 取消先则0调用；GET先则只停止原任务；两种均最终canceled | 无重复领取/Attempt，GET不把活跃取消当遗留任务收口；HTTP+PG |
| C3 | 模型已返回completed；分别阻塞其提交让取消先赢、先提交completed再取消 | 取消先→canceled；完成先→completed | 模型结束不等于数据库已完成；只一个终态，不重复追加完成/取消事件；HTTP+PG |
| C4 | 参数化Provider失败及生成总超时；分别让正常failed提交先、取消接受先 | 失败先→原failed；取消先→由取消清理结果收口，不能普通失败覆盖 | 确认本地取消原因先设置，不被异常传播误分类；HTTP+PG |
| C5 | A123/B456交错执行、使用同一共享Provider客户端；取消A，同时订阅/轮询B | A按取消收口，B正常completed | 调用、Usage、事件按request_id关联；共享客户端未被关闭，B的期限不变；Mock+本地协议+PG |

### 2.5 F：停止、存储及所有权故障（7 组）

| 编号 | 准备与调用 | 预期响应与状态 | 副作用、记录与层次 |
| --- | --- | --- | --- |
| F1 | iterator.aclose 抛受控私有异常；重复POST直到终态 | failed，PG/SSE code=cancel_cleanup_failed | cleanup_error_code保留具体分类；响应/日志不泄漏异常正文；不误写canceled；单元+HTTP+PG |
| F2 | 参数化读取/工作任务不能及时结束、aclose超时；清理期间再次中断 | 原1秒清理预算不被重复取消重置；最终failed/cancel_cleanup_failed；取消HTTP可持续返回进度 | 有界控制等待，失败任务仍跟踪、无新delta；测试最终释放故障替身并确认无测试遗留；单元+HTTP |
| F3 | 在接受取消事务退出注入确认丢失：分别真实commit后报错、真实rollback后报错；同时并发POST/GET | 故障调用503；独立PG证明提交结果；已接受则独立取消；确定回滚且无后续取消时不得擅自判canceled | 状态/事件同事务；不以异常直接认定失败/成功，无第二取消所有者/模型调用；PG+HTTP |
| F4 | 在 canceled 或 cancel_cleanup_failed 终态保存时参数化真实commit/rollback后确认丢失 | 查询恢复以实际PG结果为准；未确认时不虚报canceled；最终保存准确且唯一 | 两种终态都不重复事件、不丢Usage；保留待保存结果，无模型重发；PG+HTTP |
| F5 | 分别在取消接受前、已接受后停止专属测试PG；等待超时，再启动；同时轮询 | 不可用时503；确认接受后清理不依赖PG连接；恢复后收口，服务健康不等于生成成功 | 不回退内存成功；不持连接等待清理；只操作自有容器，串行；TCP+PG |
| F6 | 执行已停止且持有已知Usage/取消结果，终态保存暂失败；交错新GET、POST与遗留恢复循环 | 不能抢写execution_interrupted或丢失原结果；恢复后准确终态 | 复测F5-O2的延迟结果所有权与领取/恢复锁回归；HTTP+PG |
| F7 | 取消接受后的任务注册/调度抛异常；或存储读取/诊断更新出现一次故障 | 未预期接口错误统一500，存储错误503；已接受操作保留可接管责任，不能永久无人推进或虚报成功 | 检查后台任务异常已取走、无协程未await警告；终态/诊断写入不互相覆盖；单元+HTTP+PG |

### 2.6 R：恢复、迁移与记录（4 组）

| 编号 | 准备与调用 | 预期响应与状态 | 副作用、记录与层次 |
| --- | --- | --- | --- |
| R1 | 原进程已退出，PG保留pending/running/canceling及三种终态；新进程启动并再次重启 | running/canceling→failed/execution_interrupted，pending及已有终态不变 | 一次恢复终态事件、原seq连续；取消未证明完成不能补canceled；不重调模型；PG+进程 |
| R2 | 参数化正常关停、清理中SIGKILL、清理完成但终态未确认时SIGKILL；再启动 | 已明确提交终态保持；非终态按启动恢复failed；退出有界 | 应用取消/生成/订阅所有者协调，不关闭其他请求客户端后冒称取消成功；TCP+进程+PG |
| R3 | 对含原有普通/流式记录的旧版本测试库执行Alembic upgrade head；再以旧版本库启动；检查约束 | 新版本允许Attempt canceled及取消事件；旧库启动明确失败，不自动迁移 | 原数据、外部request_id、主外键保持；不新增业务表，不绕过CHECK；PG+迁移+进程 |
| R4 | 参数化已知Usage(3/2/5)、未知Usage、取消前上游已结束但完成未提交、pending无Attempt | Request/Attempt终态一致，已知Usage保留、未知null，pending不补Attempt；时间按实际观察点记录 | 取消不推断费用；内存/PG状态无canceled→internal_error回退；HTTP+PG |

### 2.7 V：真实边界与回归（3 组）

| 编号 | 准备与调用 | 预期响应与状态 | 副作用、记录与层次 |
| --- | --- | --- | --- |
| V1 | 实际Uvicorn单worker、独立TCP客户端；POST创建，分别无GET和GET启动后，仅POST取消并间隔轮询；另断开全部订阅 | 两路径无需SSE确认即可终态；中途能实际观察202/canceling，最终200 | 独立SQL与测试Provider计数证明停止、清理及唯一终态；不以ASGI缓冲结果冒充网络证据 |
| V2 | 两条真实适配器连接本地HTTP协议服务：DeepSeek Chat Completions、OpenAICompatible Responses；暂停上游输出后取消A，再验证共享客户端服务B；另检查不支持的协议配置被拒绝 | Gateway只依据统一协议；A本地流关闭、B继续完成 | 核对实际请求路径/出站参数、上游连接与客户端生命周期；不连接供应商，不推断远端计算停止；本地协议+HTTP+PG |
| V3 | 执行F1–F5-O2全量非live回归；核对文档、配置/源码快照及测试实际收集 | 普通/Schema/精确小数、模板、预算、探测、两阶段启动、seq续传、TTFT/Generation等原行为保持 | 默认Mock和真实凭据不变，不读Key、不额外联网；AGENTS原样；历史验收工件不改；全量结果及默认live跳过单列 |

## 3. 执行计划、证据与结论

### 3.1 环境与替身边界

- 使用项目 `uv` 环境的 Python 3.12、既有 pytest/pytest-asyncio、Docker Compose PostgreSQL；准确版本与运行命令在实际执行回执记录。无需新增运行时服务。
- 复用 `tests/postgres_support.py` 创建自有Compose project、随机本机端口、测试密码与迁移库；每用例独立数据库或模板克隆。独立SQL连接观察提交后状态。只清理本次自有资源，不使用用户postgres.password，不停止用户数据库或8000服务。
- MockProvider或测试内的受控实现遵守统一Provider协议：文本增量“订单 A123”“不得提交”，完成事件、失败事件、已知Usage(3/2/5)或未知Usage；Event屏障控制读取/完成/aclose。计数器分别记录调用、anext、aclose、共享客户端关闭。替身不得直接返回固定“取消成功”或替换被测取消服务/存储状态机。
- 本地协议服务返回真实Chat SSE或Responses SSE。通过实际适配器和SDK消费，不把归一化ProviderStreamEvent直接注入来冒充协议集成证明。
- 事务故障沿用真实session commit/rollback再丢确认方式，按阶段和request_id标记，留原始SQL独立观察；不只抛StorageUnavailable然后自称证明了提交不确定性。
- 受控时钟用于心跳/预算逻辑，实际cleanup超时和进程退出另保留真实时钟测试。竞争用屏障/锁证明指定顺序；所有等待有测试上限，超时直接失败，不无限轮询。
- 默认保留离线网络/凭据隔离。F6自有PG测试实施时需纳入既有conftest的独立配置分支，避免历史autouse覆盖；只做必要测试接线，不改用户配置。付费真实Provider本项不执行；历史live结果不算F6验证。

### 3.2 文件与执行入口

以下测试文件已建立；实际测试集合与组映射在验收时列明，不能减少必测场景。

| 拟建文件 | 主要覆盖 |
| --- | --- |
| `tests/unit/test_f6_execution.py` | L4、E4、F1/F2/F7的信号/清理及有界等待 |
| `tests/postgres/test_f6_storage.py` | E1、F3/F4、R1/R3的事务与约束 |
| `tests/integration/test_f6_http.py` | A、L1/L2/L3及R4 |
| `tests/integration/test_f6_races.py` | C、F6及既有恢复竞争 |
| `tests/integration/test_f6_replay.py` | E2/E3及取消历史 |
| `tests/integration/test_f6_faults.py` | F组的应用/真实PG集成 |
| `tests/integration/test_f6_protocol.py` | V2两条本地协议接入及非法配置拒绝 |
| `tests/smoke/test_f6_process.py` | L5、F5、R2、V1及实际网络/重启 |

命令工作目录为项目根 `projects/llm-gateway`。下列为已确认的执行入口；具体运行范围、退出码和结果以3.4的原始回执为准：

```bash
uv run --python 3.12 pytest --collect-only -q -k f6
uv run --python 3.12 pytest tests/unit/test_f6_execution.py -q
uv run --python 3.12 pytest tests/postgres/test_f6_storage.py -q
uv run --python 3.12 pytest tests/integration -k f6 -q
uv run --python 3.12 pytest tests/smoke/test_f6_process.py -q
uv run --python 3.12 python -W error -m pytest -k f6 -q
uv run --python 3.12 pytest -q
```

执行安排：确认后按契约/存储、生命周期、入口/轮询、竞争/故障、协议/进程逐步Red→Green；保存正确失败原因，再用同场景验证。实施步骤在测试确认后制定；共同入口、迁移、状态写入与恢复接线统一集成。本项不强行并行有共享所有权的代码。

停启PG、进程退出及宿主资源故障串行执行，不与其他故障套件重叠。稳定集成后执行必要全量；重复执行由相关代码变化、失败或未解决问题触发，不机械反复全量。正常Red不是验收失败，最终断言不得删除或放宽以换取通过。

### 3.3 原始证据与独立审查

- 每次关键Red/Green、PG故障、进程启动/退出保留命令、时间、退出码、stdout/stderr及测试节点；记录实际故障注入点、独立SQL快照与模型计数。不归档密钥/密码或私有推理。
- 建立33组到实际pytest节点的映射；组内参数化分支逐项标注。全量数、F6新增数、严格检查子集及live默认跳过数分开，不能相加冒充互不重叠。
- 实现稳定后产出同目录代码实现导读，归档安全基线/真实差分和只读独审实际提示词；使用[独立审查模板](../../templates/独立审查提示词模板.md)，按根工作流执行全新上下文Agent三维审查，最多三轮。Claude Code为可选，当前模板中的旧称不覆盖已批准工作流。
- 独审重点复核取消接受后的写入屏障、HTTP断连所有权、清理失败、提交结果未知、恢复不抢写、多请求隔离；需要反例实验由执行者运行并留原始回执。

### 3.4 当前状态与结论

| 项目 | 状态 |
| --- | --- |
| 完整设计 | 用户已确认 |
| 本测试方案 | 用户已于2026-11-02单独确认 |
| 新增测试/实现/迁移 | 已实现；91个新增F6节点，当前全库收集1437节点 |
| Red/Green/故障/相关回归/全量 | 全量1418/16跳过；R1前相关142；R1修复后F5/F6严格542通过/10跳过，版本边界见3.6/3.7 |
| 付费真实Provider | 本项不执行，不声称已验证 |
| 独立代码审查 | 第1轮BLOCKED(1 P2/2 P3)，第2轮APPROVED，当前P0–P3均0 |
| 验收结论 | 验收通过，见3.8 |

模板回看：沿用现有测试模板字段与根工作流，不新增覆盖率指标、QA目录、CSV或额外确认节点；本项最终覆盖效果待全量及独审核实。设计及测试方案已分别确认；按上述范围自主实施、验证、更新必要文档并完成独审，AGENTS.md继续保持不改。

### 3.5 已确认的协议测试口径修正

实现前核对发现，V2原写“三条”与既有配置不符。用户已明确“DeepSeek是chat completions方式，OpenAICompatible是Responses方式，两个都需要支持”，并再次指出取消本身不应知道上游协议。因此V2验证这两条既有接入及不支持协议的配置拒绝，不扩展OpenAICompatible Chat。取消API和管理器不按协议分支；适配器测试只证明各自能关闭本次流且共享客户端仍可用，不证明供应商停止计算/计费。该修正不减少两条实际接入的取消要求。

### 3.6 实际执行与修正证据

原始stdout/stderr、命令、真实差分、源码摘要及33组节点映射见[evidence.json](evidence.json)。pytest组数、参数化节点、全量及相关子集不能相加冒充互不重叠；按原始回执核对。

| 阶段/回执 | 实际结果 | 来源及含义 |
| --- | --- | --- |
| storage-red.log | 16个预期失败，缺accept_cancel/finish_cancel | 存储Agent；早期wrapper未保留pytest退出码，以原始失败摘要及工具回执为界 |
| http-red.log | pending取消404，预期失败 | HTTP测试Agent；证明缺路由，不把收集错误当Red |
| execution-red.log | 3个预期失败 | root；取消误分类、取消后仍启动、抵抗取消时清理无界 |
| storage-regression.log / storage-strict.log | 26通过（含17个F6+9个旧PG）；17严格通过 | 存储Agent，真实PostgreSQL |
| http-strict.log | 首版47严格通过 | HTTP测试Agent，后加6节点在后续统一严格中验证 |
| boundary-strict.log / boundary-pg-fault-green.log | 非停库10严格通过；停库2通过 | 边界测试Agent；真实TCP/进程及本地SDK，不是供应商 |
| full-initial-interrupted.log | 530通过/1失败/16跳过后人工中断 | root；已知fixture识别错误后停止无效继续；不算通过 |
| fixture-isolation-green.log | 原参数组10通过 | root；改按测试文件名识别F6，离线守卫曾阻止误读密码，实际未读出 |
| full-port-conflict.log | 1410通过/1失败/16跳过 | root；测试PG重启端口55184被占用，未认定产品失败或通过 |
| port-selection-red/green.log | 新端口约束先失败再通过 | root；仅测试夹具选择固定空闲20000–29999端口，本机临时端口区间为49152–65535；未证明当时具体占用进程 |
| f6-and-outage-strict.log | 90通过，7 deselected，91.86秒，exit0 | root；88个当时F6节点及2个旧停库分支，修正后全部通过 |
| full-regression.log | 1418通过/16 live跳过，350.84秒，exit0 | root；1434节点时的完整回归，早于下述迟到Trace修正 |
| late-terminal-diagnostic.log / late-terminal-red.log | 最小实验及正式回归均失败 | root；确认终态后迟到清理会把内存Trace/Attempt改回canceling；无数据库终态倒退 |
| late-terminal-green.log | 新回归1通过，exit0 | root；保留已确认终态，取消所有者持久化结果仍唯一 |
| final-related-strict.log | 142通过，7 deselected，100.58秒，exit0 | root；最终89个F6+49个旧执行器+2个旧管理器+2个旧停库节点；Python3.12 warnings-as-errors |
| final-collection.log | 1435节点，收集成功 | root；当前1346个历史节点+89个新增，默认16个live跳过，不声称新增1435个测试 |

全量与最终版本边界：全量后仅`src/services/streaming.py`的取消分支增加已确认终态保护，以及对应单元回归；原全量文件摘要、最终摘要和两文件真实差分单列于evidence。最终142严格覆盖受影响的F5执行路径及全部F6/停库场景，不把全量1418称为修正后的同一版本全量。其他普通/Schema/模板/预算等源码保持全量验证版本。没有付费真实Provider调用。

33组映射采用直接测试与明确标注的组件/历史补充；尚未把所有独立场景做笛卡尔积组合，例如ACK丢失与已知Usage、多次POST与受控心跳放在同一实验。映射保留这些边界，不能把有关联节点当作所有组合都已单独运行；独审须核实是否存在关键证据缺口。

63份AGENTS、旧Feature验收Markdown及默认配置摘要保持不变；未提供旧JSON全工件的实施前摘要，不夸大为所有历史工件均经过哈希比对。最终验证源码/测试/迁移/安全配置113份有固定摘要。普通历史迁移断言仅由旧head常量改为当前REVISION，保留约束与数据断言；另补旧普通/流式数据显式升级保护。

### 3.7 第1轮独审处置与第2轮证据

[首轮原始报告](review-r1.md)为BLOCKED：P0=0/P1=0/P2=1/P3=2。root按审查者提供时序做实际PG实验：Attempt已提交但返回暂停；取消实际提交canceling但ACK暂停；原生成恢复后首次Provider调用由0变1。实验退出1，原始输出为review-r1-start-window.log；最终canceled并不能掩盖这次额外调用。

最小修复：首次Provider启动与接受取消复用同一请求锁；锁内读取实际DB状态，并让首次anext任务进入一轮调度后释放，不等待首段内容。仅在锁外增加查询不够，内存标志本身也不能代表事务已接受。真回滚路径仍允许原执行继续。

| 回执 | 实际结果 | 证明范围 |
| --- | --- | --- |
| admission-red.log | 1失败/1通过，exit1 | 真提交延迟ACK原窗口正确失败；真回滚原执行可继续 |
| admission-green.log | 60严格通过，13.34秒，exit0 | 两个新窗口分支、F6清理unit及旧执行/管理器 |
| review-r2-related.log | 542通过/10 live跳过/885 deselected，283.43秒，exit0 | 最终源码下受影响的F5/F5-O1/F5-O2/F6全链严格检查，含全部91个F6节点；deselected不是失败或已执行 |
| final-collection.log | 1437节点，收集成功 | 当前1346历史+91新增。默认16个live总量与本次选中10个live区分 |

R1→R2产品/测试仅两份源码及新增test_f6_admission.py，实际差分及原基线摘要在evidence；源码/测试/迁移/安全配置现在114份。全部普通/Schema/Prompt等未变源码复用原全量证据；不将1418或142写成当前R2版本的单次全量。两个P3分别补最终collection来源与README当前事件/取消说明，原R1证据保留在evidence-r1.json，不覆盖审查历史。AGENTS未改、无供应商调用。

[第2轮实际输入](review-prompt-r2.md)与[材料](review-brief-r2.md)已归档；同一Agent已完成三维复核，结论见3.8。

### 3.8 最终验收

2026-11-02，依据已分别确认的设计/33组测试方案、原始执行回执及[第2轮原始报告](review-r2.md)，F6验收通过。累计两轮；首轮P2及两P3均核实关闭，第2轮APPROVED、当前P0=0/P1=0/P2=0/P3=0，没有第3轮。审查输入/调用与原始报告见[审查索引](review.md)。

- 当前1437节点，其中91个F6新增节点；33组均有对应测试，91个节点均已关联、无缺失或未知节点；直接/组件/历史补充边界保留。
- 最新源码受影响F5/F6全链严格542 passed、10 live skipped、885 deselected，exit0；包含全部F6及原流式/持久化/续传链路。两实际适配器通过本地HTTP/SDK验证，无供应商调用。
- 原完整1418/16跳过、首轮相关142及最终542分别属于对应版本，不相加、不冒充当前版本单次全量；原/新摘要、真实差分与原始日志在evidence归档。
- 第2轮审查前后116份源码/测试/迁移/安全配置及AGENTS摘要完全相同，见[审查审计](review-audit.json)。63份历史验收Markdown/AGENTS/默认配置保护摘要保持；默认两Mock不变，未读/未写真实Key或用户数据库密码。
- 测试容器/子进程清理由自有资源夹具完成；不操作用户服务或迁移用户数据库。本地使用前显式执行项目`uv run alembic upgrade head`至f6_001。
- 不证明多实例、生产容量、供应商停止计算或费用归零；取消只承诺本地停止及有界清理。AGENTS修改仍需用户另行明确确认，本次未改。

验收后若发现回归按项目Bug路线处理，不改写本次原始执行或审查报告。F7保持未开始，不预先实现重试/fallback。
