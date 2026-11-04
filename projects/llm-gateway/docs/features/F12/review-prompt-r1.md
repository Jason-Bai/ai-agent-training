你是F12的独立审查者，进行第1轮完整审查（最多三轮），不继承实现对话。目标是发现遗漏并独立判断需求正确性、实现一致性及系统正确性；设计、测试、导读和执行证据均须核实，不预设通过结论。

项目根：/Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway
首先阅读项目AGENTS.md、docs/AGENTS.md及docs/templates/独立审查提示词模板.md，按模板输出报告。

事项范围：F12模型管理与模型组路由。用户要求参考LiteLLM，模型组与任务自动路由两个都做、依次交付；本项仅前者，F13任务自动路由另行确认。F12保持必填model逻辑组入口，三层YAML（providers/endpoints/model_groups），按有序候选进行实际请求能力、输出预算、逐端点Prompt及上下文预算筛选；F7恢复、F11并发、F9逐实际Attempt计费、F8诊断、普通/结构化/流式/续传/取消/探测共同闭环。部署限定单实例单worker、内部Agent、本机或隔离网络。无动态管理API、语义分类、自动熔断或多实例承诺。排除项不能掩盖当前承诺缺陷。

需求来源、用户原文摘录与整理的区别、确认记录缺口、完整材料索引见docs/features/F12/review-material-r1.md。继续读同目录design.md、test-plan.md、implementation-plan.md及implementation-summary.md；可以质疑设计或测试是否共同误解用户需求。没有完整聊天导出，不补造确认原文。

固定基线：本项目没有Git。docs/features/F12/evidence-review-baseline-r1.tar.gz是真实实施前358份安全文件及摘要；evidence-review-changes-r1.json和.diff是实际逐内容差分；evidence-review-source-manifest-r1.json固定本轮候选源码/测试/安全示例/文档。原全量版本212文件摘要见evidence-full-source-manifest.json。新增文件直接读当前源码，旧文件结合真实差分及必要上下文；不要把文件数量或摘要相同当作正确性证明。执行者会核对审查前后源码摘要。

按真实差分覆盖全部变更和必要影响链：Registry/Router、Gateway/F7恢复、StreamingManager/执行器、F11准入与资源释放、F9价格/预留/结算、Store/Trace、Provider工厂/API、配置及测试迁移。重点核对逐候选消息和计数、阶段错误、恢复边界、持久化计划及配置变化、历史pending/Trace兼容、跨端点费用和并发请求隔离。不要只检查新增测试；tests/registry_helpers.py与历史断言迁移、真实共用步骤和upstream_observation.py取证变化也属审查范围。

原始运行证据由执行者产生：evidence-full.json/.log/.xml.gz记录完整2497通过（42live）、0失败/错误/跳过/筛除，退出码0；evidence-real-routing.log/.xml.gz为新增真实8例独立运行，最终全量再次执行；evidence-capacity-summary.json及evidence-capacity.json.gz记录15886笔真实请求、四档及20并发持续600秒。evidence-node-mapping.json、evidence-test-coverage.json为原2265节点保留及T01—T28映射；开发Red和中间失败保存在evidence-development.tar.gz；原始安全观察工件及摘要见evidence-raw.tar.gz、evidence-raw-manifest.json。逐项抽查，避免巨型输出截断。两条真实协议共用DeepSeek，不是独立供应商或生产证明。不得把执行者结果描述成审查者重跑。

只审查、不写文件、不运行测试/产品/实验或真实/付费调用；允许只读命令、脚本核对哈希和解析已有JSON/XML/压缩证据，不导入执行产品。通用工具技术上仍可写入，当前只读是任务指令，不宣称工具强制限制。禁止读取实际config/gateway.yaml、credentials.yaml、postgres.password、.env及其测试临时副本、.venv或缓存；配置仅读明确安全示例。运行配置迁移只提供evidence-runtime-config-migration.json，不读取私有备份。AGENTS不修改。需要实验时给执行者最小输入/操作/预期/观察项，由其运行并回传原始证据后复核。

请主动推导已有测试之外的组合反例和故障时序。按模板报告三维覆盖、反例检查、每项问题的严重性/文件行/触发条件/影响/违反契约/最小验证及修正建议、已读与未读材料和证据来源。区分静态确定、执行者已复现及待验证风险，不凑问题数量；P0—P2或关键证据缺口阻断。最后明确VERDICT: APPROVED或BLOCKED及P0/P1/P2/P3数量。报告直接返回，不写入文件；执行者将原样归档。
