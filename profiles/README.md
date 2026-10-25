# 自定义研究方向

按下面步骤建立一份独立研究配置。所有命令都在项目根目录执行；首次使用项目时，先按 [主教程](../README.md) 安装依赖。

<br>

## 第一步：复制完整配置

将根目录的 `config.toml` 复制为 `profiles/robotics.toml`。
必须复制完整配置，再修改其中已有的字段，而不是仅创建一个包含兴趣片段的文件。

<br>

## 第二步：修改研究主题

在新配置中调整 `arxiv.categories`、`[[interests]]` 和 `[[exclusions]]`。
例如机器人学习可以使用 `categories = ["cs.RO", "cs.LG"]`，并将原有的一项兴趣替换为：

```toml
[[interests]]
id = "robot_learning"
label = "机器人策略学习"
text = "Proposes a method for learning robot manipulation policies from demonstrations."
weight = 1.0
```

每项兴趣只写一个能从标题和摘要判断的清晰命题，替换或移除不需要的示例兴趣。
同时检查原有的排除规则，移除不适合新方向的条件。

<br>

## 第三步：分开保存状态与报告

相对路径以该配置文件为基准。在复制后的配置中，将已有字段改为：

```toml
state_dir = "../.state-robotics"
output_dir = "../output/robotics"
```

这两个字段位于配置的顶层，请修改原有值，保留它们在首个配置表之前的位置。
不同订阅使用独立目录，避免发送历史和报告混淆。

<br>

## 第四步：检查并预览

```bash
uv run arxiv-digest --config profiles/robotics.toml check
uv run arxiv-digest --config profiles/robotics.toml run --dry-run
```

真实预览需要 API 密钥，但不会发送邮件。可使用环境变量，或将配置好的 `.env` 放在
`profiles/.env`（与配置文件同目录）；本地凭据文件不要提交到仓库。
查看输出目录中的报告，确认筛选方向和摘要效果后，
再运行发送命令：

```bash
uv run arxiv-digest --config profiles/robotics.toml run
```

<br>

## 在 GitHub Actions 中使用

默认工作流读取根目录 `config.toml`。只修改本目录文件不会切换定时任务。
需要同步修改工作流运行命令、状态脚本读取的配置文件及 Artifact 路径；相关说明见 [运维手册](../docs/operations.md)。

兴趣匹配概率不代表论文质量。日期和分类由抓取逻辑处理，不要要求决策模型仅从摘要判断作者声望、
实际发表日期或全文实验质量。
