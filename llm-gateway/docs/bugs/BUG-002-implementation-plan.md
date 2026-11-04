# BUG-002 实施步骤

修复与9组测试方案已于2026-11-03获用户明确确认，见[方案](BUG-002-offline-config-isolation.md)。不使用Git/分支/提交，不修改产品源码、AGENTS或维护者运行配置。

| 步骤 | 负责人及文件 | 验证与依赖 |
| --- | --- | --- |
| 1. 安全基线、正确Red | root：隔离副本、源码/测试/安全配置与历史工件摘要 | 真实Provider定义、无Key的隔离副本中原默认Mock断言正确失败；已取得1 failed/0.15秒。 |
| 2. 配置消费方隔离 | bug002_config_consumers：test_f3_config/test_core及三份协议消费方，必要测试helper/配置回归 | 自建注册表/临时YAML，保留真实解析；可独立纯逻辑测试，PG/进程统一交root。 |
| 3. 公共API验收 | bug002_common_acceptance：f10_support、F10真实公共场景wrapper及新公共场景/Mock wrapper、观察反例 | 五类场景同一组步骤/行为断言，Mock/真实专属证据由夹具承担；F10容量始终真实，原断言不减。 |
| 4. 守卫、矩阵与集成 | root：总conftest、test_bug002_isolation等必要反例，所有接线及文档 | 守卫自身Red/Green、Mock/真实绑定/拒读三个配置变体、缺Key与live启用边界；步骤2/3文件边界互不覆盖。 |
| 5. 统一验证 | root | 原副本同场景Green、相关回归、先Mock再真实公共验收，稳定后最终全部--run-live（含真实容量），保留xunit1参数。数据库故障/容量/全量串行。 |
| 6. 导读与独审 | root冻结基线/实际差分，fresh reviewer只读 | 源码/配置/AGENTS/历史工件摘要核对、证据覆盖表、完整三维独审最多三轮，非阻断后才验收。 |

并行只覆盖独立文件编写与无数据库纯逻辑验证。公共夹具接口由root与负责人明确后接入，不并行执行PG、进程、真实API或容量实验；不使用并行评估重复审查同一对象。新需求或必须修改产品行为时回到需求节点。
