# 工具治理作业

退款与转账示例，包含参数校验、权限判断、人工确认、JSON 审批存储、审批执行、脱敏及审计。

## 运行

使用 Python 3.11+，macOS/Linux（文件锁依赖 fcntl）。

```bash
cd week2/homework
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python tool_governance_demo.py --help
python tool_governance_demo.py tenant_a u_100 refund-submit
python tool_governance_demo.py tenant_a u_200 approvals-list
python tool_governance_demo.py tenant_a u_200 approval-decide --request-id apr_替换为提交返回的ID --decision approve
python tool_governance_demo.py tenant_a u_200 approval-self
python -m pytest -q
```

确认提示输入 y。u_100 为发起人，u_200 为审批人；approval-self 模拟历史发起人转为审批人，验证禁止自审。

审批状态写入运行时自动创建的 store/approvals，不提交 Git。模拟余额和业务记录仅在当前进程内保留。设计文档位于 docs，历史设计与当前行为有差异时以代码及测试为准。
