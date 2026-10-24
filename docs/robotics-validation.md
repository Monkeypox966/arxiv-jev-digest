# cs.RO 正式专题测试：2026-10-24

## 范围与筛选

- 最新公告来源：[cs.RO recent](https://arxiv.org/list/cs.RO/recent)。
- 北京时间 2026-10-24 核查时，最新批次为 2026-10-23，共 69 篇，包括分类交叉列入论文。
- 逐篇交给官方 Jev 判断；采用人形机器人、全身控制、动作追踪 / 模仿 / 重定向 / 遥操作条件，并为人体动作感知方法设置较低权重。
- 排除飞行器、轮式驾驶、手术机器人和外骨骼辅助等不符合主题的主要应用。
- 入选 7 篇直接相关论文及 1 篇人体 IMU 运动感知补充论文。上限 10 篇，不以更早批次或无关论文补足数量。

| arXiv | 论文 | 项目链接 |
| --- | --- | --- |
| [2610.30540](https://arxiv.org/abs/2610.30540) | Motion Retargeting with Spatiotemporal Preferences for Humanoid Loco-Manipulation | 未找到 |
| [2610.30817](https://arxiv.org/abs/2610.30817) | Robot Motion Transition with Human Motion Prior | [STAMP](https://childtoy.github.io/STAMP-project/) |
| [2610.30876](https://arxiv.org/abs/2610.30876) | LocoManiLab | [项目页](https://locomanilab.github.io/) |
| [2610.31166](https://arxiv.org/abs/2610.31166) | Memory before Goal | [项目页](https://memory-before-goal.github.io/) |
| [2610.31608](https://arxiv.org/abs/2610.31608) | Humanoid Mobility via Attention-based Contacts | [ACP](https://leggedrobotics.github.io/ACP/) |
| [2610.30334](https://arxiv.org/abs/2610.30334) | MPBRS | [作者代码与演示](https://github.com/aralab-unr/MPBRS) |
| [2610.30500](https://arxiv.org/abs/2610.30500) | PhysibleMDM | 未找到 |
| [2610.31156](https://arxiv.org/abs/2610.31156) | Calibration-free IMU-based Relative Orientation Estimation under Dynamic Human Motion | 未找到；相关方法补充 |

八篇的完整作者—机构映射均核对论文 PDF 首页；复杂脚注另做了页面渲染检查。五个项目 / 代码链接实际访问返回 HTTP 200。其余三篇检查了论文页、PDF 链接及标题搜索，未发现可核实的作者项目网站；未将引用文献中的项目当作本论文项目。

人工核查记录位于 [`verified_details.json`](../src/arxiv_digest/data/verified_details.json)。摘要由 DeepSeek 根据标题与原始摘要生成，采用简洁、突出贡献的表述；机构与项目资料单独附来源，不混入摘要模型的输入。

## 运行结果

- [最终预览](https://github.com/Monkeypox966/arxiv-jev-digest/actions/runs/40074502009)：69 篇候选、8 篇完整摘要与元数据。
- [正式发送](https://github.com/Monkeypox966/arxiv-jev-digest/actions/runs/40074606064)：复用 69 条判断及 8 条摘要缓存，发送日报 1 封；在 QQ 收件箱打开并核对了 8 个作者机构区块、公告批次和报告页尾。
- [重复运行](https://github.com/Monkeypox966/arxiv-jev-digest/actions/runs/40074709171)：成功跳过已处理批次，新增模型调用与邮件均为 0。
- [跨平台 CI](https://github.com/Monkeypox966/arxiv-jev-digest/actions/runs/40074501396)：Linux / Windows × Python 3.11 / 3.13，各 78 项测试通过。

这次测试也将默认每日订阅切换到本专题、每批最多 10 篇，并保留原有 API 滚动窗口与 RSS 模式供其他订阅使用。研究相关性的人工偏好仍应通过后续阅读反馈继续调整。
