# 2026-10-26 定时投递排查

所有时间均为北京时间（Asia/Shanghai）。

## 证据

| 检查 | 结果 |
| --- | --- |
| 主 cron | `30 1 * * *`，对应 09:30；配置在默认分支 main |
| 工作流和仓库 | active；未归档、未禁用；Actions 允许执行、contents 写权限有效 |
| 定时 actor | Monkeypox966；账号可访问且 gh 已认证，前一天定时任务也由该账号触发 |
| [10-25 定时运行](https://github.com/Monkeypox966/arxiv-jev-digest/actions/runs/40169797988) | 14:38:19 创建，14:38:22 runner 开始，14:38:37 SMTP 成功；0 模型调用、1 封空日报 |
| 10-26 09:30 定时运行 | 截至 10:14 未生成；也没有当日排队或正在运行的定时任务 |
| 持久化数据库 | quick_check 为 ok；无 pending；保留此前 38 篇已发送论文；检查点 10-23 |
| [10-26 手动补跑](https://github.com/Monkeypox966/arxiv-jev-digest/actions/runs/40258272560) | 10:16:17 创建，约 4 秒后 runner 启动；10:17:39 完成发送，64 篇新增候选、5 篇精选、1 封邮件 |
| 本次模型调用 | Jev 64 次，DeepSeek 15 次（包含元数据和摘要） |

10-25 的定时事件生成比设定时间晚 5 小时 8 分钟，而 runner 排队仅约 3 秒。10-26 手动链路也正常，因此可见故障发生在 GitHub 定时事件生成阶段，而非此次的模型、状态阻塞或 SMTP 认证失败。GitHub 未公开该仓库事件延迟的内部原因，不能据此断定一定是平台负载。官方状态页在排查时未报告 Actions 全局故障，这也不能排除个别定时事件延迟。

此前仅说明“今天未触发”不够完整：10-25 的迟到自动任务实际上又发了一封空日报，说明还需要每日投递去重。

## 修复

- 保留 09:30 主任务，增加 09:47–17:47 每小时一次备用检查。
- 自动任务在读取状态后按本地日期和收件人检查当天是否已有确认日报；有则不发信、不抓取、不调用模型。手动正常发送也计入；独立 SMTP 测试不计入。
- 多人部分完成只补未完成的收件人；pending 继续阻断。
- 同一事务记录 SMTP 确认时间；兼容旧数据库，未删历史。
- Summary 记录每日目标时间、处理开始时的延迟、跳过人数和原因；手动 daily 模式便于验证。

本地 302 项测试通过，覆盖每日守卫、空日报、按收件人补发、UTC/本地日期边界、旧状态迁移、pending 阻断及原有完整流程；静态检查与格式检查通过。线上每日守卫验证在本次修复部署后执行，结果将补充至此。

SMTP 成功表示发件服务器接受，并非独立核验最终收件箱。备用任务仍依赖 GitHub，不能保证准点；严格准点需要独立调度来源。依据：[GitHub schedule 文档](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)、[故障排查文档](https://docs.github.com/en/actions/how-tos/troubleshoot-workflows)。
