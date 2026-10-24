# Security

密钥通过环境变量或 GitHub Secrets 提供。不要将 `.env`、授权码或完整服务响应放进 Issue、PR、日志或运行报告。

发现安全问题时，请使用仓库 **Security → Report a vulnerability** 私密报告（维护者需启用 Private vulnerability reporting）。若入口不可用，可先开不含利用细节和秘密信息的 Issue，请维护者提供私密报告渠道。

如果密钥泄露，立即到服务商撤销并重建；从代码删除不能让旧密钥失效。

工作流只在可信默认分支的定时 / 手动运行中读取 Secrets。PR CI 不读取发信凭据、不恢复生产状态。API 地址可配置，切换服务时请确认其可信性；对应密钥会发送到该地址。

公共仓库的配置、状态分支及 Actions Artifact 可能公开。状态只保存收件地址哈希，但这不是针对可猜地址的匿名化保证。使用私有仓库保护敏感研究方向和运行记录。
