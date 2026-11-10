# 验收记录

本目录保存可复核的运行结果。合成验收数据带有明确标记，不作为真实企业专家效果报告；记录不包含API Key和JWT。

| 记录 | 内容 |
|---|---|
| [多步子调查与官方GEPA验证](20261111-gepa-investigation-verification.md) | 连续追查、局部失败、人工恢复、官方搜索、逐题重复对照及发布回退 |
| [调查流程与文档一致性复核](20261111-coherence-review.md) | 证据时效、历史重放、子调查缺口传递及文档业务顺序 |
| [气象与空间工具验证](20261110-site-context-verification.md) | 位置版本、QWeather、高德和ArcGIS只读工具及当前配置边界 |
| [调查与提示词优化验证](20261110-agent-extensions-verification.md) | 技能、动态调查、人工补证、MCP Tasks与反思式提示词搜索 |
| [首版整理验证](20261110-v1-verification.md) | MCP补充材料、验收协作、迁移合并与代码文档整理 |
| [图谱验收](20261110-business-graph-verification.md) | 业务证据链、知识变更复核、重复异常、实测历史与自动同步 |
| [图谱HTTP运行记录](20261110-business-graph-live.json) | 真实API、MCP和Beat/Worker运行 |
| [一致性检查](20261109-coherence-review.md) | 模块分层、共享附件校验、任务入口和部署源码一致性 |
| [历史北京Qwen运行](20261106-real-acceptance.json) | 北京入口连接超时的实际任务结果 |

其他按日期命名的JSON、Markdown与XML保留原始结果。历史迁移编号、服务数量和测试统计对应记录当时的代码，不能替代当前版本的验收。

新建环境执行`python scripts/manage_stack.py up`。本机真实模型联调使用`python scripts/acceptance.py --development-login`；会产生模型调用。图谱及人工业务验证使用`python -m scripts.accept_business_graph`，使用人工合成夹具，不调用文字模型。
