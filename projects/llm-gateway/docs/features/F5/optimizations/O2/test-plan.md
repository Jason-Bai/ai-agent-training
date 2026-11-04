# F5-O2：两阶段调用与断点续传——测试方案

> 状态：验收通过。最新完整回归1330 passed / 16 live skipped，累计第2轮独立Agent审查APPROVED、P0–P3均0；版本与证据见3.9。
>
> [设计](design.md)已于2026-11-02经本次对话确认。本文件已由用户单独确认，并授权两条真实调用；实施按本方案验收。原F5-O1的1228/312及六例真实回执是历史证据，不冒充本项结果。

## 1. 范围与通过标准

覆盖POST创建、首次GET原子启动、PG事件分页、SSE游标、断连继续、多订阅、时限、任务/事务故障、重启、相关历史回归及两条显式真实流式接入。F6取消、F7重试、F8查询、F9计费、LISTEN/NOTIFY与多实例不在范围内。

| 层次 | 证明范围 | 不能代替 |
| --- | --- | --- |
| 纯逻辑/受控时钟 | 参数、状态条件、轮询与计时边界、快照匹配 | 真实事务与网络断连 |
| MockProvider + 真实PG | 实际提交/回滚、认领竞争、分页、终态、Usage与调用数 | 云端能力/收费 |
| ASGI真实send故障 + PG | 实际发送等待、独立诊断与状态不被覆盖 | TCP客户端确实收到内容 |
| 独立Gateway进程 + TCP + PG | 首段提前到达、断连后继续、重连、多订阅、SIGKILL/停机与恢复 | 多实例及生产可靠性 |
| 两种真实Provider + 隔离PG | 主Chat/备Responses各一条真实流式链路 | 任意故障的供应商行为、通用Token/计费准确性 |

全部32组及所列变体通过，相关回归通过，最终源码/测试与受审版本一致；独立Agent三维完整审查无P0–P2且无关键证据缺口。完整独审最多三轮；P3记录核实与处置。不按测试数量或覆盖率百分比替代必测规则。

共同断言：

- 所有判断按Gateway产生的request_id关联，再用PG内部主键关联Attempt/事件；成功POST、成功订阅的响应头及帧request_id一致，内部PK不公开。
- POST成功严格202、四字段JSON，data仅request_id/status；GET有效数据为SSE，204无响应体，其他错误保持四字段JSON。普通/结构化/探测的公开字段不增加actual_model/attempts。
- 仅创建：pending、有快照、seq=0，无Attempt、无事件、0 Provider调用。有效生成本项至多1次Provider尝试，无SDK/Gateway隐式重试或fallback。
- 业务帧id等于数据库seq并严格按序；载荷与已提交PG事件一致；心跳无id、无存储、不占seq。各订阅独立，不用集合位置猜关联。
- 每个生成最多一个终态；每个订阅结束/超时不等于生成终态。已知Usage原值保存，未知null，不按订阅数重复记账、不用本地计数补云端Usage。
- 原生成指标只由原执行及首次实际发送观测补记；历史重放不覆盖。服务端send成功不冒充调用方已处理；ACK丢失允许重投。
- 用独立SQL连接观察已提交事实，不能只查进程Trace或生产Session；不以SQLite、FakeStore代替PG原子性证明。
- 不读取维护者真实YAML/.env/DB密码/Provider Key、不访问真实模型（显式live除外）；日志/响应/记录无管理凭据、DSN密码、原始异常堆栈。输入快照范围沿O1，不额外引入自动脱敏承诺。

## 2. 用例

统一合成请求：basic_text@1.0.0、general-primary、单条user“解释订单A123的处理状态”。默认脚本依次输出“订单 ”、“A123”、“ 已签收。”及completed；已知Usage为10/3/13，unknown为独立变体。每项按“准备→调用→实际出站/发送→独立SQL观察”检查。

### 2.1 创建与入口

| 编号 | 准备/输入/操作 | 响应及行为 | Provider/PG/诊断断言 |
| --- | --- | --- | --- |
| A01 | 合法POST；不GET，等待多个轮询/恢复周期 | 202与完整创建结构，无SSE；status=pending | 0 complete/stream/Attempt；快照=实际预检装配，seq=0，started_at/deadline_at=null；等待不自动生成/清理 |
| A02 | 分别非法/重复键JSON、空/多消息、非user角色、空白/字符超限、模板/变量/模型错误、能力false、Token超限、错误/超限预算、stream/response_schema额外字段 | 保留原400/422分类；data=null | 0调用/Attempt，仅failed元信息、无快照/业务事件；纯输入错误不因记录失败进入模型 |
| A03 | GET省略after_seq、显式0/前导0；另测空/负数/小数/true/非数字、重复/未知查询字段、非法UUID；after_seq与Last-Event-ID不同 | 合法值正确读取，非法422；省略为0，显式query为唯一游标依据 | 非法GET不启动生成、不改请求/事件，不新增模型请求记录；无隐式游标回退 |
| A04 | 不存在UUID、普通调用UUID、无有效快照的流式拒绝UUID | 404/not_found | 原记录保持；0新模型调用，无额外请求/Attempt |
| A05 | pending时after_seq=1；运行/终态时超最大；终态已读完及终态尚有未读两类（completed/failed） | 未来游标400/invalid_event_cursor；已读完204空体；未读返回SSE | 无错误启动/重置；204不宣称成功；回放不重调模型 |

### 2.2 启动、快照与状态归属

| 编号 | 准备/输入/操作 | 响应及行为 | Provider/PG/诊断断言 |
| --- | --- | --- | --- |
| L01 | POST后首次有效GET；Mock在首段后屏障等待 | GET启动且首段先于模型完成到达 | 1 stream/0 complete；pending→running，恰好1 Attempt；started_at/deadline_at从首次启动计，不从POST计 |
| L02 | 同一pending同时20个GET；在认领提交/任务注册处交错；再加入后续GET | 每个有效订阅可读取同一生成 | 实际stream=1、Attempt=1、无重复任务；同一deadline；按ID核对全部订阅 |
| L03 | 创建后改变/移除Prompt注册；另测模型/Provider/计数资源配置不匹配；再测服务重启后保留pending且配置兼容 | 消息仍用保存快照；配置不兼容明确失败 | 不重渲染或换模型；可兼容时出站消息/预算=POST快照；不兼容execution_configuration_changed、0调用、唯一failed事件 |
| L04 | 阻塞POST 202发送、GET200头、订阅结束、一个订阅断开，穿插观察状态 | HTTP202/200/函数结束均不充当模型完成信号 | 创建仍pending，执行仍running；只有生成所有者写终态；GET不新增Attempt/模型请求 |

### 2.3 事件与续传

| 编号 | 准备/输入/操作 | 响应及行为 | Provider/PG/诊断断言 |
| --- | --- | --- | --- |
| E01 | 真PG插入提交前设置屏障，同时TCP订阅；提交后释放Provider后续事件 | 提交前不可见、不可发送；首段确在上游结束前到达 | seq/载荷/状态跨独立连接一致；不得缓存完整结果才发 |
| E02 | 中文/emoji/换行/空格及SSE样式文本；两种Usage；同时观察心跳 | id和request_id正确，文本逐字一致，无data.seq重复字段 | 连续seq、唯一终态、Usage已知/null准确；心跳不占行/号 |
| E03 | 自有PG预置205条增量+终态；从0/页边界/非零中间seq读取 | 跨3页顺序完整，每次页上限100；不足100条即时发送 | 无OFFSET丢失/跳号，无重复/漏帧，无整回答内存聚合；纯回放0模型调用 |
| E04 | 首段后真实客户端断开；Mock屏障确保断开期间继续产出；SQL观察，再携last_processed_seq重连 | 断开后事件继续提交，续传完整结束 | 断连时上游未close，实际调用仍1；终态后上游仅关闭一次；不重置期限、不重算历史指标 |
| E05 | 同请求快/慢两订阅，另请求并发；一个订阅阻塞或断开 | 快订阅与另请求正常；各自读取位置独立 | 未因慢订阅预取/缓存无界，不影响生成、他人状态/Usage/序号；每个生成各调用1次 |
| E06 | 预置O1有效completed/failed流式记录及旧payload，再新建O2请求 | 两代有效事件均可按游标读取，终态结束 | 旧记录/终态/Usage不改写；0历史重调，O2序号仍独立从1起 |
| E07 | send已返回但客户端模拟尚未持久化处理游标，重连仍携旧seq；另一例成功处理后再续传 | 前例允许重投相同id；后例只读取更大id | 去重由调用方按seq完成；不伪造ACK/恰好一次，不新增订阅账本/重复Usage |

### 2.4 时限与观测

| 编号 | 准备/输入/操作 | 响应及行为 | Provider/PG/诊断断言 |
| --- | --- | --- | --- |
| T01 | 首次GET后模型等待；受控时钟走多个空闲周期、连续空running事件；分别两个订阅 | 心跳按各自最后成功发送的15秒计，无忙等；有数据立即发送 | 无心跳行/seq/虚假TTFT；不延长生成期限，正常常量单独核对 |
| T02 | 在delta/heartbeat/terminal/最终空帧处分别阻塞或抛OSError；模拟30秒单send预算（受控加速） | 只结束该订阅，无第二发送者或模型失败通知 | 生成/其他订阅继续，Request终态不被覆盖，无关闭共享Provider；传输诊断不当作生成error_code |
| T03 | 分别阻塞启动认领、上游、实际PG写入至剩余期限；反复重连及无订阅两类 | 同一B起点总期限；停止生成后条件failed收口；无新增正常生成/发送预算 | 认领/写入用更小剩余预算，后续GET不刷新deadline；已有completed不覆盖；无模型重试 |
| T04 | 受控时间使健康订阅超过30秒并读到终态；旧历史分页回放也超该时长 | 不把单send30秒误当整个GET超时 | 每次发送受限，健康总订阅不被截断；数据库事务不跨发送/睡眠保持 |
| T05 | 用显式时间点区分首上游、首次发送、远端结束、落库；两个订阅交错，完成后/重启后再回放 | TTFT/Generation观察点明确，首个实际发送最多补记一次，未知null | 回放不覆盖原指标；不以总耗时减TTFT冒充生成时间；订阅失败不污染原生成结果 |

### 2.5 故障、未知提交与进程

下列停启/强退故障使用专有PG并串行运行。故障替身包裹真实commit/rollback/SQL，用于制造确认丢失或控制窗口，不替代存储事实。

| 编号 | 准备/输入/操作 | 响应及行为 | Provider/PG/诊断断言 |
| --- | --- | --- | --- |
| F01 | 分别在POST插入、GET查询/认领提交前真实SQL/连接失败；入口原有4xx的记录同时失败 | SSE前503；原入口4xx保持；订阅已开始则只结束自身 | 0额外模型调用；事务无半成品；安全日志无body/凭据，无内存回退 |
| F02 | 真实增量插入后、计数更新后各注入失败并回滚；终态三表任一步失败 | 未提交事件不可读；生成停止并条件收口 | 计数/事件原子回滚；Request/Attempt/终态一致，无矛盾或重复终态 |
| F03 | 对创建/启动/增量/终态分别：真实commit已成功后丢确认、真实rollback后丢确认；穿插另一GET | 按各阶段核对，不盲重发或补相反终态 | 创建已提交pending保持；启动已提交无执行者失败且0调用，未认领仍pending；已提交delta不重复；已提交completed保持，实际Provider次数至多1 |
| F04 | Provider无终态、非法内部事件、意外Python异常；认领后任务注册失败/运行任务意外退出 | 稳定failed/internal_error或provider_invalid_response，监督异常 | 无永久running、无自动第二次生成；确认未进入Provider时0调用，原错误/已知Usage不丢失 |
| F05 | 一次仅破坏某订阅查询；另次停自有PG使生成写入失败；A停止/B保持活跃后恢复 | 单订阅错误不改生成；写失败停止模型；恢复只收口A，B继续 | 上游close有界，PG失联期不假称终态已保存；恢复后唯一终态，无误伤活跃任务 |
| F06 | 正常停机时有活跃订阅、已无人订阅的生成及pending；DB在线/离线两变体 | 所有自有任务/连接有界退出，pending仍保留 | Uvicorn有限排空+应用清理时限可观察，无僵尸任务/长事务；未完成最终写入由启动恢复兜底 |
| F07 | 独立进程在流生成中SIGKILL，同库重启两次；另预置canceling/pending及三终态 | running/canceling失败，pending保持，终态不变；历史可续读失败终态 | 不自动调用Provider，不重复失败事件；保留旧seq/已知Usage。pending随后有效GET才调用一次 |

### 2.6 回归、隔离与真实接入

| 编号 | 准备/输入/操作 | 响应及行为 | Provider/PG/诊断断言 |
| --- | --- | --- | --- |
| R01 | 原unit/integration/protocol/smoke；F5直接POST SSE断言按本次明确变更迁移到POST+GET，普通/结构化/探测保留 | 原文本精度/预算/错误与统一Provider协议不回归；不新增Chat/Responses对外路径 | 保留旧测试意图，不删掉零调用/增量/Usage/清理断言；原报告保持历史 |
| R02 | 禁止实际配置/密钥读取与非回环Provider连接哨兵；结束比较实现前保护摘要和资源清单 | 默认测试不依赖维护者切Mock/真实设置 | 自有PG密码/端口/库/卷，源资产/历史/真实凭据不改；容器/卷/进程清理；故障套件串行 |
| R03 | 全目录collect；Python3.12关键测试DeprecationWarning视为错误；稳定版本全量 | 无同名模块冲突或异步泄漏；实际退出码/收集项明确 | 新文件用f5o2前缀；导入、夹具/端口错误不算业务Red，不把历史结果冒充当前 |
| V01 | 显式真实主Chat、备Responses各一条：POST→GET首段→关闭→续传至终态；max_output_tokens=128、timeout_seconds=30 | 实际SSE及业务id，终态可完整回放 | 两条共2次有成本模型生成；每请求1 Attempt、Usage/载荷/seq与实际回执对应；真实快模型提前完成不冒充已证明断连时继续生成，该点用E04屏障证明 |

## 3. 执行计划、证据与结论

### 3.1 环境与TDD

本项已于2026-11-02获设计和测试方案双确认，已保存当前代码/测试/安全配置及历史/资产摘要并运行基线；沿存储→所有权→订阅→进程推进Red→Green，再相关回归。导入/依赖错误、Docker启动失败不是业务Red；补充验收测试若首次即Green如实记录，不补造失败。

用当前锁定依赖和Python3.12，SQLAlchemy/psycopg/Alembic及PG18沿O1。常规测试使用MockProvider+真实自有PG；每条模型调用/关闭计数和参数都可观察，不能替换待测Manager/Store为恒定成功结果。PG观察用独立连接，真实TCP证明提前到达与断连；ASGI发送屏障证明阻塞，不用“客户端暂时不读”推断TCP背压。

实施时新增测试入口采用不同basename（如test_f5o2_storage.py、test_f5o2_http.py、test_f5o2_process.py），具体划分写入实施步骤。先收集后运行目标/相关用例，稳定后全量；数据库停启等故障不与其他套件并行。无变化、失败或未解问题不重复启动同目的检查。

原计划命令（实际执行以3.5–3.9及evidence.json为准，本段不是运行回执）：

```bash
uv run --python 3.12 python -m pytest --collect-only -q
uv run --python 3.12 python -m pytest tests/unit tests/postgres tests/integration -k f5o2 -q
uv run --python 3.12 python -m pytest tests/smoke -k f5o2 -q
uv run --python 3.12 python -W error::DeprecationWarning -m pytest tests/unit tests/postgres tests/integration -k 'f5o2 or f5_execution or f4_json_codec' -q
uv run --python 3.12 python -m pytest -q
uv run --python 3.12 python -m pytest tests/live -k f5o2 --run-live -x -q -o junit_family=xunit1
```

关键回归还包括实际受影响的O1状态/恢复及普通/结构化接口；实施步骤列实际节点/命令，不以-k列表代替覆盖。真实PG缺前置条件必须明确失败，不以skip或内存替身凑通过。

### 3.2 真实调用与费用授权范围

用户确认本测试方案时已授权显式执行V01，**主备各一次，共2次有成本真实模型生成**；使用本机已有Provider凭据、临时真实绑定和隔离PG，不切维护者默认Mock，不复制/打印Key、不重跑旧14个live。真实前置条件缺失则报告，不伪装成功；若失败需要额外付费样本，说明原因及范围，不无意义重复成功样本。

保存实际命令、request_id、适配器/协议、状态、Usage、事件数、阶段与安全回执，不保存认证头/实际Key。两条接入同为DeepSeek服务的不同适配/协议，不宣称两家供应商独立容灾。一次样本不证明生产可靠性、供应商停止计费或端到端恰好一次。

### 3.3 自检、独审与结果

提交前一次自检对照本表32组及实际参数化变体，记录实际节点/原始输出、Provider副作用及SQL结果。独立Agent按项目三维审查、最多三轮；每轮实际提示词先保存，审查原始报告及可获得的工具/消息记录取证；只读任务约束不冒充工具层强制限制。前轮未变内容可由真实基线/hash复用，不能只看通过数或前轮Approved；关键材料缺口必须补。

此处为实施中记录：设计/测试确认后完成Red/Green及针对性验证；后续全量、真实Provider、独审和最终结论分别见3.6–3.9。用户确认本测试方案后自主制定实施步骤并实施，不另设例行“开始”确认。只有新契约歧义或第三轮仍阻断才返回相应用户节点。

### 3.4 模板与交接

沿用现有设计/测试模板，当前无新增通用缺口，不重复扩展模板。实施完成后同步README、架构、契约、AGENTS和进度，产出代码导读；公共文档已同步当前两阶段接口，验收状态仍以实际最终测试及独审记录为准。


### 3.5 实施记录与覆盖映射

实施步骤见[implementation-plan.md](implementation-plan.md)，代码阅读见[implementation-summary.md](implementation-summary.md)。实际执行者为主集成者与隔离任务执行者；存储、订阅可并行，PG停启故障套件始终串行。新增验收测试中首次即Green的补测不追述为Red。

基线：`uv run --python 3.12 pytest -q`，1228 passed / 14 skipped，退出0，205.07秒。纯逻辑/Store/HTTP的最初Red分别为缺少应用生成所有权、缺少pending存储、POST仍返回SSE200；均先记录正确失败再实现。随后真实SQL/ASGI故障复现未调度协程泄漏、订阅读库失败HTTP未正常结束、离线孤儿收口跳过连接池关闭，并以原用例复测。中间出现一次测试DDL同步等待阻塞事件循环，已改线程执行并限制SQL锁等待；这是测试实现问题，不归为产品故障。

实际节点以`tests/`为根，参数化变体随同运行；下表覆盖职责与观测点，不靠测试数量替代规则。

| 组 | 实际节点/层次 |
| --- | --- |
| A01 | integration/test_f5o2_http.py::test_create_is_pending_without_provider_or_deadline；postgres/test_f5o2_storage.py 创建零调用 |
| A02 | integration/test_f5_gateway.py 的预检、字符、Token、严格类型与额外字段；test_f5o1_http.py 拒绝元信息零Attempt |
| A03 | test_f5o2_http.py 非法/省略游标、Last-Event-ID；test_f5o2_replay.py 显式前导零和分页游标 |
| A04/A05 | test_f5o2_http.py 不存在/错误类型/未来游标/204；test_f5o2_replay.py completed及failed终态 |
| L01/L02 | test_f5o2_http.py 20并发；smoke/test_f5o2_process.py 首段提交屏障/20订阅；Store 20认领仅一赢家 |
| L03 | test_f5o2_replay.py Prompt移除冻结、Provider/计数资源变化；test_f5o2_http.py 模型变化；process pending重启兼容 |
| L04 | test_f5o2_delivery.py 阻塞202 headers/body窗口与发送失败；发送过程中查询pending/生成独立完成 |
| E01/E02 | process提交先于TCP首段；原O1 HTTP提交屏障、已知/nullUsage；原F5 Unicode/SSE样式文本及心跳不落库 |
| E03/E06 | test_f5o2_replay.py 真实PG的205条O1历史增量+终态，0/99/100/101/200/205游标及两种终态，不改原行 |
| E04/E07 | process断连期间继续提交、旧游标重投/新游标续传、调用方按id去重，一次实际Provider尝试 |
| E05 | process20订阅/其他请求隔离；test_f5o2_subscriptions.py 慢/快订阅；delivery单订阅故障 |
| T01 | test_f5o2_subscriptions.py 两订阅独立受控时钟及多周期、空running、慢查询；O1 HTTP心跳无行/无seq |
| T02 | test_f5o2_delivery.py headers/delta/heartbeat/terminal/eof × block/error，均不改生成结果 |
| T03 | test_f5o2_faults.py 认领提交等待及真实增量事务等待计入B总期限；process无人订阅继续/不刷新期限 |
| T04 | 受控缩短单send预算证明总订阅可超过该预算；真实PG分页/连接释放另测，不声称实等30秒 |
| T05 | test_f5o2_metrics.py 两活跃订阅按实际send竞争只记一次；delivery历史/重启回放不改原指标 |
| F01 | test_f5o2_faults.py 五写入阶段真实SQL、GET预查/订阅读库、入口错误记录失败 |
| F02 | 原O1 HTTP after_insert/after_counter_update真实事务回滚；原O1 Store三表终态失败原子回滚 |
| F03 | test_f5o2_faults.py 创建/认领/增量/终态真实commit或rollback后丢确认，穿插并发GET；Store同类独立观察 |
| F04 | faults注册失败关闭协程、任务提前异常/取消、非法Provider/意外异常；原O1无合法终态验收 |
| F05/F06 | faults单订阅读库错误；process停启PG时A停止/B继续、在线/离线停机、有/无订阅；lifecycle离线孤儿仍关闭连接池 |
| F07 | process SIGKILL、同库两次重启、pending/canceling及三终态不重调、不重复失败；pending以后GET才调用 |
| R01 | 历史unit/integration/protocol/smoke全量；旧live仅迁移流式入口，不额外付费重跑 |
| R02/R03 | conftest密钥/非回环网络哨兵、专有PG/临时端口、保护摘要；collect、3.12 strict和全量原始回执 |
| V01 | live/test_f5o2_acceptance.py，主Chat/备Responses各一次，两条真实调用单独执行 |

原14条live为历史范围，默认不执行；本次新增2条live，使默认跳过16条。原live流式测试代码已适配POST202→GET/id解析，普通/Schema分支与fixture不变；仅收集8条相关历史节点，未重新付费验证。


### 3.6 第1轮独审前的本地与真实接入回执

| 验证 | 命令/范围 | 实际结果 |
| --- | --- | --- |
| 收集 | `uv run --python 3.12 pytest --collect-only -q` | 1342 tests collected，exit0 |
| 最终全量 | `uv run --python 3.12 pytest -q`（另存JUnit） | 1326 passed / 16 live skipped，269.19秒，exit0 |
| 3.12关键严格回归 | `python -W error::DeprecationWarning -m pytest`，352项Provider流/执行器、Store、旧/新HTTP、订阅/指标/故障 | 352 passed，120.62秒，exit0 |
| 两条真实 | `uv run --python 3.12 pytest -q tests/live/test_f5o2_acceptance.py --run-live -o junit_family=xunit1`（另存JUnit） | 2 passed，6.34秒，exit0 |

严格回归的完整参数、命令及退出码在[evidence.json](evidence.json)，不是只记录-k简称。各套含数据库停启的故障全量串行；strict事务故障与live使用不同专有PG，没有同时停启数据库。最终全量运行前后源码/测试摘要完全一致。首轮全量的2失败来自协议测试漏迁移首次GET；同两例复测2 passed后，才运行最终全量，未删除断言。

| request_id | 适配/协议 | 实际Usage input/output/total | 业务事件 | 事实 |
| --- | --- | --- | --- | --- |
| 76a98ca7-e538-44b2-84a2-d87be78f766f | deepseek / chat_completions | 243/21/264 | 22，首seq1 | pending零Attempt；GET后恰好1 Attempt，断开后按seq补读到completed |
| a407384d-cf76-4caa-8a76-666c1daa11a6 | openai_compatible / responses | 243/21/264 | 22，首seq1 | 同上；接口与PG载荷/seq/Usage一致 |

真实两条同为DeepSeek服务，实际返回模型名deepseek-flash，仅作内部接入证据，不增加公开字段。只执行本项2次有成本生成；快模型可能在订阅关闭前已完成，断连期间仍生成由受控Mock+真实TCP屏障证明。未知Usage仍由独立Mock变体验证；无生产、多实例、供应商停止计费或端到端恰好一次结论。

资产、迁移、历史工件和默认配置106份保护摘要一致。默认gateway.yaml未切真实；没有新增迁移或修改依赖。本节为第1轮审查前的回执；两轮审查及最终验收见3.7–3.9。


### 3.7 第1轮审查、复现与修正

[原生Claude Code第1轮原始报告](review-r1.md)未改写。CLI正常退出0，result.is_error=false、stop_reason=end_turn；初始化仅Read、MCP为空，实际66次Read，无凭据读取或权限拒绝；99份受审源码/测试/迁移/默认配置前后未变。两次差分路径拼写错误后已正确读取；一次full-final.xml过大读取失败，报告明确其全量数字依赖执行者回执，未冒充重跑。strict/live XML读取成功。CLI完整原始流与工具审计在临时证据目录，归档保留安全审计字段和报告，不将内部思考流作为验收证据。

审查报告为APPROVED、P0/P1/P2=0、P3=1；执行者没有据此宣告验收。以下均按原输入/时序先复现，再修复：

| 来源 | 确定问题与副作用 | 回归与最小修正 |
| --- | --- | --- |
| 执行者 | 两个新接口OpenAPI仍用默认校验错误结构；补model后GET错误被默认媒体类型记为SSE，与实际JSON不同 | `tests/unit/test_f5o2_openapi.py`两例；POST声明统一错误，GET显式声明成功SSE和通用Response文档基类，使错误模型媒体类型为JSON；实际仍返回EventStreamResponse |
| 执行者 | 前次认领真实rollback后，下一次claim已提交而任务未注册时，后台orphan恢复可提前把running写failed | `test_orphan_recovery_waits_for_inflight_retry_claim`；恢复与认领/注册共享请求锁，成功注册移除旧orphan |
| Claude第1轮P3，执行者真实PG复现 | 任务停止、终态延后提交窗口内的新订阅会触发通用reconcile，覆盖原错误码且已知Usage不落库 | `test_new_subscription_preserves_deferred_owner_error_and_usage`；Manager通过应用提供的awaiting_persistence谓词识别仍由原执行者排队的收口，订阅/孤儿恢复均不抢写，原恢复队列提交错误及Usage |

最后一项虽然工具评为P3，实测违反本项已确认的错误/已知Usage保留要求，本次作为必须消除的验收问题处理。修复没有允许终态后补写Usage或改写历史，而是阻止订阅抢先写错误终态。

隔离副本前两项定向验证40 passed；合入并补第三项后，3.12严格模式下相关41项40.76秒通过，exit0。最初在/tmp运行认领复现未加载pytest配置而失败，只属运行配置错误，不算业务Red；指定项目配置后正确复现running被改failed，随后同测试修复通过。OpenAPI两例、第三项PG复现均保留原始失败日志。

2026-11-02用户明确选择“改用全新审查Agent，并更新工作流”。当前根AGENTS/docs约定及通用模板已更新：首次审查上下文不继承实现对话，后续同一审查者复核；Claude Code改为可选；累计三轮上限不因换工具重置。原Claude记录保留，本项下一轮为累计第2轮、使用全新上下文Agent，尚未出结论。传入已确认需求、设计、测试方案、固定源码/真实差分及原始运行证据，不传私有实现推理或预设通过结论。

修订后全量已完成，见3.8；两条真实调用采集于上述修正前。修正涉及OpenAPI声明、恢复锁及待持久化所有权判断，Provider/预算/事件消费实现未变；不额外重复付费样本，最终结论明确这条版本边界。


### 3.8 修订后最终回归与第2轮审查准备

- 修订后串行全量：`uv run --python 3.12 pytest -q`（另存JUnit），**1330 passed / 16 live skipped，275.03秒，exit0**；运行前后源码/测试摘要一致。
- 三处修复及影响链的3.12严格回归：41 passed / 40.76秒，exit0；首轮352项严格回归保留其采集版本，不冒充修订后重跑352项。
- 真实两条保持原采集回执；没有追加付费调用，版本边界见3.7。最终全量包含新增4项，合计1346个节点。
- 本次第2轮采用`fork_turns="none"`的全新审查Agent，未继承实现对话；累计轮次仍为2。审查输入及独立结论另存，不以首轮APPROVED或测试数量预设本轮通过。

源码、测试、迁移及默认配置101份摘要已冻结；完整变更以实施前基线为准，另保留3份源码从第1轮到当前的精确差分及前后摘要。第1轮版本从原始Read结果/实际差分还原并校验字节摘要，不用手写说明冒充差分。工作流的4份文档更新一并纳入本轮检查。本段为第2轮审查准备记录，最终结论见3.9。


### 3.9 最终验收

**验收通过，2026-11-02。** 已确认32组及对应变体由实际节点/参数化断言覆盖，三处新增复现问题已消除；[第2轮全新Agent原始报告](review-r2.md)为APPROVED，P0/P1/P2/P3均0。累计两轮，未因工具切换重置，不再增加例行用户最终确认。

| 层次 | 实际结果与边界 |
| --- | --- |
| 最新完整离线回归 | **1330 passed / 16 live skipped**，275.03秒，exit0；真实PG、TCP/进程及停启故障串行包含在内，1346节点无收集冲突 |
| 修正相关3.12严格回归 | **41 passed**，40.76秒，exit0；旧352项严格回归保留为较早版本证据 |
| 两条真实接入 | **2 passed**，6.34秒，exit0；Chat/Responses同一DeepSeek服务，原采集版本与后续三文件修正差异明确，未追加调用 |
| 版本与保护 | 第2轮前后101份源码/测试/迁移/安全配置摘要一致；资产、迁移、默认配置与历史工件106份保护摘要一致，无依赖或迁移变更 |
| 独立性与工具边界 | 实际启动`fork_turns="none"`，提示词逐字归档；新Agent未继承实现对话。只读是任务指令，工具没有平台级写入封锁；命令种类依据其原始报告，执行者另行实测哈希未变，不把自述伪装成完整机器调用流 |

第1轮Claude实际仅Read/MCP为空的工具取证继续保留；本轮更换方式由用户明确确认，工作流/通用模板已经同步，历史报告和执行过的提示词不改写。[evidence.json](evidence.json)保留原始日志、命令/退出码、摘要及两轮审计；临时原始流/快照路径也在工件中注明。

本项不宣称生产、多实例、计费准确性、供应商独立容灾或端到端恰好一次。默认配置保持Mock，真实Key未改；测试专有PG/进程已关闭，无自动业务数据清理。下一步F6主动取消按新功能路径另行讨论，不预先实现。
