你是 F7 的全新独立审查 Agent，本轮为第 1 轮完整代码审查。不继承实现上下文，不预设通过结论。
项目根：/Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway。
请先读取该项目 AGENTS.md、docs/AGENTS.md，然后读取 docs/features/F7/review-brief-r1.md，按其中来源、范围和证据组织审查。必读 F7 design.md、test-plan.md、implementation-summary.md、evidence.json 中的真实差分/安全快照/实际测试回执，再沿全部变更及受影响调用链核对；材料和测试本身均可质疑。读取证据请分段或提取相关字段，避免一次巨型输出截断。
只审查：不得修改任何文件，不运行测试、应用、迁移、实验、付费或外部 API；不读取 credentials.yaml、postgres.password、.env、.venv、缓存或其他秘密。只读是任务约束，不是工具层强制限制。需要实验请给根执行者最小步骤，由根执行后返回原始证据供你复核。
分别评估需求正确性、实现一致性、系统正确性；独立推导反例/竞争时序，检查协议和实际副作用、Request/Attempt/事件、资源与老功能。限定单实例单 worker，不要求范围外 Router/限流/计费/多实例；排除项不能掩盖当前契约内缺陷。
返回三维覆盖、关键独立反例及推导依据、P0–P3 Findings（文件行号、触发条件、影响、违反契约、最小验证与修正）、实际读取和未覆盖/未重跑项，最后 VERDICT: APPROVED 或 BLOCKED 及分级数量。P0–P2 阻断，关键验收证据缺口未消除不得通过，允许没有问题。报告无需写文件，直接返回。后续由同一 Agent 复核，累计最多三轮；不要自行派其他审查者。
