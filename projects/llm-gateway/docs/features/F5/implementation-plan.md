# F5 Streaming Implementation Plan

> **For agentic workers:** 使用 writing-plans / subagent-driven-development / test-driven-development；项目不依赖 Git，不创建分支/提交。设计与测试方案均由用户于 2026-11-01 确认，后续无需例行开始确认。

**Goal:** 实现受控请求到真实增量 SSE 的单次调用闭环，并以已确认51组场景、真实两例与原生Claude Code独审验收。

**Architecture:** Gateway 完成预检，StreamingExecution 通过可等待的start/send出口执行；API负责ASGI及SSE编码。Adapter返回自身统一事件和远端终态来源标记，单次流由执行者关闭，共享客户端归lifespan。

**Tech Stack:** 现有锁文件 Python≥3.12/FastAPI/Starlette/asyncio/httpx2/OpenAI SDK/pytest，不增加产品依赖。

## Chunk 1：契约、配置与Provider

### Task 1：严格契约与能力绑定

文件：contracts.py、providers/base.py、services/models.py、providers/factory.py、constants.py、config/gateway.yaml；tests/unit/test_f5_contracts.py；现有静态测试配置做必要字段迁移。

- [x] 保存源码/测试/配置基线、历史/本机配置保护摘要，运行 `uv run python -m pytest -q`，预期旧全量成功、live默认跳过，保存原始输出。
- [x] 先测流式类型不存在/支持字段边界、严格supports_streaming、事件字段组合和注入能力。避免导入失败作为Red：动态取类型并assert存在，然后再检查行为。
- [x] 运行新测试确认目标失败；实现独立StreamRequest、ProviderStreamEvent和启动校验。stream为可aclose的异步迭代器，status/error_code作为唯一执行决策，upstream_terminal_received仅计时。
- [x] 所有默认模型显式supports_streaming=true；静态fixture/旧专用替身最小迁移，不放宽旧期望。暂时不在startup调用远端探测。
- [x] 再运行新测试与受影响旧配置/生命周期测试，预期通过。共同断言：Schema不在stream OpenAPI请求中，字段出现即422，正常complete类型不变。

### Task 2：两个真实Adapter和脚本Mock

文件：providers/deepseek.py、openai_compatible.py、mock.py；新增providers/streaming.py隔离仅Provider使用的分帧/两种协议状态；tests/protocol/test_f5_providers.py。

- [x] 阅读锁定SDK真实流式方法，复核官方协议；先写回环SSE测试，确认stream方法缺失或字节/事件不符的Red。
- [x] DeepSeek采用真实httpx2流；OpenAICompatible采用真实SDK流式传输及原始dict/字节解析。禁止完整read后切块，保持retry=0、无环境代理/重定向。
- [x] Chat逐段content并等待stop+[DONE]才成功；Responses逐段output_text.delta，完整completed才成功，不重复output_text.done/最终整段文本。网络/协议故障只产failed，预期异常不泄漏第二套返回协议。
- [x] Usage严格校验、未知null；映射拒答/截断/过滤/HTTP故障，保留受控元信息。明确远端failed与本地故障合成failed的来源标记。
- [x] Mock走相同契约，可脚本控制事件/等待/异常，分别记录调用、next、单流close、共享client close。默认普通调用仍MOCK_REPLY。
- [x] 执行W01—W10参数化协议测试，检查实际出站JSON、真实SDK读取、字节分片、关闭与零隐式重试；运行旧协议回归。

## Chunk 2：预检、执行与HTTP

### Task 3：单次流执行与Trace

文件：services/gateway.py、services/streaming.py、services/trace.py、errors.py；tests/unit/test_f5_execution.py、tests/integration/test_f5_gateway.py。

- [x] 先写不存在路由/执行类型的Red及最小预检零调用断言，正常路径不能通过完整complete代替。
- [x] Gateway建立PreparedStream，沿用模板装配/指纹→模型→能力→预算/超时→计数顺序。仅提取实际共享小步骤，不重写F4普通调用。
- [x] Trace加入streaming用途、B/首段写出/上游首段及远端终态时间点、总执行耗时与provider_error_code；未知null。记录成功必须completed实际send完成，Middleware不重复收口。
- [x] 执行器在B设deadline，worker统一start/读取/发送，另监听disconnect；首段前也能心跳，最多一个pending anext，send完成再读下一条，无完整回答缓冲。
- [x] 对实际send等待施加相同deadline；发过终态不补第二个。Provider failed发送中又超时/断连则最终执行码为request_timeout/client_disconnected，保留provider_error_code。
- [x] 统一取消挂起读取、结束发送任务、关闭本次流，清理共享1秒宽限；不关共享Provider。仅在没有挂起发送且不额外分配发送预算时尽力发失败终态。
- [x] 逐组先运行T/E目标失败，再最小实现并运行相同用例；用屏障和受控计时验证，不加公共故障开关。关键断言：首段上游到达2/写出3、远端结束7、最终8，得到TTFT3000/Generation5000/总8000。

### Task 4：ASGI/SSE边界

文件：api/routes.py、api/sse.py、middleware/request_context.py；复用依赖注入；tests/smoke/test_f5_http_server.py。

- [x] 路由Red：当前404应变成受控请求拒绝或SSE成功，非法Schema/stream额外字段零调用。
- [x] 自定义最小Response将start、编码后真实ASGI send与disconnect监听交给执行器；不使用yield冒充发送确认，不引入流式组件库。
- [x] 返回text/event-stream/UTF-8与X-Request-ID；四类event/data精确匹配，JSON转义正文换行；无id/retry/started/自动重连。
- [x] 真实单worker进程：Provider首段后等待release，HTTP客户端收到首段才释放；心跳、首段前后断连、流失败、并发A断连B成功。外层watchdog与finally关闭进程/连接。
- [x] ASGI send分别阻塞头/文本/heartbeat/completed/failed，验证同一deadline与资源回收。真实客户端不读不替代确定性阻塞实验。

## Chunk 3：验收与闭环

### Task 5：回归、真实接入、文档和审查

- [x] 根据test-plan逐条映射C/E/T/W/R/L的参数化用例与原始证据，检查没有用测试替身替代被测执行/编码/SDK。
- [x] `uv run python -m pytest -q`，旧全量及新增全部离线通过，live只默认跳过；隔离Python3.12关键集启用DeprecationWarning为error。
- [x] `uv run python -m pytest tests/live/test_f5_streaming.py --run-live -x -q -o junit_family=xunit1 --junitxml=/tmp/llm-gateway-f5-live-results.xml`；默认配置不切真实；本机既有credentials仅实际live阶段读取。两例失败不静默跳过，失败与复测分别归档。
- [x] 产出implementation-summary.md，更新README实际目录/调用方法、contracts、architecture、progress、AGENTS、设计/测试实际状态；模板只补实际暴露的通用缺口。
- [x] 保存完整变更diff、原始执行回执、安全元信息与受保护摘要，执行前归档原生Claude Code实际review-prompt-rN.md；只Read、三维检查，最多三轮完整审查。缺证据不称通过。
- [x] 核实发现并原场景复测，最终非阻断审查后检查源码/测试/默认配置摘要未变，报告实际验收结果与证明范围。

## 计划自检与执行记录

计划只在本事项目录维护；新功能已获用户授权，不因技能默认handoff再设置开始确认。计划文档检查不替代原生Claude Code代码审查。每个切片记录对应Red/Green原始运行，已执行框后续逐项勾选。

2026-11-01计划只读检查：三个Chunk均Approved；使用独立子任务完成契约/适配器/网络测试，执行者集成并核对切片需求与质量，原生Claude独审另行执行。跨目录同名测试引起pytest收集冲突后改为职责名；该收集错误不计Red。

最终全部步骤已完成，结果与审后P3测试补强见test-plan.md 3.10；无Git操作，不追加例行用户验收节点。
