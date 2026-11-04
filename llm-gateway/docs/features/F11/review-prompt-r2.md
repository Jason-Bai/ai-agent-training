继续F11第2轮完整独立复核。你是同一R1审查者，可以复用已核实且摘要未变的覆盖，但请独立判断最终三维正确性与验收证据，不预设APPROVED或BLOCKED。

项目根：/Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway
轮次：F11 R2，总计最多三轮，不因更改口径或工具重置。
范围：全局/Agent两层请求名额、内部恢复持有原名额、本地执行及清理真正停止才释放；单实例单worker，内部Agent/本机或隔离网络。不新增上游组、队列、熔断、Router、多实例或额外恢复机制。

用户新决定：在执行者建议“按Gateway正确性验收、供应商成功率单独报告；未归因、漏读漏发、错误终态/资源/账务仍阻断”后，用户原文回复“确认，开始吧”。真实确认材料与原方案都在docs/features/F11/review-material-r2.md及diagnosis-r2-20261103.md。请核对原文与边界，不把实现者的归因或通过数量当结论。

只审查、不修改、不运行测试/实验或真实付费API。不读取Key、.env、.venv、缓存、实际config/gateway.yaml或config/prices.yaml；配置只读安全示例和已脱敏/测试快照。允许只读解析源码、diff、JSON/XML、tar/gzip及计算摘要。通用工具的只读是任务约束，不宣称技术强制只读。需要实验请提出最小操作，由root执行，不能自己扩大付费调用。

固定基线与差分：
1. R1真实重建基线 /var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/llmgw-f11-diagnosis-r2-nz404qhe/review-r1-reconstructed ，由原实施前安全base+当时R1归档差分重建，211项与R1摘要相符；重建回执同级review-r1-reconstruction.json。
2. 本轮冻结 /var/folders/1x/3lv86fj56zjgm8txl21tvpq00000gn/T/llmgw-f11-mb5906h7/review-r2-final/manifest.json ，R1到本轮真差分r1-to-r2.diff及清单r1-to-r2.json同目录，累计对实施前基线的changes.diff也在该目录。请独立核对差分/摘要/遗漏。实际运行配置两项由root核对，不要求你打开。审查前后核对允许读的候选项。
3. 原R1报告、提示词、材料不改写。旧artifact manifest只有四份已授权活动文档变化，原始报告等归档一致；293份历史保护文件无变化。产品src、迁移、配置、两份AGENTS相对R1未改；请核对而非相信声明。

先读AGENTS.md、docs/AGENTS.md、独立审查模板及review-material-r2.md，再按当前变化核对design/test-plan/implementation-summary与实际源码和证据。重点是tests/upstream_observation.py、tests/upstream_availability.py、两份新单元测试，F10 bootstrap/Environment、metrics、capacity及两份旧live fixture留证。原产品调用链按R1覆盖/摘要复用并复查受影响边界。特别独立推导错误豁免、读取取消/关闭、遗漏完整终态/正文、跨请求/Attempt、未知Usage/账务、观察器改变响应/时序的反例。

运行证据均由root/实现任务生成，你未重跑：
- evidence-full-r2.json、xml.gz、log及collection-r2.log.gz：冻结217项，完整2265节点，2242 passed/23 failed，34 live、全部2231非live通过，0错误/跳过，1953.20秒。原始23个失败保留，没有改成全pytest通过。
- evidence-capacity-r2.json.gz：原1/5/10/20档各60秒及1并发600秒均执行。持续12/50成功，25个provider_unavailable及13个request_timeout；不称20并发持续通过。原SQL/事件/费用/账户恒等式/阶段排空检查执行。
- evidence-full-r2-raw.tar.gz：1115份HTTP/正文/typed/故障快照/资源等；额外只读访问本轮临时测试DB，保存74/75容量失败完整Trace及SQL，采集连接只在活跃期短暂存在，最终连接数符合原门槛。不是生产无观察器性能结论。
- evidence-capacity-r2-audit.json、evidence-scenes-r2-audit.json、evidence-r2-supplement.tar.gz内数据复核脚本：自动判定器本轮故意较窄，普通body读取等待、混合恢复及探测需单列复核；不要因脚本写classified就预设正确。最后一条容量混合恢复只有原始HTTP/SSE及原报告SQL/费用摘要，没有完整逐字段Trace，来源差异明确、不补造。
- 原6个F11成功场景在这轮被供应商故障阻断，SQL未全留证；两条真实probe恢复后，仅有界补验一次同6场景，evidence-supplement-r2.xml.gz/log/json实际6 passed/56.82秒。项目源码配置未改，临时插件只读追加最终Trace/SQL/账务/余额/名额；原场景所有断言执行。旧失败不反写，不把两个批次数量相加当全量。
- 一份旧probe快照是在后台补写前的running/reserved，没有据此说最终已failed。独立有界Mock延迟11秒复现原10秒probe期限，观察running后约11.03秒failed、退出reserved及余额一致；两条真实probe另completed，受控/真实分开。请判断是否仍有影响验收的缺口，不以新请求改写旧快照。
- evidence-r2-diagnosis.tar.gz含有效Red/Green及原始流停顿/自然503重复两次现场；evidence-transport-send-r2.txt是root读取的已安装HTTP客户端send异常关闭实现，不需访问.venv。

最终按项目模板给三维覆盖、独立反例、P0-P3 Findings、实读/未验证范围和VERDICT。区分已确认缺陷、证据缺口、范围外限制；可质疑方案/测试一起理解错，也允许无发现。若缺关键证据不能APPROVED；若明确用户新口径、功能与系统边界已满足，也不要把供应商调用未全成功本身当Gateway缺陷。不要凑问题或为未来模型管理/路由增加要求。完成后直接回传报告，不写文件。