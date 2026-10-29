# 独立服务器调度

GitHub Actions 的 cron 可能延迟或漏触发。本组件由持续在线的 Linux 服务器每小时调用 GitHub `daily` 模式。服务器只负责发信号；公告是否更新、当天是否已发送及筛选、翻译和 SMTP 都由 Actions 判断和执行。服务器无需模型或邮箱密钥，也无需 Python 第三方依赖。

## 安装

要求 Linux、Python 3.11+、systemd，服务器能够访问 GitHub API。先将主分支更新到包含 `scheduler_id` 输入及发送回执的版本，再把本目录复制到服务器，以 root 执行：

```sh
umask 077
cp scheduler.env.example /etc/arxiv-digest-scheduler.env
# 编辑该文件，填写 OWNER/REPO 和 GitHub token。
sh install.sh
```

推荐使用仅授权目标仓库的 fine-grained Token，权限为 **Actions: Read and write**。服务器脚本直接调用 GitHub API，不需要安装 GitHub CLI，也不读取 Contents 或投递回执。模型和 SMTP 凭据继续保留在 GitHub Secrets。

默认每天北京时间 **10:00–23:00 每小时触发一次**，对应 UTC `02:00–15:00`。当天已发过邮件后，服务器仍按时发信号，Actions 自行跳过。`Persistent=yes` 使服务器重启后补做错过的检查；本机电脑和 Codex 不需要保持运行。

配置中的 `DIGEST_TIMEZONE` / `DAILY_AFTER` 必须与项目时区及 `--daily-after` 一致；修改每日时段或时区时也修改 timer 的 UTC 时间范围。安装脚本可重复执行，用于更新部署文件。

目录中的 `scheduler.py` 是无第三方依赖的触发器，`arxiv-digest-scheduler.service` 和 `.timer` 是 systemd 单次服务及定时器，`status.sh` 用于查看定时器和最近健康状态，`uninstall.sh` 用于卸载。普通卸载保留令牌配置和本地状态；只有显式执行 `sh uninstall.sh --purge` 才会删除它们。

## Actions 判断与触发保护

Actions 恢复发送历史后，先检查当天符合公告日期要求的已发送日报，再核对 `/new` 的公告日期。旧批次、周末没有新公告或没有匹配论文时不发邮件；新批次有匹配论文时才发送。所有空日报均已取消，旧 `send_empty` 选项不再启用空信。

- 每个整点：触发一次 `daily`，不在服务器判断是否已发送。
- 有任务正在执行：等待下次检查，避免不断排队。
- HTTP 响应丢失：先保存关联 ID，等待 GitHub 运行索引，不立即重复提交。
- pending：由 Actions 原有保护阻止不确定投递的自动重发，按[恢复手册](../docs/operations.md)核实后处理。

GitHub 整个平台、服务器网络或 SMTP 故障仍可能使邮件迟到。SMTP 接受确认不代表已独立检查最终收件箱；服务器异常写入下列日志和健康文件，不自动订阅第三方告警服务。

## 查看状态

```sh
systemctl list-timers arxiv-digest-scheduler.timer
systemctl status arxiv-digest-scheduler.service
journalctl -u arxiv-digest-scheduler.service --since today
cat /var/lib/arxiv-digest-scheduler/health.json
# 或
/opt/arxiv-digest-scheduler/status.sh
```

`dispatched` 表示已提交触发请求，不代表已发送邮件；`running` / `awaiting_run_index` / `awaiting_previous_run` 表示正在等待，`not_due` 表示尚未到开始时间。Actions 的 `waiting_for_arxiv_update`、`already_delivered_today`、`no_matching_papers` 均为正常无信状态。单次 oneshot 服务显示 inactive 是正常现象，查看退出状态和 timer 是否 active。

只测试外部触发链路、不额外发日报时，可以从服务器的服务环境运行 `scheduler.py --force-dispatch`：它仍固定使用 `daily` 模式，因此当天已完成的收件人不会重复发信。

暂停独立调度：

```sh
systemctl disable --now arxiv-digest-scheduler.timer
```

这只暂停服务器定时器，仓库原有 cron 继续作为另一条触发来源。
