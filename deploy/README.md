# 独立服务器调度

GitHub Actions 的 cron 可能延迟或漏触发。本组件由持续在线的 Linux 服务器定时调用 GitHub `daily` 模式，筛选、摘要和 SMTP 仍在 Actions 中运行。服务器不需要模型或邮箱密钥，且不需要安装 Python 第三方依赖。

## 安装

要求 Linux、Python 3.11+、systemd，服务器能够访问 GitHub API。先将主分支更新到包含 `scheduler_id` 输入及发送回执的版本，再把本目录复制到服务器，以 root 执行：

```sh
umask 077
cp scheduler.env.example /etc/arxiv-digest-scheduler.env
# 编辑该文件，填写 OWNER/REPO 和 GitHub token。
sh install.sh
```

令牌需有目标仓库的 **Actions: Read and write**、**Contents: Read** 权限；推荐仅授权这一个仓库。服务器脚本直接调用 GitHub API，不需要安装 GitHub CLI；如果使用 GitHub CLI 创建或查看令牌，可运行 `gh auth login` 后用 `gh auth refresh -s actions:write,contents:read`，再将 `gh auth token` 的结果写入环境文件。模型和 SMTP 凭据继续保留在 GitHub Secrets。

默认北京时间 11:00 触发，此后每 10 分钟检查一次，至 23:50。定时器使用明确 UTC 时间；10:00、10:10、10:20 的检查会由程序直接跳过。`Persistent=yes` 使服务器重启后补做错过的检查，服务随系统启动；本机电脑和 Codex 不需要保持运行。

配置中的 `DIGEST_TIMEZONE` / `DAILY_AFTER` 必须与项目时区及 `--daily-after` 一致；修改每日时段或时区时也修改 timer 的 UTC 时间范围。安装脚本可重复执行，用于更新部署文件。

目录中的 `scheduler.py` 是无第三方依赖的触发器，`arxiv-digest-scheduler.service` 和 `.timer` 是 systemd 单次服务及定时器，`status.sh` 用于查看定时器和最近健康状态，`uninstall.sh` 用于卸载。普通卸载保留令牌配置和本地状态；只有显式执行 `sh uninstall.sh --purge` 才会删除它们。

## 完成条件与重试

程序读取 `digest-state` 分支中的 `delivery-status.json`，只有**当天全部收件人的正常日报（含空日报）获得 SMTP 接受确认**才停止补跑；独立连通性测试不计入。该回执只含日期、时区、人数和状态，不含邮箱地址、论文或凭据。

- 已完成：只检查小型回执，不启动 Actions 或调用模型。
- 有任务正在执行：等待下次检查，避免不断排队。
- 缺少回执、日期过期或仅部分成功：调用 `daily`，由原有每日守卫只处理未完成的收件人。
- HTTP 响应丢失：先保存关联 ID，等待 GitHub 运行索引，不立即重复提交。
- 工作流成功但没有发送确认：继续检查，不能把绿色工作流当成投递。
- 任务已结束却仍有 pending：停止自动重发并报错，按[恢复手册](../docs/operations.md)核实邮箱后处理。

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

`delivered` 表示回执确认完成；`dispatched` 仅表示已提交；`running` / `awaiting_run_index` / `awaiting_receipt` 尚未完成；`error` 需要排查，定时器仍会在下次时刻重新检查。单次执行后 oneshot 服务显示 inactive 是正常现象，查看退出状态和 timer 是否 active。

只测试外部触发链路、不额外发日报时，可以从服务器的服务环境运行 `scheduler.py --force-dispatch`：它仍固定使用 `daily` 模式，因此当天已完成的收件人不会重复发信。

暂停独立调度：

```sh
systemctl disable --now arxiv-digest-scheduler.timer
```

这只暂停服务器定时器，仓库原有 cron 继续作为另一条触发来源。
