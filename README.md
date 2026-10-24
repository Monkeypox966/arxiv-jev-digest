# arXiv Jev Digest

每天让 **Jev** 从研究领域内的新增 arXiv 论文中筛选相关工作，再用 **DeepSeek** 生成结构化摘要，通过邮件送达。运行在 GitHub Actions，无需常驻服务器。

研究兴趣、筛选数量、模型、语言、邮箱服务和运行时间均可配置。当前默认配置追踪 **cs.RO 最新公告批次的人形机器人、全身控制、动作追踪与模仿**，每天最多 10 篇；可自由替换为其他研究方向。

```text
arXiv → 分页获取候选 → 历史去重 → Jev 逐篇判断各项兴趣
     → 相关性门槛 → 核实机构 → 机构过滤 + 综合排序 + Top K → DeepSeek 摘要 → 邮件
                                 ↘ 结果缓存、JSON 报告、发送状态
```

## 功能

- **先筛选再总结**：Jev 阅读每篇候选的标题、原始摘要和分类，只有入选论文生成摘要；达标候选的机构提取可能另外调用 DeepSeek。没有关键词预筛。
- **机构偏好与过滤**：先过相关性门槛，再按作者机构排序，默认相关性 80% + 机构 20%；已评分机构低于 60 分移除。未收录与未核实分开配置，默认保留并标注，避免名单漏收导致误删。详见[排序规则](docs/ranking.md)。
- **可解释的筛选**：每项兴趣单独返回 Noul 概率；权重、合成方式和阈值由代码决定，报告保留实际模型版本和各项分数。
- **明确公告批次**：默认读取分类 recent 页的最新一批，再用 API 获取全部元数据，区分公告日期与提交日期；可切换滚动 API 窗口或 RSS。
- **可恢复运行**：模型结果缓存；发送历史按收件人独立保存；成功才标记完成。SMTP 接收结果不确定时进入人工确认流程。
- **漏跑补抓与补查**：每个分类、收件人有独立公告检查点；在可验证的 recent 历史内补抓。缺机构的未发送论文可有限重试，缺失资料不长期缓存。
- **预算与健康报告**：所有模型 HTTP 尝试（含重试）计入预算，机构提取和摘要分别统计；失败也保存 `health.json` / `usage.json`。可选向原收件人发送失败通知，预览和元数据诊断永不发邮件。
- **结构化摘要**：翻译标题、一句话概述、研究问题、核心方法、主要结果、阅读提醒。明确只依据原始摘要，不宣称读过全文。
- **作者与项目来源**：读取 arXiv 首页摘要 / Comments、正文 HTML、PDF 和已确认项目页，按字段补缺；检测 HTML 机构标记丢失，必要时补查 PDF。项目和机构各自保留来源，项目页只补缺失作者的机构。详见[覆盖范围与验证](docs/enrichment.md)。
- **安全配置**：密钥和邮箱地址通过环境变量 / GitHub Secrets 设置；邮件内容进行 HTML 转义；无 PR 密钥调用。
- **开箱可检查**：离线演示、配置检查、测试、锁定依赖、跨平台 CI、MIT 许可。

0.2.0 的真实模型、QQ 投递、重复运行和故障恢复核查见[可靠性验证记录](docs/reliability-validation.md)。

## 五分钟部署

### 1. 准备仓库

将项目推送到自己的 GitHub 仓库，或 Fork 已发布的仓库。定时工作流必须位于仓库默认分支；Fork 后在 Actions 页面启用工作流。

### 2. 编辑研究兴趣和邮箱配置

修改 [`config.toml`](config.toml)：

```toml
[arxiv]
categories = ["cs.AI", "cs.CL", "cs.LG"]

[selection]
top_k = 10
min_score = 0.60

[[interests]]
id = "agents"
label = "智能体评测"
text = "Introduces a benchmark for language-model agents that use tools."
weight = 1.0
```

以上为节选，请直接编辑原配置中对应区块，避免重复 TOML 表。每项兴趣表达一个可以从摘要判断的命题；建议用英文写判断条件。排除条件另写在 `[[exclusions]]`，用正向句子描述被排除的主题，例如 “The primary application is medical image analysis.”。

邮箱默认使用 QQ 邮箱示例：`smtp.qq.com`、端口 `465`、`security = "ssl"`。使用其他服务商时修改 `[email].host`、`port` 和 `security`。支持 SSL 与 STARTTLS，不支持明文连接。

### 3. 添加 GitHub Secrets

打开仓库 **Settings → Secrets and variables → Actions → New repository secret**：

| Secret | 用途 |
| --- | --- |
| `TYPESAFE_API_KEY` | 官方 Jev API 密钥；[申请地址](https://console.typesafe.ai/settings/keys) |
| `DEEPSEEK_API_KEY` | DeepSeek 或所配置兼容服务的密钥 |
| `SMTP_USERNAME` | SMTP 登录名，通常是完整发件邮箱 |
| `SMTP_PASSWORD` | SMTP 授权码或应用密码 |
| `EMAIL_FROM` | 发件邮箱，通常与 SMTP_USERNAME 一致 |
| `EMAIL_TO` | 收件邮箱，多人用英文逗号分隔，每人单独收信 |

如果通过 OpenRouter 使用 Jev，改为配置 `OPENROUTER_API_KEY`，同时在 `config.toml` 设置：

```toml
[jev]
backend = "openrouter"
endpoint = "https://openrouter.ai/api/alpha/decisions"
model = "typesafe/jev-1.14"
```

OpenRouter 的 Jev 使用 decisions 接口，不能使用普通 chat/completions 接口。不同后端的模型标识不能混用。

### 4. 预览，再启用发送

进入 **Actions → Daily arXiv Digest → Run workflow**：

1. 首次选择 `preview`。这会真实抓取论文、调用两个模型，**产生 API 用量，但不发邮件、不标记已读**。
2. 在工作流 Artifacts 下载报告，打开 `digest.html` 检查筛选和摘要。
3. 再次手动运行，选择 `send`，验证实际收信。

单独核查论文链接与机构可选择 `inspect`，在 `paper_ids` 填 1–5 个空格分隔的 arXiv ID，例如 `2605.07315 2508.08241 2606.03985`。这会真实调用元数据提取模型，生成 `metadata.json`，不调用 JEV 筛选，也不发送邮件。

只验证邮箱可选择 `delivery_test`：会真实发送一封标题明确的投递联调邮件，不调用模型，不清除或改动真实论文发送历史，并使用同样的远程 pending 保护。CLI 为 `test-email`。

之后每天自动执行发送模式。默认每天北京时间 **09:30** 启动，处理完成后发送邮件。修改 [`.github/workflows/daily.yml`](.github/workflows/daily.yml) 的 `cron` 可调整启动时间，表达式使用 UTC，当前的 `30 1 * * *` 对应北京时间 09:30。配置中的 `timezone` 只控制报告显示，不会改变 Actions 调度。

GitHub 调度可能延迟；公共仓库长期没有活动也可能停用定时工作流。详见 [GitHub schedule 文档](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)。

## 本地使用

安装 [uv](https://docs.astral.sh/uv/getting-started/installation/)，在项目目录运行：

```bash
uv sync --locked
uv run arxiv-digest check
uv run arxiv-digest demo
```

`demo` 完全离线、不需要密钥，生成 `output/demo/digest.html`。其中论文、概率和摘要均为虚构示例，演示不代表 Jev 实际效果。

真实运行前将 `.env.example` 复制为 `.env`，填入凭据。系统环境变量优先于 `.env`；此文件已加入忽略规则。

```bash
uv run arxiv-digest check --credentials
uv run arxiv-digest run --dry-run
uv run arxiv-digest run
uv run arxiv-digest inspect --refresh 2508.08241 2606.03985
uv run arxiv-digest status
```

指定其他配置文件：

```bash
uv run arxiv-digest --config profiles/robotics.toml check
```

路径相对于配置文件所在目录解析。请使用完整配置；`profiles/` 中的说明提供了扩展方法。

## 配置行为

| 区块 | 关键配置 | 行为 |
| --- | --- | --- |
| `arxiv` | `categories`, `source`, `lookback_days`, `catch_up` | 默认 latest 首次取最新批，后续在 recent 覆盖内精确补抓；api 为滚动提交窗口；rss 为最近公告流 |
| `arxiv` | `page_size`, `max_candidates` | 完整分页；超过候选上限直接失败，避免悄悄漏掉论文 |
| `jev` | `backend`, `endpoint`, `model` | TypeSafe 默认固定 `jev-1.14.0`；可切换 OpenRouter |
| `selection` | `top_k`, `min_score` | 默认最多 10 篇；满足阈值不足时不强行补齐 |
| `selection` | `combine` | 默认 `max(weight × p)`；或 `1 − ∏(1 − weight × p)` |
| `selection` | `exclude_threshold` | 任一排除项达到阈值就剔除 |
| `ranking` | `institution_weight`, `min_institution_score`, `unlisted_policy` | 已评分机构按门槛过滤；未收录默认保留，名单不是客观质量评审 |
| `deepseek` | `base_url`, `model`, `language` | 默认 DeepSeek 官方 `deepseek-flash`，支持兼容接口 |
| `deepseek` | `disable_thinking` | 默认发送 `thinking: {type: disabled}`；服务不支持该字段时设为 false |
| `email` | `recipients`, `send_empty` | EMAIL_TO 覆盖收件人；默认无匹配不发空邮件 |
| `retention` | `cache_days` | 缓存保留 30 天；已发送和已处理历史持续保留 |
| `enrichment` | `incomplete_cache_hours`, `retry_attempts` | 不完整元数据默认缓存 24 小时；未发送且机构不完整的候选最多查找 3 次（含首次） |
| `enrichment` | `max_pdf_pages`, `follow_project_page` | 有界 PDF 正文与可识别附录扫描，项目页仅补缺；按论文版本保存来源 |
| `budget` | `max_jev_calls`, `max_deepseek_calls`, `max_input_tokens`, `max_output_tokens` | 每轮硬调用上限及 token 预留/实耗结算，耗尽即阻断新调用与发信 |
| `notifications` | `email_on_failure` | 默认配置开启发送模式失败通知；SMTP 自身故障时仍需查看 GitHub 失败记录 |
| 根配置 | `compact_summaries` | 默认展示一段贡献摘要；false 展示完整六字段摘要，JSON 始终保留所有字段 |

权重合成后的分数用于排序，尤其 noisy-OR 不应解释为经校准的整体概率；相关性也不代表论文质量。

Jev 示例配置使用 8 个并发请求、每分钟最多 600 次调用。请按自己组织在 TypeSafe 控制台显示的限额调整 `concurrency` 和 `requests_per_minute`，并为同一组织的其他应用预留额度。日志每完成 100 篇筛选、5 篇摘要报告一次进度。

API 模型名和可用性可能变化，默认值核验于 2026-10-24。参考 [TypeSafe 模型文档](https://docs.typesafe.ai/models)、[DeepSeek 模型文档](https://api-docs.deepseek.com/quick_start/pricing)。

### 邮箱设置

QQ 邮箱需在“账号与安全 → 安全设置”确认 POP3/IMAP/SMTP 服务已开启，并生成授权码，将授权码填入 `SMTP_PASSWORD`。账号与发件地址使用完整 QQ 邮箱地址；不要填写 QQ 登录密码。参数依据 [QQ 邮箱官方配置说明](https://wx.mail.qq.com/list/readtemplate?name=app_intro.html#/agreement/authorizationCode)。

使用邮箱服务商当前提供的 SMTP 参数和授权方式。在 `[email]` 中设置服务器和端口，在 Secrets / `.env` 中设置账号、授权码和收件人。例如服务商要求 SSL 465 时：

```toml
[email]
host = "smtp.your-provider.com"
port = 465
security = "ssl"
```

服务商要求 STARTTLS 587 时，将 `port` 设为 587、`security` 设为 `starttls`。发件人必须是服务商允许的地址。支持不同服务商的发件人和收件人。

## 抓取、历史与重跑

- `source = "latest"` 首次只取最新完整公告批次，包括交叉列入论文。开启 `catch_up` 后按每个收件人、分类的成功检查点补抓 recent 内后续公告；周末沿用最新批但不重发。超过 recent 可核实历史或 `catch_up_max_days` 会明确失败，不用提交日期冒充公告日期。显式恢复见[操作手册](docs/operations.md)。
- arXiv 新提交与公告时间不同，API 模式查询的是**提交时间窗口**，不是“过去 24 小时公布”的精确集合。默认窗口是部署第一天的候选范围，首次可能需要筛选几千篇；后续只处理新增或尚未完成的论文。
- 首次运行按整个窗口统一排序选 Top K。一次成功处理后，同批未入选论文也标记为已处理，下一天聚焦新增论文。更改兴趣、模型或筛选参数会生成新配置指纹，对窗口内未发送过的论文重新筛选。
- arXiv ID 去掉版本号，跨分类重复与后续版本不会重复推送。没有“新版更新提醒”。`include_cross_lists = false` 时只保留主分类匹配的论文。
- 成功的模型响应立即缓存。筛选失败或摘要不完整会使运行失败，**不会发送缺少摘要的半成品日报**；重跑可复用成功结果。
- 每位收件人有独立的已发送和已处理记录。多人接收时分别发送，不暴露其他收件人。增加收件人后，新收件人可以获得窗口内的最佳论文。
- `--dry-run` 只写缓存和报告，不改变发送 / 已处理状态。没有配置收件人时使用独立预览身份。
- `inspect` 仅核查元数据、更新缓存并写诊断报告，不改变已处理、发送和公告检查点。
- 缺少机构、未入选且未发送的候选可进入延迟补查队列；每轮最多 20 篇。尚未到期的重复运行不消耗重试次数；已发送论文永不因此重发。只缺项目链接不会把未入选论文持续补发。
- 每个真实报告保存在 `output/<日期-运行标识>/`，包含 HTML、纯文本、JSON；多收件人报告使用编号子目录。JSON 含入选论文、Jev 分数及摘要，不含邮箱地址和密钥。

机构与项目链接的人工核查记录位于 `src/arxiv_digest/data/verified_details.json`，记录核查日期和来源。自动提取只接受文档中实际出现的机构名称和候选链接，并在报告中标明自动提取状态；复杂脚注仍可能解析不完整，请以原文署名为准。项目链接为空表示未能找到或核实，不代表作者一定没有发布项目。内置的 2026-10-23 cs.RO 八篇记录已逐篇核对 PDF 首页，项目页另行核查。

### GitHub 状态持久化

工作流将 SQLite 数据库保存到独立的 **`digest-state` 分支**，不向主分支提交每日数据。失败时也保存成功调用的缓存和邮件待确认记录。并发组会串行执行工作流，不中断正在发信的运行。请保留 Actions 的 `contents: write` 权限，并允许工作流更新状态分支。

状态分支首次运行自动创建；网络或权限错误会直接失败，不会将“无法读取旧状态”误当首次运行。如果推送状态失败，工作流会失败，并尽量将数据库作为 Artifact 备份。**再次发送前先恢复失败运行的数据库到状态分支**，避免丢失发送历史。恢复步骤见 [`docs/operations.md`](docs/operations.md)。

状态包含论文、模型决策及邮箱地址哈希，不含明文邮箱或密钥。公共仓库的状态分支和报告可能被他人读取，也可能透露研究兴趣；需要保护研究兴趣时使用私有仓库。数据库只在受信任的每日 / 手动工作流中恢复，不供 PR 工作流读取。

如果修改 `state_dir` 或 `output_dir`，同步修改 workflow 的 Artifact 路径。长于 `lookback_days` 的停机会超出恢复窗口，可临时增加窗口（最多 31 天），并相应提高缓存保留天数与候选上限。

### 邮件接收结果不确定时

SMTP 无法与数据库原子提交；即使服务器接收了邮件，客户端也可能没收到确认。GitHub 发送模式会先写入 `pending`，再将数据库保存到远端状态分支，成功后才允许 SMTP DATA。确认发送后原子标记 `sent` 并推进公告检查点，最后再次保存远端状态。出现待确认记录时，后续自动发送停止。本地如需同等远端保护，可在可信仓库与 GH_TOKEN 环境使用 `run --persist-outbox`。

本地查看、确认：

```bash
uv run arxiv-digest pending
# 检查收件箱，已经收到时：
uv run arxiv-digest resolve DELIVERY_ID --outcome sent
# 确认没有收到，并接受重新尝试时：
uv run arxiv-digest resolve DELIVERY_ID --outcome retry
```

GitHub 上在手动工作流选 `resolve_sent` 或 `resolve_retry`，填入错误日志中的 delivery ID。确认步骤只更新状态；之后再运行 `send`。这不是 SMTP 层面的绝对一次投递保证；`sent` 表示发件服务器接受，最终送达仍可能受退信或垃圾箱规则影响。

## 开发与验证

真实模型调用、QQ 收件、中断恢复和重复运行的验证结果见 [初始联调记录](docs/validation.md)；最新专题测试见 [cs.RO 正式测试](docs/robotics-validation.md)。

```bash
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
uv build
```

测试使用模拟 HTTP / SMTP，覆盖分页、去重、概率校验、排除与排序、缓存、摘要错误、发送中断、多人部分成功和恢复。CI 在 Linux / Windows、Python 3.11 / 3.13 上运行，不需要密钥，不调用付费模型、不发送真实邮件。

## 目录

```text
.github/workflows/      每日工作流、跨平台 CI
.github/scripts/        状态分支读写
src/arxiv_digest/       抓取、决策、摘要、报告、邮件、状态和 CLI
tests/                  离线自动化测试
docs/                   运维和架构说明
profiles/               自定义研究方向说明
config.toml             可直接修改的示例配置
.env.example            凭据名称示例
uv.lock                 可复现的依赖版本
```

## 成本与局限

Jev、DeepSeek 和 SMTP 都是外部服务，账号可用性、API 用量和价格由服务商决定。项目不承诺免费额度，也不承诺每天固定费用。Jev 调用量约为新增候选数，DeepSeek 调用量最多为入选数；失败重试可能产生额外 API 费用。预览同样调用真实 API。

模型只读取标题和摘要，可能误筛或误总结。默认阈值未经个人标注集校准，建议先预览几天、检查漏选后再调整。arXiv API / RSS 的延迟或故障会让运行失败，需要在恢复窗口内重跑。

## 参考与致谢

项目独立实现，以下项目提供了架构思路：

- [JEV-Paper-Radar](https://github.com/Eliot5566/JEV-Paper-Radar)：逐兴趣 Noul 决策与代码合成排序。
- [daily-arXiv-ai-enhanced](https://github.com/dw-dengwei/daily-arXiv-ai-enhanced)：Actions 定期抓取与 AI 摘要。
- [arXiv-Daily-Summarizer](https://github.com/RunRiotComeOn/arXiv-Daily-Summarizer)：摘要邮件服务。
- [Arxiv-tracker](https://github.com/colorfulandcjy0806/Arxiv-tracker)：分页、新鲜度与发送后去重。

接口依据 [arXiv API 手册](https://info.arxiv.org/help/api/user-manual.html)、[TypeSafe System One](https://docs.typesafe.ai/api/system-one)、[DeepSeek JSON Output](https://api-docs.deepseek.com/guides/json_mode)。贡献流程见 [CONTRIBUTING.md](CONTRIBUTING.md)，安全问题见 [SECURITY.md](SECURITY.md)。

## License

[MIT](LICENSE)
