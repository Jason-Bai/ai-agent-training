你是 F10 第1轮全新独立审查 Agent，不继承实现对话。项目根目录 /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway。

先读取 /Users/baiyu/workspaces/githubs/ai-agent-training/projects/llm-gateway/docs/features/F10/review-material-r1.md，其中列明用户确认及来源缺口、设计和测试、真实基线/差分/哈希、原始执行回执与必要调用链；再按照项目独立审查提示词模板完成三维审查。实现者的说明和测试数量均待核实，允许质疑设计，独立推导反例，不接受预设通过结论。

只读审查，不修改文件，不运行测试或实验，不调用真实/付费API，不读取真实凭据、.env、.claude、.venv或缓存。需要实验则返回最小复现让执行者运行；通用工具权限未强制只读，不宣称工具层只读。逐文件分段读取，避免巨型输出截断。

报告需求正确性、实现一致性、系统正确性覆盖及未验证项，具体反例和依据，P0-P3发现的文件行/触发/影响/验证建议，以及实际读取与执行者证据来源。最后给 VERDICT: APPROVED 或 BLOCKED 和等级数量。缺关键验收依据不能通过，普通范围外限制不自动当缺陷。本次为首轮，完成后直接返回完整报告，不写文件。