# 0.2.0 可靠性验证记录

核查日期：北京时间 2026-10-25。代码、配置与工作流均已推送到公开仓库，密钥继续只存 GitHub Secrets。

## 自动测试

[代码 CI](https://github.com/Monkeypox966/arxiv-jev-digest/actions/runs/40113864811)：268 项测试在 Ubuntu / Windows、Python 3.11 / 3.13 四个组合全部通过；静态检查与格式检查通过，0.2.0 wheel 已验证包含模板、机构名单和运维/预算模块。

新增测试涵盖 v1→v2 SQLite 迁移、pending 保留、事务故障整体回滚、公告检查点、跨月/多分类补抓、短缓存、延迟重试、预算耗尽续跑、认证故障传播、来源身份与歧义、PDF 标号、外链限制，以及 SMTP 前持久化失败阻断。

## 真实 API 与模型

[最终三篇元数据诊断](https://github.com/Monkeypox966/arxiv-jev-digest/actions/runs/40113865367) 使用 GitHub Secrets 调用真实 DeepSeek，未调用 JEV，未发送邮件。产生 11 次元数据 HTTP 尝试，服务商报告输入 24,426 tokens、输出 1,788 tokens；本轮用量完整，未触及预算。这里只记录用量，不据此推算美元费用。

| 样例 | 项目链接 | 核查结果 |
| --- | --- | --- |
| LaTER 2605.07315v1 | [代码仓库](https://github.com/TioeAre/LaTER)，来源为 abs 摘要 | 9/10 位作者有明确机构对应；Yuchen Liu 无明确 PDF 标号，保持未知，没有沿用 HTML 合并姓名块的猜测 |
| BeyondMimic 2508.08241v4 | [项目网站](https://beyondmimic.github.io/)，来源为 abs Comments | PDF 的 Berkeley 4 人、Stanford 3 人，共 7/7 对应通过核查 |
| Humanoid-GPT 2606.03985v1 | [项目网站](https://qizekun.github.io/Humanoid-GPT/)，来源为正文 HTML | PDF 对应覆盖 13/13；Li Yi 为清华 + Shanghai Qi Zhi，Xuchuan Chen 为清华 + Galbot，Sikai Liang 为 Galbot + 上海交大，没有覆盖成项目页的不同署名 |

真实测试曾发现 HTML 合并作者导致误配，以及模型漏填 PDF 中清楚的数字标号。已用实际形状加入回归：歧义 HTML 机构不采纳；已验证机构的明确数字关系由保守代码核对；各来源模型诊断仅保留在 JSON，不混入邮件中的最终说明。核查覆盖率不代表模型能穷尽任意论文的全部机构。

[正式预览](https://github.com/Monkeypox966/arxiv-jev-digest/actions/runs/40111987484) 抓到最新 cs.RO 2026-10-23 批次 69 篇。已发送的 8 篇机器人论文按历史跳过，剩余 61 篇用更新后的任务判据实际调用 JEV；没有新增达标项，故没有生成摘要或发送日报。服务报告 JEV 输入 73,366 tokens、输出 8,418 tokens。

## 真实邮件与状态恢复

[投递联调](https://github.com/Monkeypox966/arxiv-jev-digest/actions/runs/40112649588) 经 QQ SMTP 实际发送 1 封标题明确的测试邮件，服务器确认接受。使用与日报相同的远端 pending 前置保存和确认流程；0 次模型调用，真实论文 sent/reviewed 历史不变。服务器接受不等于独立确认进入收件箱。

[重复正式运行](https://github.com/Monkeypox966/arxiv-jev-digest/actions/runs/40113058953) 成功：仍抓取 69 篇，0 篇新候选、0 次模型调用、0 封邮件。数据库保持原有 38 篇发送记录（前期 30 篇 + 8 篇机器人论文），无待确认记录；公告检查点为 2026-10-23。

中间一次任务在业务处理、状态保存和 Artifact 上传完成后，GitHub 详情接口持续返回 502、运行未收尾。先核实远端 SQLite blob 与已下载备份完全一致，再取消该任务并复跑；上述成功运行证明发送历史与检查点没有丢失，也没有重发。

## 保留的边界

- 公告精确补抓仅覆盖 arXiv recent 可验证历史，超界明确失败；API 手动恢复使用提交日期，不能冒充完整公告历史。
- 扫描图片、纯动态/登录网页、无法辨认的上标或附录仍可能缺资料；保持缺失状态并按配置有限补查。
- API/SMTP/GitHub 故障无法被软件消除。预算与状态保护、异常记录、备份、可选通知和人工 resolve 用于控制损失、减少重复，而非承诺永不失败。

每天现为北京时间 11:00 启动。恢复、诊断及配置方法见[操作手册](operations.md)。
