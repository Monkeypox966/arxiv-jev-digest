# 自定义研究方向

复制根目录的 `config.toml` 成一个完整配置文件，再修改分类、兴趣和排除条件。例如机器人学习可以使用 `categories = ["cs.RO", "cs.LG"]`，并将兴趣改为：

```toml
[[interests]]
id = "robot_learning"
label = "机器人策略学习"
text = "Proposes a method for learning robot manipulation policies from demonstrations."
weight = 1.0
```

每项兴趣只写一个清晰命题。避免要求模型从摘要判断作者声望、论文实际发表日期或全文实验质量。日期和分类由抓取逻辑处理，匹配概率不代表研究质量。

如果文件保存在本目录，路径以该配置文件为基准。建议把 `state_dir` 设为 `../.state-robotics`，`output_dir` 设为 `../output/robotics`，避免不同订阅混淆。运行命令：

```bash
uv run arxiv-digest --config profiles/robotics.toml check
uv run arxiv-digest --config profiles/robotics.toml run --dry-run
```

GitHub 工作流默认读取根目录配置。切换工作流使用的配置时，需要同步修改运行命令、状态脚本读取的文件以及 Artifact 路径。
