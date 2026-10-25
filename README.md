# arXiv Jev Digest

[English](README.en.md) | 简体中文

用 **Jev 或严格兼容 System One 协议的决策模型**筛选新增 arXiv 论文，用 **DeepSeek** 生成中文摘要，再通过邮件发送。运行在 GitHub Actions，无需服务器。

默认追踪 **cs.RO 人形机器人、全身控制、动作追踪与模仿**，每批最多 10 篇。邮件包含标题、arXiv 链接、中文摘要、项目链接及作者机构；找不到的信息会标明缺失。研究方向、机构偏好、语言和邮箱均可修改。

```text
arXiv 最新公告 → 历史去重 → 决策模型相关性筛选 → 机构核实与排序 → DeepSeek 摘要 → 邮件
```

<br>

## 配置教程

<br>

### 1. Fork 并启用 Actions

Fork 本仓库，进入 **Actions** 启用工作流。配置和工作流需保存在默认分支。

<br>

### 2. 设置研究兴趣

编辑 [`config.toml`](config.toml)，修改原有配置中对应的字段：

| 配置 | 用途 |
| --- | --- |
| `arxiv.categories` | arXiv 分类，如 `["cs.RO"]` 或 `["cs.AI", "cs.LG"]` |
| `[[interests]]` | 研究兴趣；替换示例中的兴趣，避免保留无关方向 |
| `[[exclusions]]` | 要排除的主题；换方向时也应检查这些规则 |
| `selection.top_k` | 每批最多推送数量，默认 10 |
| `selection.min_score` | 相关性门槛，默认 0.60 |
| `ranking` | 相关性通过后按机构偏好过滤、排序，默认权重 20%；详见[排序配置](docs/ranking.md) |
| `deepseek.language` | 摘要语言，默认简体中文 |
| `email` | SMTP 服务器、端口、安全模式和邮件标题 |

每项兴趣写一个可从标题和摘要判断的明确命题，建议用英文。例如，替换原有的一项兴趣：

```toml
[[interests]]
id = "robot_learning"
label = "机器人模仿学习"
text = "Proposes a method for learning robot manipulation policies from demonstrations."
weight = 1.0
```

不要重复粘贴同名 TOML 配置表。模型地址、型号和运行预算也可在配置中调整；默认使用 TypeSafe 官方 Jev 和 DeepSeek 官方接口。API 调用会产生费用，元数据提取和失败重试也计入用量。

<br>

### 3. 选择决策后端（可选）

默认使用 TypeSafe Jev。支持 OpenRouter，以及其他严格兼容现有 System One 协议的决策服务；每次运行使用一个后端。切换服务只需修改原有 `[jev]` 配置：

```toml
[jev]
backend = "my-provider"
endpoint = "https://decision.example.com/v1/systemone"
model = "provider-model-id"
api_key_env = "DECISION_API_KEY"
```

将示例地址和型号换成服务商提供的实际值，`backend` 可自定义为小写字母开头的名称，允许数字、短横线和下划线。新后端必须显式填写 `endpoint` 和 `model`。接口须使用 HTTPS、Bearer 密钥，接受 `state + questions` 对象，并返回 `answers.<问题名>.type = "noul"` 及 `noul` 概率（0–1）；不同协议需要另行适配。每篇论文的兴趣与排除项一次提交，数量须符合服务商上限。

| 后端 | 密钥变量（未设置 `api_key_env` 时） | 地址与型号 |
| --- | --- | --- |
| `typesafe` | `TYPESAFE_API_KEY` | 默认官方 Jev 配置 |
| `openrouter` | `OPENROUTER_API_KEY` | `https://openrouter.ai/api/alpha/decisions`；`typesafe/jev-1.14` |
| 自定义名称 | `DECISION_API_KEY` | 服务商的完整决策接口地址、模型 ID |

GitHub Actions 已传入上述三个 Secret；其他服务的密钥推荐保存为 `DECISION_API_KEY`。本地可用 `api_key_env` 指定其他变量名；在 Actions 使用其他变量名时，也需在工作流 `env` 中传入对应 Secret。切换后先运行 `preview` 检查效果和阈值。兼容性接口测试不等于各家服务已完成真实密钥联调。

<br>

### 4. 添加密钥与邮箱

进入仓库 **Settings → Secrets and variables → Actions → New repository secret**，添加：

| Secret | 填写内容 |
| --- | --- |
| 决策服务密钥 | 默认填 `TYPESAFE_API_KEY`（[申请地址](https://console.typesafe.ai/settings/keys)）；其他后端按上表选择，只需填写所用服务的密钥 |
| `DEEPSEEK_API_KEY` | [DeepSeek API Key](https://platform.deepseek.com/api_keys) |
| `SMTP_USERNAME` | 完整发件邮箱地址 |
| `SMTP_PASSWORD` | 邮箱 SMTP 授权码或应用密码 |
| `EMAIL_FROM` | 发件地址，通常与 `SMTP_USERNAME` 相同 |
| `EMAIL_TO` | 收件地址，多个地址用英文逗号分隔 |

**QQ 邮箱示例**：在邮箱的“账号与安全 → 安全设置”开启 SMTP 并生成授权码，将授权码填入 `SMTP_PASSWORD`。默认配置已使用 `smtp.qq.com`、端口 `465`、`security = "ssl"`；不要填写 QQ 登录密码。

其他邮箱请将 `[email]` 改为服务商提供的参数，支持 SSL 和 STARTTLS（常用端口分别为 465、587）。凭据只存 Secrets，不要写入代码或提交到仓库。

<br>

### 5. 测试并开始推送

进入 **Actions → Daily arXiv Digest → Run workflow**：

1. 选择 `preview`：真实抓取并调用模型，但不发邮件。完成后下载 **Artifacts** 中的报告，打开 `digest.html` 检查结果。
2. 选择 `delivery_test`：只发送一封测试邮件，不调用模型。
3. 选择 `send`：正常筛选并发送日报。后续定时任务自动按此模式运行。

默认每天**北京时间 09:30 启动**，完成筛选后发信。修改 [`daily.yml`](.github/workflows/daily.yml) 中的 `cron: '30 1 * * *'` 可调整时间，当前表达式使用 UTC；`config.toml` 的 `timezone` 只影响报告显示。

**没有新增匹配论文时也发送空日报**（`email.send_empty = true`），邮件会说明本次无匹配论文。空日报本身不额外调用模型；如有新候选，仍需调用决策模型判断相关性。已发送论文不会重发，周末可能继续读取上一公告批次。设为 `false` 可关闭空日报。GitHub 定时任务可能延迟或漏触发，并非准点投递服务；超过 60 天无仓库活动也可能自动停用。[官方说明](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)

<br>

## 没收到邮件时

先查看 **Actions → 最近一次运行 → Summary**，确认是否触发、是否成功、筛选数量和发送数量：

- **没有运行记录**：检查工作流是否启用、是否在默认分支；可手动运行 `send` 补跑。
- **成功但发送为 0**：检查是否运行了 `preview` 或关闭了 `email.send_empty`；正常发送模式默认也会发送空日报。
- **运行失败**：查看错误和 `health.json`；核对 API 余额、Secrets、SMTP 授权码。
- **存在 pending**：先核查收件箱与垃圾箱，再按[恢复手册](docs/operations.md)确认，避免重复投递。

发送历史、缓存保存在自动创建的 `digest-state` 分支，需保留工作流的 `contents: write` 权限和状态分支。报告和恢复数据库保存在 Actions Artifacts。

<br>

## 本地运行（可选）

安装 [uv](https://docs.astral.sh/uv/getting-started/installation/) 和 Python 3.11+，克隆项目后执行：

```bash
uv sync --locked
uv run arxiv-digest check
uv run arxiv-digest demo
```

`demo` 离线生成虚构示例报告，不需要密钥。真实运行前将 [`.env.example`](.env.example) 复制为 `.env` 并填入凭据，然后运行：

```bash
uv run arxiv-digest check --credentials
uv run arxiv-digest run --dry-run
uv run arxiv-digest run
```

<br>

## 更多文档

[运维、诊断与恢复](docs/operations.md) · [机构排序](docs/ranking.md) · [项目链接与机构提取](docs/enrichment.md) · [架构](docs/architecture.md) · [验证记录](docs/reliability-validation.md) · [贡献](CONTRIBUTING.md)

机构偏好不等于客观论文质量；自动筛选、摘要和机构提取仍可能有误，请以原文为准。

参考项目：[JEV-Paper-Radar](https://github.com/Eliot5566/JEV-Paper-Radar)、[daily-arXiv-ai-enhanced](https://github.com/dw-dengwei/daily-arXiv-ai-enhanced)、[arXiv-Daily-Summarizer](https://github.com/RunRiotComeOn/arXiv-Daily-Summarizer)、[Arxiv-tracker](https://github.com/colorfulandcjy0806/Arxiv-tracker)。

[MIT License](LICENSE) · [安全问题](SECURITY.md)
