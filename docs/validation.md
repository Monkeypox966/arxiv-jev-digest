# 实际联调记录

验证日期：2026-10-24，Asia/Shanghai。

以下结果来自真实 arXiv、TypeSafe Jev、DeepSeek 和 QQ SMTP 调用。离线演示不计入这些结果；记录不包含密钥、授权码或个人邮箱地址。

## 自动化回归

[CI 运行记录](https://github.com/Monkeypox966/arxiv-jev-digest/actions/runs/40072316621)：Linux / Windows × Python 3.11 / 3.13，四组各 67 项测试全部通过，同时通过格式、静态检查、配置检查和离线演示。

## 真实接口与投递

| 验证项 | 结果 |
| --- | --- |
| arXiv API | 实际分页获取近 7 天、四个示例分类的 2,556 篇候选 |
| Jev | 官方 `jev-1.14.0` 返回合法 Noul 概率、实际模型版本和输入 token 数 |
| DeepSeek | `deepseek-flash` 为 30 篇入选论文返回六个完整结构化字段 |
| 排序与排除 | 入选 ID 不重复，按匹配分数降序，均通过排除规则与阈值；此轮分数为 0.93–0.96 |
| SMTP | QQ SSL 465 的 TLS 握手、登录、服务器接收均通过 |
| 实际收件 | 在 QQ 收件箱打开并确认连通性测试邮件及 30 篇论文正式日报 |
| 邮件渲染 | 正式 HTML 约 92 KB，实际 QQ 邮件页面可读取 30 个方法段落与报告页尾 |

真实单篇接口探测使用 arXiv `2610.31667`，未使用虚构演示论文。

## 中断恢复

为验证恢复能力，主动中断了首轮任务。[中断运行](https://github.com/Monkeypox966/arxiv-jev-digest/actions/runs/40071745625)按预期显示 cancelled，但其状态保存和备份步骤均成功，保留了 990 条 Jev 结果。下载备份后的 SQLite 完整性检查为 `ok`，没有待确认邮件。

[恢复后的预览](https://github.com/Monkeypox966/arxiv-jev-digest/actions/runs/40072342988)成功读取状态分支，复用当前窗口内的 985 条缓存，完成剩余 1,571 篇判断及 30 篇摘要。滚动时间窗口与论文内容变化会影响可复用数量，不要求与旧快照中的缓存总数相同。

## 缓存发信与重跑去重

[正式发送](https://github.com/Monkeypox966/arxiv-jev-digest/actions/runs/40072401891)：

- 候选 2,556 篇，入选 30 篇。
- 全部复用已保存结果：新增 Jev 调用 0 次、DeepSeek 调用 0 次。
- 发出正式日报 1 封，并在 QQ 收件箱确认送达。
- 保存后的数据库完整性为 `ok`；已发送论文 30 篇、已处理论文 2,556 篇、待确认投递 0 条。

[相同配置重复运行](https://github.com/Monkeypox966/arxiv-jev-digest/actions/runs/40072704842)：

- 仍获取到 2,556 篇候选，均由历史记录去重。
- 新增 Jev 调用 0 次、DeepSeek 调用 0 次、邮件 0 封。
- 工作流成功结束，状态分支再次保存成功。

这些结果验证接口集成、恢复和投递行为，不代表筛选阈值经过个人偏好校准，也不构成摘要准确率或论文质量的评测。
