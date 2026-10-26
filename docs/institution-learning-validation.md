# 机构及简称自动学习验证

验证日期：2026-10-26，Asia/Shanghai。以下为真实 ROR、机构官网、DeepSeek 和 GitHub Actions 联调；未调用 Jev、未发送邮件，不包含访问凭据或个人邮箱。

## 简称对应已有机构

[简称核查运行](https://github.com/Monkeypox966/arxiv-jev-digest/actions/runs/40316400778)确认：`MPI-IS` 的 affiliation 检索未命中，但 ROR 名称查询能唯一匹配登记的 acronym。程序据此对应到 `Max Planck Institute for Intelligent Systems`，继承现有人工分数 80，不需要模型重新评分。

简称与规范名称持久化到 `digest-state` 的 SQLite 及 `institutions.json`。后续运行已从状态分支恢复该对应，而非重新检索或依赖当前进程内存。

## 名单外机构评分与入库

[真实评估运行](https://github.com/Monkeypox966/arxiv-jev-digest/actions/runs/40317825037)使用当前机器人领域配置：

| 输入 | 结果 | 身份来源 | 评分来源 |
| --- | --- | --- | --- |
| MPI-IS | 80，复用人工规则 | 已保存的 ROR 对应 | 当前人工名单 |
| Idiap Research Institute | 80，模型评估并入库 | ROR | 两份 Idiap 官网页面的实际引用 |
| Honda Research Institute Japan | 80，模型评估并入库 | ROR | Haru 项目研究页及 HRI-JP 研究页的实际引用 |

两次 DeepSeek 调用共报告输入 9,051 tokens、输出 827 tokens；未发生 JSON 重试。新增机构的模型置信度均为 0.80，达到当前 0.80 门槛。规范名称、观测别名、分数、身份来源、评分引用和时间均已保存；这两项仍属于模型辅助的阅读偏好评分。

[全新任务重复核查](https://github.com/Monkeypox966/arxiv-jev-digest/actions/runs/40317971254)再次输入这三个名称：均恢复原有 80 分，自动名单仍为 3 个机构；Jev / DeepSeek 调用及输入、输出 tokens 全部为 0。三个机构均直接从已学习名单命中，验证了状态分支持久化和跨运行复用。

核查前后发送记录、版本记录、已处理论文、outbox、公告检查点和两类补查队列的完整内容摘要一致；数据库完整性为 `ok`，pending 为 0。本次测试没有新增邮件或推进论文历史。

联调发现将细分兴趣传给机构评分会导致模型重复判断论文相关性。现已只提供 arXiv 研究分类，按具体研究成果判断机构实力；论文相关性仍由前一步 Jev 筛选。没有通过降低置信度门槛使样本入库。

## 未核实结果

Tampere University 的官网在本次有限抓取中缺少足够的具体机器人研究证据，因此保留中性意见，未写入确定名单。此前对 Oregon State University、Worcester Polytechnic Institute 的核查也因置信度不足而未入库；不能据此判定这些机构质量低。这些结果按短期缓存期限重查。

模型明确表示信息不足时不再为此连续重试；格式错误、不实引用、预算和权限错误仍按原有规则处理。

## 自动化检查

[代码 CI](https://github.com/Monkeypox966/arxiv-jev-digest/actions/runs/40317659631)：Linux / Windows × Python 3.11 / 3.13 四组均通过 422 项测试，同时通过格式、静态检查、配置检查和离线演示。

离线测试还覆盖模型提出简称后用官网原文确认、未知机构评分持久化、60 天缓存清理后保留、别名冲突隔离、人工改分优先、研究范围隔离、关闭学习、状态分支往返恢复及发送历史保持。模型身份确认路径使用明确标注的构造样本，不冒充真实 API 联调。
