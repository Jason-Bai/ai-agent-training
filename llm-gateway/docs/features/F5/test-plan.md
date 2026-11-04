# F5：Streaming——测试方案

> 状态：验收通过。2026-11-01用户单独确认本文件具体用例及执行计划，授权TDD；范围确认、设计确认与本次测试确认分开记录。对应[已确认设计](design.md)，已按TDD实现；第2节为已确认验收要求，第3.6节起分阶段记录实际运行，最新见3.9，最终结论见3.10；此前各阶段结果分别保留。

## 1. 范围与通过标准

### 1.1 基线、层次与通过条件

保护 [F4](../F4/test-plan.md) 与 [F4-O1](../F4/optimizations/O1/test-plan.md) 已验收行为。历史离线 878/真实六例是此前证据，不替代 F5 回归；不预先承诺新增测试数量。

| 分类 | 实际被测对象与替换边界 | 不能证明 |
| --- | --- | --- |
| Gateway 行为 | 真实请求/预检/执行/Trace；只用脚本 MockProvider 替换上游，ASGI 发送出口可故障注入 | 云端能力、真实网络分块 |
| Provider 协议适配 | 真实 DeepSeekProvider/httpx2、OpenAICompatibleProvider/AsyncOpenAI 对接回环 HTTP/SSE 服务，核对请求字节与事件 | 真实 Key/云端能力 |
| 本地真实 HTTP/SSE | 单 worker 临时 Uvicorn 与真实流式客户端，受控 Mock 产生内容/等待/失败 | 外部供应商、多实例；客户端暂不读取也不能单独证明服务端 send 已阻塞 |
| 显式真实接入 | 临时 FastAPI → 两个真实适配器 → DeepSeek，主 Chat/备 Responses 各一次流式成功 | 双供应商容灾、全部云端异常或生产可靠性 |

四类均为必验，默认测试只运行前三类，真实两例单独显式启用。通过条件：本文件全部必测变体与 F1—F4 回归通过，次数/Trace/资源断言一致，真实两例有安全回执，代码导读和文档同步，原生 Claude Code 三维审查无 P0–P2。关键证据缺失或任一必验未执行，不称整项通过。

不测试 F6 主动取消 API、F7 恢复、F8 SQLite/查询、断点续传、多实例、真实模型抗注入效果或业务事实正确性；不能因这些未来能力未实现而新增产品功能。

### 1.2 公共断言与固定材料

- 基础请求使用设计 3.1 的 A123 样本；主/备逻辑名参数化。System/Few-shot/模板原文不变。成功文本固定为增量 `["订单", " ", "A123", " 的商品为充电器。", "\n"]`，终态 completed，Usage 默认 null；拼接结果必须保留空格/换行。
- 另一组已知 Usage 固定 input=10/output=5/total=15。未知为 null，不伪造 0。空字符串、仅空格、仅换行与有效文本分开测试。
- **零调用**：Provider stream/complete 调用数均 0、Attempt=0、请求 failed；**一次调用**：stream=1、complete=0、Attempt=1；失败不启动第二次调用、不访问备用 Provider。启动错误另记 C0：无请求/Attempt，客户端未构造（注入对象检查除外）。
- SSE 每个 data 的字段精确匹配设计；request_id 与响应头/Trace 一致、客户端提供的 ID 不生效；只允许四种公开事件，无 model/attempts/parsed/时间指标/供应商原始字段。JSON 错误四字段/data=null；已开始 SSE 不混入 JSON 错误体。
- 至多一个终态发送尝试，无终态之后的文本/心跳；成功必须上游正常完成且非空白回答、完成事件成功写出。客户端缺终态不能判成功。
- 根据 request_id 或固定请求标记关联 Mock 调用、Attempt、结果，不按并发列表下标配对。正常/失败/断连都检查挂起任务、单流关闭、共享客户端存活和 Trace 不再 running。
- 使用明显的假敏感标记注入请求/上游错误，扫描公开响应与 Trace，禁止完整消息/回答、Key、原始堆栈/错误正文泄漏。测试夹具本身保存脚本文本不等于产品 Trace 保存正文。

## 2. 用例

表格每个明确列出的变体都是必测；实现时参数化保留各自结果，不能用一个正常输入代表整行。

### 2.1 请求、配置与预检（设计 3.1—3.2）

| 编号 | 准备 / 输入 | 预期响应或行为 | 参数 / 副作用 | Trace | 层级 |
| --- | --- | --- | --- | --- | --- |
| F5-C01 | 主/备默认 Mock；省略/显式合法输出与超时 | SSE 成功，生效默认或请求值正确 | 一次调用，统一 stream 参数无 Schema | 模板/指纹/预算/operation=streaming | 单元+HTTP |
| F5-C02 | response_schema={}、null；stream=true、false；api_protocol；未知字段 | 422 invalid_request，JSON | 零调用 | failed | ASGI |
| F5-C03 | 缺 model/prompt/messages；0/2条消息；system/assistant；空白内容；非法 JSON；重复 JSON 键 | 沿用严格入口规则，422 | 零调用，不能被解析器/模型自动转换掩盖 | failed，无不完整 Attempt | 单元+ASGI |
| F5-C04 | 预算/超时 null、true、字符串、0、负数、小数；正整数上限/上限+1 | 类型错422；平台超限400既有code；边界允许 | 拒绝零调用；允许一次调用，不擅自调低值 | 已完成步骤元信息准确 | 单元+ASGI |
| F5-C05 | 原消息20,000/20,001；context10,000/10,001；渲染User20,000/20,001 | 临界允许，超限按既有分类拒绝 | 拒绝零调用；完整System/Few-shot/User均计数 | 指纹/计数与参考编码一致 | 单元+ASGI |
| F5-C06 | 未知模板/版本、缺变量、额外变量、覆盖message；未知模型；能力false | 对应既有错误/unsupported_capability，JSON | 零调用 | failed，已取得元信息保留 | ASGI |
| F5-C07 | 变量错+模型错；模型未知+预算超限；能力false+预算超限 | 按设计3.1固定顺序首错 | 零调用 | 不记录未执行步骤的结果 | ASGI |
| F5-C08 | 窗口恰等输入+输出+余量；差1 Token；Few-shot与context_answer@1.2.0文本路径 | 边界允许/超预算拒绝，无Schema约束混入 | 拒绝零调用，不裁剪；出站消息与计数一致 | 消息指纹/计数版本稳定 | 单元+协议 |
| F5-C09 | supports_streaming=true/false；缺失/null/字符串/0/1 | 仅严格布尔可启动，false保留普通调用 | C0或正常，无远端能力探测 | 启动不建请求 | 单元 |
| F5-C10 | 声明true但适配器/注入实例无stream；支持stream但模型声明false | 前者启动拒绝；后者请求能力拒绝 | 前者无模型调用；后者零调用 | 无伪成功 | 单元+ASGI |
| F5-C11 | 两条默认Mock声明、静态测试注册表迁移；注入集合/凭据隔离 | 启动完整，无隐式真实调用 | 不读用户credentials/.env，不改变原客户端所有权 | 无无关请求 | 单元 |
| F5-C12 | 检查OpenAPI组件/路由与请求校验 | stream请求仅五类受支持字段，Schema/stream/协议参数均不出现；SSE与JSON错误类型清楚 | 文档不触发Provider | 不要求OpenAPI请求建模型Trace | ASGI |

### 2.2 内部事件、公开事件与失败（设计 3.3—3.6）

| 编号 | 准备 / 输入 | 预期 | 参数 / 副作用 | Trace | 层级 |
| --- | --- | --- | --- | --- | --- |
| F5-E01 | 固定成功增量，Usage null/已知两组 | 原文逐段转发，一次completed；不重复完整答案 | 一次调用，单流关闭1次 | completed；Usage对应 | 单元+HTTP |
| F5-E02 | completed和最后delta同事件；failed/output_truncated和最后delta同事件 | 先发最后文本再发对应终态；失败不续写 | 一次调用，无第二Attempt/备用调用 | 对应completed/failed | 单元+ASGI |
| F5-E03 | delta为空；空格；换行；组合后有正文；仅空白后completed | 空串不发；空白原样发；仅空白最终failed/provider_invalid_response | 一次调用，不trim；仅用非空白标记 | TTFT按已写出的非空delta；空白失败仍保留 | 单元+HTTP |
| F5-E04 | 首段前failed；已输出后failed，涵盖认证/额度/限流/连接/超时/不可用/参数错误 | 已建立SSE后均response.failed、安全消息，不返回JSON | 一次调用，不重试/备用；已发内容不撤回 | failed，首段前TTFT=null | 单元+HTTP |
| F5-E05 | 无事件即EOF；文本后正常EOF；网络读异常 | EOF缺终态→provider_invalid_response；已分类网络异常→对应code | 一次调用，释放流 | failed，已知指标保留 | 单元+协议 |
| F5-E06 | 未知status；delta非字符串；running含usage/error；completed含error；failed缺/未知error；终态来源标记非布尔或与running/completed矛盾 | provider_invalid_response，不发该非法事件的delta | 一次调用，停止后续读取 | failed，不误记上游完成时间 | 单元 |
| F5-E07 | Usage布尔/字符串/负数/小数、总数不符；合法零Usage；actual_model非法类型 | 原始非法类型拒绝，合法值原样；不coerce | 一次调用 | 非法failed；合法Usage0与未知null分清 | 单元+协议 |
| F5-E08 | Mock在读取/发送组织中抛非预期异常；受控Provider故障 | 未预期internal_error与已分类故障分开，无堆栈 | 一次调用或未调用时0 | 原因正确，不遗留running | 单元+ASGI |
| F5-E09 | 终态后脚本继续给delta/另一终态；完成事件发送异常 | 不再消费/转发；失败写终态后不追加第二个 | 一次调用，close1，无遗留任务 | 首次成功写出则不改写；未写出则failed | 单元 |
| F5-E10 | 中文/emoji/引号/反斜杠/CRLF及正文伪造event:data行；网络任意分片 | JSON解码后原文精确相等，不注入额外事件 | 一次调用，SSE framing正确 | request_id一致，无正文持久记录 | 编码单元+HTTP |
| F5-E11 | 不传/伪造X-Request-ID；多请求受控交错 | ID由Gateway生成，每事件/响应头/Trace正确关联 | 每请求一次调用；不按完成顺序误配 | 无交叉文本/Usage/错误 | ASGI+HTTP |

### 2.3 时间、心跳、背压与清理（设计 2.3—2.4、3.7）

| 编号 | 准备 / 可观察时序 | 预期 | 副作用 | Trace | 层级 |
| --- | --- | --- | --- | --- | --- |
| F5-T01 | Provider产首段后等待release_final；客户端实际收到首段后才释放 | Provider尚未结束，客户端已收到首段；不得先收完整流 | 一次调用，无完整回答缓冲 | 记录首段写出，不提前completed | 真实HTTP |
| F5-T02 | 上游建连/首文本等待；推进时钟至15秒边界前/到达/后 | 之前无heartbeat，到达可写时有；无正文也能心跳 | SSE先建立、Provider1次；心跳不重建上游读取 | TTFT仍null，Usage未知 | 单元+真实HTTP |
| F5-T03 | 文本写出后不足/达到15秒；上游内部空事件持续到来 | 以最近成功发送计算；空事件不延长空闲期 | 无并发send，pending read最多1个 | 心跳不计回答/TTFT | 单元 |
| F5-T04 | 心跳持续；文本持续；Provider一直无结果，分别越过总期限 | 均request_timeout，deadline不滑动；失败事件不可写时允许EOF | 停止读/心跳/关闭本次流，不再调用 | failed，总期限/已知指标正确 | 单元+HTTP |
| F5-T05 | 控制ASGI响应头send、文本send、心跳send、completed-send、已知provider_auth_error后的failed-send分别永久等待 | 实际发送等待受同一总期限约束，不无限挂起；已有上游失败也不能延长发送 | 响应头失败尚未调用可0；其余一次调用；无并发发送/新预取/第二次终态尝试 | 最终error_code=request_timeout，已有provider_error_code=provider_auth_error保留；未写出不记成功 | ASGI故障注入 |
| F5-T06 | Provider读超时先于deadline；deadline先于读超时；同轮可观察到期 | 前者provider_timeout；后两者request_timeout | 一次调用，不重试，清理有界 | 首因与终态一致 | 单元+协议 |
| F5-T07 | SSE后首段前断连；首段后断连；上游completed但下游尚未完成时断连 | 内部client_disconnected，无canceled，无终态送达保证 | 读取停止、close1；不关闭共享Provider客户端 | failed；保留上游completed/Usage | ASGI+真实HTTP |
| F5-T08 | completed成功写出后断连；失败先到/正常终态先到/期限先到受控交错 | 已成功写出的终态不变；没有两个终态发送尝试 | close1，心跳停止，无挂起任务 | 恰好一次收口 | 单元 |
| F5-T09 | 慢send屏障挂起；释放后下一次读取 | 挂起时无额外预取/无界积压，不并发发送heartbeat | provider next调用计数保持不增；释放后才增 | 仍running，释放完成后正常收口 | 单元+ASGI |
| F5-T10 | 正常/失败/断连/超时的close；close抛错/永久等待 | 每流close1，清理共享固定宽限，不覆盖首因/已完成终态 | 无遗留受控任务；共享客户端仍可用 | 关闭问题安全记录，不保存异常正文 | 单元 |
| F5-T11 | 同一Provider并发A断连/超时，B正常完成；进入/结束顺序不同 | A失败，B正文/心跳/终态不受影响 | 只关闭A的流；应用退出才关闭自有共享客户端 | 按ID关联，各自正确 | HTTP+单元 |
| F5-T12 | B=0，上游首段2，下游首段3，上游终态7，结束8；心跳先到 | ttft=3000、generation=5000、总耗时8000毫秒 | 用同一单调时钟，不混用下游发送和上游到达 | 原始时间点与推算相符 | 单元 |
| F5-T13 | 没有文本；首文本是空白；文本只收到未写出；有文本但无上游终态；已知远端failed终态；Adapter网络故障合成failed | TTFT/generation按各自端点是否存在决定；未知null，合成failed不能填远端终态时间 | 不额外调用模型取Usage/计时 | 正确保留，不能填0或总耗时-TTFT | 单元 |
| F5-T14 | 预检耗时、SSE头发送耗时、受控清理耗时 | B不含预检，覆盖头发送；总执行耗时含实际有界清理 | 普通调用latency语义不变 | 无混淆指标，B前拒绝流式指标未知 | 单元 |

### 2.4 真实适配器的协议字节（设计 3.4）

| 编号 | 回环服务返回 / 出站观察 | 预期统一结果 | 副作用 / 记录 |
| --- | --- | --- | --- |
| F5-W01 | Chat请求原始JSON与分块响应 | stream=true/include_usage=true、受控messages/非思考/输出预算，选择正确URL；不携带Schema | 真实适配器1请求，无SDK/HTTP隐式重试 |
| F5-W02 | Chat角色空块、正文、最终choice含Usage/stop、DONE；Usage缺失变体 | running增量→completed，Usage已知或null；Gateway不读finish_reason | 不把DONE当回答；元信息对应 |
| F5-W03 | Chat仅DONE；stop后无DONE；DONE前无正常结束；非法JSON/choice/content/role | provider_invalid_response；不得因连接关闭成功 | 单次调用/失败/关闭 |
| F5-W04 | Chat length、content_filter、refusal、aborted、insufficient_system_resource、tool_calls | 对应output_truncated/过滤/拒答/不可用；工具为invalid_response | 失败前合法文本保留，无fallback |
| F5-W05 | Responses请求与created/in_progress/output_text.delta/item.done/completed | 正确/responses、input、stream=true/store=false/effort=none/预算；只有delta转文本，completed才完成 | 真SDK流式读取，末尾完整output不重发 |
| F5-W06 | Responses incomplete/max_output_tokens；failed的已知/未知error；refusal/content_filter | 归一化平台失败；未知上游error→provider_error | 保留合法Usage/受控状态，不透传原message |
| F5-W07 | Responses只有text.done/item.done；无终态EOF；terminal与response.status矛盾；工具/非文本输出 | 不成功，按invalid_response或已知失败分类 | 不把非文本事件转回答，不补全答案 |
| F5-W08 | 两协议中文UTF-8跨字节、JSON跨网络块、SSE注释/空行/CRLF/多行data；malformed事件 | 正常分片不丢字；非法类型/JSON不被SDKcoerce，按invalid_response | 协议解析层承担；记录实际字节断言 |
| F5-W09 | 两协议HTTP401/402/429/400/404/422/5xx、连接错误、读超时、读中断 | 正确provider_*；SSE已建立所以公开failed，不回退JSON | 每变体1请求/Attempt，不自动重发 |
| F5-W10 | 真实适配器在首个增量后暂停上游；随后关闭Gateway客户端 | 本地HTTP先收到增量；断连后上游响应被关闭 | 证明适配器未先完整读取；共享客户端可复用 |

### 2.5 回归与真实接入

| 编号 | 场景 | 必验结果 | 证据层次 |
| --- | --- | --- | --- |
| F5-R01 | 运行原全部单元/集成/协议/进程测试 | F1—F4请求顺序/指纹/字符与Token/JSON精度/Schema/探测/异常/生命周期不回归；新增能力字段仅必要迁移 | 默认完整离线 |
| F5-R02 | 默认配置/资源/历史证据/用户.env与credentials保护；文档路径与状态 | 除显式能力字段迁移外无运行配置改写；固定资源/Prompt/原历史验收未变 | 文件摘要+文档检查 |
| F5-L01 | general-primary，DeepSeekProvider/Chat，固定A123请求 | 至少一个非空delta、唯一completed、已取得合法Usage；Trace/输出预算/实际上游身份与协议相符 | 显式真实 |
| F5-L02 | general-backup，OpenAICompatibleProvider/Responses，同一请求 | 同上；实际走Responses，不能用Chat替代 | 显式真实 |

两条真实成功样本记录各阶段耗时与计数；本地预检输入计数与实际 input_tokens 应一致，若有差异先失败并定位，不通过加安全余量或重试掩盖。不能要求云端一定切成固定段数、生成固定字面答案、15秒内必定出现心跳或达到特定延迟。云端失败与断连的可重复边界由 Mock/协议/本地HTTP证明，不能标成真实云端故障实验。

## 3. 执行计划、证据与结论

### 3.1 环境与测试组织

仅文档确认后实施。使用项目 `uv sync --locked`、Python≥3.12；最低3.12关键兼容测试使用独立临时虚拟环境，不能改写主环境。以现有 pytest/pytest-asyncio、Uvicorn、httpx2、真实SDK为基础，不因测试方便增加产品依赖。

拟新增 `tests/unit/test_f5_contracts.py`、`test_f5_execution.py`（含时间/清理）、`tests/integration/test_f5_gateway.py`、`tests/protocol/test_f5_providers.py`、`tests/smoke/test_f5_http_server.py`、`tests/live/test_f5_streaming.py`；可在实施步骤按职责最小拆分，保留本表ID映射。旧测试仅作显式能力字段和必要注入契约迁移，原断言与黄金不可放宽。

已落地测试入口（实际结果见3.6，命令列表本身不作证据）：

```bash
uv sync --locked
uv run python -m pytest tests/unit/test_f5_contracts.py tests/unit/test_f5_execution.py tests/integration/test_f5_gateway.py -q
uv run python -m pytest tests/protocol/test_f5_providers.py -q
uv run python -m pytest tests/smoke/test_f5_http_server.py -q
uv run python -m pytest -q
uv run python -m pytest tests/live/test_f5_streaming.py --run-live -x -q -o junit_family=xunit1 --junitxml=/tmp/llm-gateway-f5-live-results.xml
```

### 3.2 故障注入与时序证明

- 脚本 Mock 记录每次stream参数、next次数、单流close次数和共享client close次数。脚本支持合法平台事件、受控失败、非预期异常及等待屏障；不增加产品HTTP开关，不把执行主链或SSE编码替换成固定成功。
- 提前到达采用握手屏障：先产一段，再等待 `release_final`；真实HTTP客户端收到完整首个事件后才释放。测试外层 watchdog 到期则失败并清理，避免“先缓存完整答案”的错误实现导致测试永久等待。
- 心跳/指标采用受控单调时钟及最小私有测试注入，检查默认常量确为15秒，不让断言从同一个生产常量推导预期。真实HTTP心跳可缩短内部测试间隔，但明确不是生产15秒墙钟实测；不新增请求参数或一套Clock框架。
- 超时/背压控制实际ASGI send：分别阻塞响应头/文本/心跳/终态，断言固定deadline能结束挂起发送；真实客户端不读只能作辅助，不能替代确定性send屏障。
- Provider读取任务跨多次心跳保持同一次等待；慢send期间next计数不增加。pending read与close以受控事件观察；取消/超时后等待清理结束再检查任务，不能在清理前拍快照误报。
- 竞争测试用屏障决定先后，不用随机sleep猜顺序；至少覆盖终态发送前断连、发送中异常、成功写出后断连及截止时刻完成。按设计只断言服务端观察到的写出，不能声称客户端已经收到。
- 清理共享1秒宽限已随完整设计确认；本测试方案确认后，用受控时间/可取消等待覆盖抛错与卡住，断言共享宽限及有界退出；不声称可以强杀忽略取消的任意第三方协程。
- 真实HTTP测试启动临时回环单worker子进程，通过仅供测试的进程通信/隔离快照观察Mock与Trace；不加产品Trace/故障路由。不依赖进程内TestClient/ASGITransport证明分块到达。用finally关闭客户端、socket、服务任务/子进程与临时文件。
- 协议测试运行回环SSE服务，保留真实HTTP/SDK解析；核对实际请求字节、分帧后的值/类型和关闭行为。注入的Usage非法类型在转换之前被识别，不只断言构造后的Pydantic对象。

### 3.3 真实接入与安全证据

默认整套测试不读实际credentials、不访问外网，live默认跳过。显式 `--run-live` 后，缺本机配置/Key/网络能力时失败，不能静默跳过或换Mock。测试用独立注册表指向真实两条路径，不修改用户默认YAML；从本机既有 `config/credentials.yaml` 读取凭据，Key不进入聊天/命令/日志。

正式新增真实验收为主/备各一次成功流，共两次计划调用。只有单次执行失败需要定位/复测时才增加调用，分别记录失败与重跑，不能用最终成功覆盖首轮。F1—F4默认离线全回归是必验；其原真实六例不自动重复收费，如实施改变其真实出站行为再判断必要复测并记理由。

记录 request_id、adapter/protocol、受控上游状态、actual_model、模板/指纹、计数版本、生效预算、本地输入数/实际Usage/差值、TTFT/Generation/总耗时、调用/Attempt数和终态。只保存安全元数据，不保存真实文本/消息/完整上游对象、认证头或Key。

### 3.4 执行记录与独审

| 项目 | 当前结论 |
| --- | --- |
| 分段需求与四层测试范围 | 用户已认可 |
| 完整设计 | 2026-11-01用户确认；两轮文档自检记录见3.5 |
| 本文件具体用例与执行计划 | 2026-11-01用户在逐例方案确认问题后回复“确认”，已授权TDD |
| Red / Green / 相关回归 | 已执行，最终全量1099 passed / 8 live默认跳过；见3.10 |
| 实际HTTP与两条真实流式 | 子进程6例通过；最新主备真实2例通过，累计6次F5真实调用；见3.9 |
| 最低Python3.12关键集 | 独立环境最终235 passed，DeprecationWarning视为error |
| implementation-summary.md | [按当前实现建立](implementation-summary.md)，随代码参与独审 |
| 原生Claude Code三维独审 | 第2轮APPROVED、P0–P2=0，P3=1已补HTTP两例；首轮核实/最终结论见3.8/3.10 |
| F5整项验收 | 验收通过；实际结论见3.10 |

实施后按用例ID补充实际覆盖文件、命令、环境、通过/失败数及原始Red/Green/HTTP/真实回执，关键运行同步归档 `evidence.json`。先确认失败来自目标行为，不把导入错误或缺文件当有效Red。测试数重叠要注明，不将不同层重复数量相加。

独审使用[项目工作流](../../../AGENTS.md)和[Claude Code提示词模板](../../templates/ClaudeCode审查提示词模板.md)，执行前归档每轮实际提示词，原生CLI只读审查需求正确性/实现一致性/系统正确性。最多三轮完整代码审查；P0–P2阻断，修复后原场景复测，第三轮仍阻断交用户重审。记录真实工具读取、退出/结果、审查者与执行者证据来源；不能以启动/读文件或文档检查称通过。

### 3.5 模板回看与文档核验

本项暴露的通用要求是：异步系统的“上游结束、服务端写出、客户端收到”有不同观察点；要用受控时钟/屏障、阻塞出口和清理计数证明，不能只收集最终结果或使用sleep。设计/测试模板最小补充这些提问，与具体15秒/1秒/SSE字段分开。

文档落地后检查相对链接、JSON/YAML示例、围栏和F5规则交叉一致，保护源码/测试/运行配置/资源及历史验收工件；实际检查回执另行记录。该检查仅证明文档结构与改动范围，不证明本方案实现正确，模板改进效果也尚未验证。

2026-11-01 文档核验：本轮新增2份F5方案，修改8份当前说明/模板；140处本地文件链接存在、3个JSON代码块可解析，围栏/表格列数及51组用例编号检查通过。写入前后摘要核对其余129份既有文件未变，包含源码、测试、运行配置、资源及历史验收工件。原始检查回执为本机临时工作目录的check.json（目录定位文件 `/tmp/llm-gateway-f5-docs-path`）；不将它冒充功能测试回执。

按brainstorming流程做了两次只读文档检查：首次指出“Provider已失败但failed终态发送又超时”的错误优先级不清；已按总期限约束明确最终执行错误、保留provider_error_code并补T05变体，第二次文档复核Approved。该复核由文档检查子任务完成，未运行产品测试或真实调用，**不是原生Claude Code代码审查，也不计入实现后的三轮代码审查**。随后用户于2026-11-01确认完整设计；本测试方案随后也由用户单独确认，进入实施，不据此声称任何测试已通过。

### 3.6 实施、Red/Green与正式功能验证

执行者为本轮实现任务及其切片子任务；原始运行者、命令、退出码、耗时和stdout保存于[evidence.json](evidence.json)。下列数量有重叠，不能相加。文档/计划检查与实现后的原生Claude Code审查分开。

| 阶段 | 实际结果与意义 |
| --- | --- |
| 基线 | 878 passed / 6 skipped，53.33秒；源码/测试/默认配置67份已复制基线，历史资源及本机文件51份摘要保护 |
| 入口/执行Red | 路由6个404目标失败；prepare_stream缺失断言1个目标失败，均发生于对应实现之前 |
| 契约Red/Green | 首轮39 failed/4 passed→43 passed；注入能力3个DID NOT RAISE→47 passed；诊断来源2个失败→49 passed；最终与旧配置/生命周期共218 passed |
| Provider Red/Green | 37个stream缺失目标失败→37 passed；拒答delta/part生命周期各有新增有效Red，修复后最终新61+旧83协议场景共144 passed |
| 嵌套Usage | 初次1 failed/3 passed；补齐自洽非法值后3 failed/1 passed→严格重验后全部通过，未修改全局Usage语义 |
| 取消/清理 | 清理期间和失败通知期间取消留下running的2个有效Red；同步finally收口与有界shield清理后通过。pending read取消中的关闭异常另有1个Red→保留cleanup_error_code |
| 异常/心跳 | 意外Provider OSError误归下游断连1个Red→只在实际发送边界翻译。连续2000个空事件饿死心跳1个Red→独立检查空闲时钟后通过 |
| OpenAPI | 真实GET /openapi.json为500的1个Red；定位自定义Response构造器缺status_code默认值，修复后请求字段与SSE文档验收通过 |
| 真实ASGI阻塞 | 头/文本/heartbeat/completed/failed五种实际send等待，5 passed；总期限包含实际发送，Provider原错误分类保留 |
| 真实进程HTTP | 最终6 passed；客户端收到首段才释放上游的握手、心跳、前后断连、失败、并发隔离及总期限；短心跳为测试私有注入，不冒充15秒墙钟实测 |
| 中途回归 | 1052/1087/1089 passed分别属于对应阶段，不替代最终版本；完整输出与时间顺序保留 |
| 最终全量 | `uv run python -m pytest -q`：1090 passed / 8 skipped，72.64秒；标签frozen-full，无warnings/失败 |
| 最低3.12 | 独立临时虚拟环境，F5契约/执行/HTTP/协议+原精确JSON关键集226 passed，14.47秒；DeprecationWarning视为error，与全量重叠 |
| 最新真实两例 | 2 passed，1.86秒；两次均input_delta=0，见下面回执，不代表通用计数一致或生产验证 |

不计有效Red的记录也原样保留：跨目录同名测试造成collection mismatch；部分初期旧配置回归发生于其Adapter方法尚未集成；未listen端口在macOS表现为timeout的夹具假设；旧快照反证第一次缺streaming模块。网络新测试首跑即Green，随后旧快照仅测试bootstrap适配得到404反证，不能冒充实施前Red。`same-turn-deadline-red`虽标签有red但实际首跑PASS，说明该风险未复现，只作为补验收；未据此修改实现。

当前76份源码/测试/默认配置在frozen-full和py312-frozen各自运行前后一致。live-release之后仅增加同轮deadline测试，产品源码未改；该真实运行的76份输入在其运行前后一致。最终51份受保护资源/历史验收/本机.env与credentials摘要未变，默认配置仅两条模型各增加显式能力字段，未切真实。

### 3.7 首轮独审前真实样本、覆盖映射与边界

本项计划两次真实调用先取得2 passed（[首轮原始回执](live-results.xml)）；随后清理诊断、异常责任方与心跳实现有修订，再显式复测最新两例2 passed（[最新原始回执](live-release-results.xml)），累计4次实际F5调用。没有自动重试、静默换模型或覆盖首轮。F4原六例本次默认离线回归，不重复实际收费调用。

| 路径 | 本地/实际输入 | 差值 | TTFT ms | Generation ms | 总耗时 ms |
| --- | --- | --- | --- | --- | --- |
| DeepSeek/Chat | 514/514 | 0 | 534.88 | 137.70 | 672.98 |
| Compatible/Responses | 514/514 | 0 | 563.82 | 117.54 | 681.75 |

这是两次固定输入样本的服务端观察，不是性能基准；Generation含网络/读取节奏，客户端首次收到的计时另在JUnit元信息中，不与TTFT混用。未知元信息仍为null。两条都连DeepSeek，不证明独立供应商容灾。

| 验收规则 | 实际覆盖文件与补充证据 |
| --- | --- |
| C01—C04/C09—C12 | tests/unit/test_f5_contracts.py、integration/test_f5_gateway.py；输出/超时上限复用同一_budgets，合法上界另由原F3预算回归覆盖 |
| C05—C08 | integration/test_f5_gateway.py的原消息/context/完整渲染临界、窗口等式与顺序；固定计数资源/Few-shot输入原黄金回归 |
| E01—E09 | unit/test_f5_execution.py与protocol/test_f5_providers.py；非法内部实例及nested Usage重验、无终态、截断、单次终态与异常归属 |
| E10—E11 | integration/test_f5_gateway.py的编码/ID，smoke/test_f5_http_server.py的真实并发隔离；旧请求交错回归按ID关联 |
| T01/T02/T04/T07/T11 | smoke/test_f5_http_server.py的6个实际单worker/TCP用例；对应纯逻辑补充在unit/test_f5_execution.py |
| T03/T05/T06/T08—T10/T12—T14 | unit/test_f5_execution.py，integration/test_f5_gateway.py的实际ASGI send阻塞；默认间隔边界/空事件饥饿、同轮deadline、清理再次取消/异常、时间点推算 |
| W01—W10 | protocol/test_f5_providers.py，真实SDK/HTTP回环61例，原协议83例保留；真实适配器先段后终态屏障、socket关闭与共享client存活 |
| R01/R02 | 最终全量1090/8skip；资源/历史/本机配置保护、完整diff、文档与实际29个源码文件目录一致性 |
| L01/L02 | live/test_f5_streaming.py显式两条路径；各轮JUnit与安全元信息分别归档 |

模板实际回看：测试收集冲突补充真实collect要求；清理中再次中断和连续内部活动饿死外部周期输出的反例补充异步时序提问。未把F5固定数值或事件名推广到模板，也不据此宣称通用审查检出率提高。

### 3.8 原生Claude Code审查

原生第1轮已完成：[实际提示词](review-prompt-r1.md)、[CLI原始报告](review-r1.md)，退出0、is_error=false、stop_reason=end_turn，947.31秒，报告APPROVED/P0–P3均0。执行者未据此直接验收：追加复现发现元信息丢失，且报告反例6的“头发送返回时已过期限必然零Attempt”推导不成立，按同一已确认契约修订后提交完整第2轮。

实际日志为82次Read、59个唯一文件；63项必读清单中4项未见单独Read：项目AGENTS.md、空utils/__init__.py、test_f3_config.py、test_f5_streaming.py。后两项变更出现在已读完整diff中，但不冒充单独读了文件；原报告自述已读范围与实际日志不一致，以日志为准，第二轮必须补齐。Read无错误；另有一次范围外MCP浏览器工具请求被权限拒绝、未执行。原文不修改，报告内根相对链接按项目根理解。76份源码/测试/默认配置在整个首轮审查前后未变。

| 执行者核实事项 | 复现与处置 |
| --- | --- |
| 已知Usage/模型元信息在缺尾标记、损坏尾块或后续故障时丢失 | 离线实际Adapter→Gateway先复现合法Usage10/5/15未进Trace；隔离副本补2个Chat与2个Responses正确Red，保存每次流内已严格确认的元信息。新增实际回环到Gateway的2个Trace断言通过，失败仍不伪造终态/Generation结束时间 |
| 报告反例6零Attempt推导错误 | 同步受控头发送返回已晚于deadline，实际新建Attempt且调用Provider1次；新增正确Red，在创建Attempt前显式重查期限，复测0调用/0Attempt |
| 原报告的“仅Read”与工具请求记录 | 只有Read成功执行；MCP请求被权限拒绝，单独记录。第二轮用进程级空MCP配置收紧工具，不改用户全局/模型/认证配置 |

隔离副本Red/Green与项目集成分别记录；副本未读取密钥、不改首轮正在审查的项目文件。首轮完成且76份摘要核对后，才落回6份本项文件。原始首轮diff保留；[本轮增量diff](review-diff-r2.patch)共212行，覆盖4份源码与2份测试，原始67份基线重建出的首轮76文件与其审前摘要一致。两项实质问题按本项约定不满足评为执行者P2，不篡改Claude原报告的零发现。


### 3.9 修订后的正式验证（待第2轮独审）

- 项目相关集：164 passed；隔离副本对应协议/执行集116 passed是另一层证据，不与项目结果相加。
- `uv run python -m pytest -q`：1097 passed / 8 live默认跳过，72.91秒；原F1—F4回归与新增7个变体均纳入。
- 最低Python3.12隔离关键集：233 passed，14.85秒，DeprecationWarning视为error；与全量重叠。
- 真实两条再验：2 passed，2.57秒，[原始回执](live-r2-results.xml)，均input_delta=0；累计6次实际F5调用，先前4次保留，不冒充自动重试或首轮。
- 上述r2-full/r2-py312/r2-live的76份输入摘要各自前后一致；原51份受保护工件保持不变。当前待原生Claude Code完整第2轮，未报告整项验收。


### 3.10 最终验收与第2轮审查处置

原生CLI完整第2轮完成：[实际提示词](review-prompt-r2.md)、[原始报告](review.md)、[材料来源索引](review-evidence-r2.md)。进程退出0、is_error=false、stop_reason=end_turn，718.02秒。初始化实际tools=[Read]、mcp_servers=[]，全程50次Read/50唯一文件、48项必读全部实际直接Read，无工具错误；报告自身46/44的统计不准确，以实际日志50/48为准。两个额外读取为errors.py与api/errors.py。本轮重新检查核心执行/传输/协议/绑定/全部新测试，其余未变公共件复用首轮及摘要，读取数量不能代替正确性判断。

报告APPROVED，P0=0/P1=0/P2=0/P3=1。P3为E02缺少“completed携带末段delta”的直接验证；行为已静态确认正确。执行者补integration/test_f5_gateway.py的HTTP参数化两例（completed/failed）：文本先于终态、Usage正确、终态后不继续读取、单流关闭一次，2 passed。只补一份测试文件，未改产品源码/配置/依赖；[审后测试差分](post-review-test-only.patch)留存。按非阻断P3记录处置，不为测试补强开启第三轮完整代码审查。

| 验收项 | 最终证据 |
| --- | --- |
| 全量离线/协议/实际进程 | acceptance-full：1099 passed / 8 live默认跳过，72.81秒 |
| 最低Python3.12关键集 | acceptance-py312：235 passed，15.03秒，DeprecationWarning为error；与全量重叠 |
| 最新真实接入 | r2-live：两例2 passed，input_delta均0；本项累计6次，回执见3.9；审后只补测试，产品代码未改，不重复真实调用 |
| 审查取证 | 第2轮实际Read清单、正常退出、原始报告/提示词/哈希均在evidence.json；全程仅Read，无MCP调用 |
| 输入/历史保护 | 审前后76份输入完全一致；审后仅上述1份测试增加2变体，当前76份输入在最终运行前后一致；产品源码29份与受审版本逐份相同，51份受保护工件未变 |
| 交付闭环 | 代码导读、README实际29文件目录、接口/架构/AGENTS/进度、模板与CLI操作经验同步；各层证据边界保留 |

对报告观察项另做事实核验：裸TimeoutError被标成执行者已复现，但当时直接用例只覆盖OSError；执行者审后追加离线实验，确认裸TimeoutError→internal_error、一次failed Attempt、关闭一次，不冒充审查者重跑。关于Content-Length的“防御性no-op”描述不采纳：实测基类Response产生Content-Length:0，流式Response必须删除它，删除具有实际作用；产品代码不改。原报告保持原文，事实/处置在此注明。

截至2026-11-01，本项已确认范围、全部必验层次及非阻断独审满足，F5验收通过。Mock/协议回环/真实TCP/固定真实样本证明范围各异；无多实例、恢复、主动取消、SQLite、纯模型计算计时或停止供应商计费保证。后续发现本项回归按项目Bug路径处理；F6另按新功能路径讨论。
