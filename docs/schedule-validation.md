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

## 验证结果

- 本地 302 项测试通过，覆盖每日守卫、空日报、按收件人补发、UTC/本地日期边界、旧状态迁移、pending 阻断及原有完整流程；静态检查与格式检查通过。
- [代码 CI](https://github.com/Monkeypox966/arxiv-jev-digest/actions/runs/40259115268)：Windows / Ubuntu × Python 3.11 / 3.13 四组全部通过。
- 使用官方 actionlint v1.7.12 检查全部工作流，结果通过；下载文件与官方发布的 SHA256 校验值一致。未运行可选 shellcheck / pyflakes。
- [第一次线上每日守卫](https://github.com/Monkeypox966/arxiv-jev-digest/actions/runs/40259122946)：10:26:55 识别出今天已补发，跳过 1 位收件人；0 次 Jev、0 次 DeepSeek、0 封邮件，状态迁移成功。
- [再次线上每日守卫](https://github.com/Monkeypox966/arxiv-jev-digest/actions/runs/40259280033)：10:28:52 重复验证，同样跳过、0 次模型调用、0 封邮件。
- 再读远端生产数据库：quick_check 为 ok，sent_at 列存在，0 pending；已发送论文从 38 增至 43，恰好新增今天的 5 篇；公告检查点推进至 10-26。两次守卫未增加发送记录。

上述线上守卫通过手动 daily 模式执行，与自动任务共用命令和生产状态。新增备用 cron 的未来自动触发尚未实际观察，不能把上述测试解释为已经验证 GitHub 会准点触发。

SMTP 成功表示发件服务器接受，并非独立核验最终收件箱。备用任务仍依赖 GitHub，不能保证准点；严格准点需要独立调度来源。依据：[GitHub schedule 文档](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)、[故障排查文档](https://docs.github.com/en/actions/how-tos/troubleshoot-workflows)。

## 2026-10-27：独立调度实际部署与验证

10-27 北京时间 09:54 再次检查时，仓库工作流 active、默认分支 main，但 GitHub 未创建当日 09:30 或 09:47 的运行。最近一条 schedule 于 04:11 执行，因早于 09:30 正确跳过。这是触发阶段缺失/延迟，不能通过 SMTP 或筛选逻辑解释；GitHub 未公开具体内部原因。

此前添加的每小时备用 cron 与主任务依赖同一平台，因此不足以覆盖这次故障。手动发送和每日守卫通过，也不能据此宣称未来定时触发已验证。

### 当天补发与实际收件

[09:55 补发](https://github.com/Monkeypox966/arxiv-jev-digest/actions/runs/40435375256)：09:55:31 QQ SMTP 接受 1 封日报；随后在 QQ 收件箱实际看到了标题为 `cs.RO 人形机器人日报 · 2026-10-27 · 0 篇` 的邮件。107 篇候选来自仍为 10-26 的公告批次，已处理记录保留，故本次没有新的入选论文；没有为补发重复调用模型。

### 已部署的独立服务器

在用户指定的持续在线 Linux 服务器安装并启用了 `arxiv-digest-scheduler.timer`，使用仓库 [deploy/](../deploy/README.md) 中的代码和 systemd 单元。服务器 Python 3.13，时钟同步状态为 yes。调度独立于本机电脑、Codex 和 GitHub cron。

- 每日北京时间 09:30 开始，此后每 10 分钟检查一次，至 23:50。
- 从 `digest-state/delivery-status.json` 读取与 outbox 同源的聚合 SMTP 确认记录，不以工作流绿色状态代替发送成功。
- 当天全部收件人确认后停止补跑；部分完成、响应丢失、运行索引延迟和 pending 分别处理。
- 服务器仅保存 GitHub 调用凭据，不保存模型或 SMTP Secret。启用开机恢复及 missed-timer 补查。

### 真实时钟测试

| 验证 | 结果 |
| --- | --- |
| 回执初始化 | [10:10 工作流](https://github.com/Monkeypox966/arxiv-jev-digest/actions/runs/40436926941)识别今天已完成，导出正常日报人数 1、已确认 1、pending 0 |
| 独立时钟自动提交 | systemd 单次测试定时器于 10:12:35 自动执行，同一生产脚本使用 `--force-dispatch` 测试入口；10:12:37 创建 [GitHub 运行](https://github.com/Monkeypox966/arxiv-jev-digest/actions/runs/40437096870)，关联 ID 为 `vps-20261027T101235-65d218e4` |
| 不重复发信 | 上述真实工作流使用 `daily` 模式，识别已发送；新增模型调用、输入输出 tokens、邮件均为 0 |
| 正式服务自动核查 | 为正式 timer 临时添加 20 秒测试时刻；10:16:05 由时钟触发正式服务，读取当天完整回执，状态 delivered、退出码 0，没有再创建工作流 |
| 恢复正式时间表 | 清除临时测试时刻、测试单元及其状态文件；正式 timer 保持 enabled / active，服务 ExecStart 不含测试参数，DropInPaths 为空 |
| 明日时间解析 | systemd 日历以 10-28 01:29:59 UTC 为基准，下一次为 01:30 UTC，即北京时间 09:30 |

上述测试实际验证了服务器时钟 → GitHub workflow_dispatch → 同一每日守卫 → 持久化 SMTP 回执链路。10-28 的早晨尚未到来，不能把当前验证写成已观察到次日准点投递。GitHub API、runner、服务器网络和邮箱服务仍是独立故障来源；调度器会按回执按需重查，结果不确定的发送仍需人工核实。

### 回归

本地完整 437 项测试通过。[部署代码 CI](https://github.com/Monkeypox966/arxiv-jev-digest/actions/runs/40436926138)的 Linux / Windows × Python 3.11 / 3.13 四组全部通过，含格式、静态检查、配置和离线演示。新增测试覆盖真实 SMTP 确认口径、空日报、测试邮件排除、部分成功、日期过期、pending、任务占用、HTTP 响应丢失、关联 ID 及工作流绿色但没有回执的情况。
