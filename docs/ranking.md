# 机构偏好排序与过滤

流程为：全部新增论文 → Jev 相关性判断与排除规则 → 相关性门槛 → 所有达标论文的机构核实 → 机构最低分过滤 → 综合排序 → Top K → 中文摘要与邮件。

机构是用户指定的论文质量代理指标，分值是可修改的阅读偏好，**不是客观大学排名，也不是对论文实验、创新性或可信度的评审**。不根据作者姓名、项目所用机器人品牌、摘要提到的公司或被引用论文推断作者机构。

## 默认设置

```toml
[ranking]
enabled = true
institution_weight = 0.35
min_institution_score = 0.80
unknown_policy = "keep"
unlisted_policy = "keep"
neutral_score = 0.60
assess_unlisted = true
learn_institutions = true
assessment_cache_days = 30
assessment_retry_hours = 168
assessment_min_confidence = 0.80
assessment_limit_per_run = 12
assessment_retry_limit_per_run = 20
```

`enrichment.enabled = true` 是开启机构排序的前提。所有达到相关性门槛的论文都先核实机构，不在旧的 Top K 中二次重排。只有最终入选论文生成摘要；机构提取本身可能调用 DeepSeek，查阅量与费用因此增加。机构结果单独缓存，可复用。

- 综合分 = `(1 − institution_weight) × Jev 相关性分 + institution_weight × 机构分`。机构权重取值 0–0.5，确保相关性权重至少一半。
- 先检查相关性 `selection.min_score` 与排除规则。高机构分不能让不相关论文通过。
- 机构分取从论文 / 项目页提取、带来源的署名机构清单中的**最高分**，不要求逐个作者对应，不累计机构数量，不区分一作或通讯作者，不凭作者名猜测机构。
- 已核实机构但未命中名单：开启辅助评估后，尝试核实机构身份及官网研究资料，再给有证据的辅助偏好分；没有足够资料时仍为 `unlisted`，默认用 60 分中性基线并保留。设 `unlisted_policy = "exclude"` 可选择严格排除未评估机构。
- 未提取到带来源的署名机构：标记 `unknown`，默认保留并用 60 分中性基线。设 `unknown_policy = "exclude"` 可严格移除。不因缺少作者对应关系而拒绝机构或补查。
- `min_institution_score` 作用于人工评分或已有证据的辅助评分，取值 0–1；默认 0.8 会移除人工 B / 0.6 档，改为 0.6 可保留 B 档，设为 0 不按分值过滤。中性保底并非已判定机构较弱，未评估和未知机构分别由两项 policy 决定。
- 同分按相关性、arXiv ID 稳定排序。相关性分、综合排序分、机构分及匹配依据保留在 JSON 报告中，邮件不展示这些诊断明细。

默认名单位于 [`institutions.json`](../src/arxiv_digest/data/institutions.json)：

| 分值 | 默认偏好示例 |
| --- | --- |
| S / 1.0 | Stanford、CMU、MIT、UC Berkeley、清华、北大、上海交大、浙大、ETH、TUM、KAIST、NUS、NTU、HKUST、CUHK；NVIDIA、Physical Intelligence、Figure、银河通用、智元、RobotEra、Galaxea、Agility、Apptronik、1X 等 |
| A / 0.8 | 北航、同济、南科大、西湖大学、复旦、中科大、哈工大、UT Austin、UPenn、UCSD、EPFL、牛津、剑桥等；傅利叶、Generalist AI、Covariant 等 |
| B / 0.6 | Harvard、UCLA、武汉大学、北京邮电大学、Purdue、Aalto、ShanghaiTech、University of Nevada Reno、ROBOTIS 等 |

名单不穷尽所有优秀机构；尤其“未列入”仅表示当前规则未收录。建议根据实际研究偏好增补，不必照搬分档。

当前名单根据用户提供的 S / A / B 分级整理为 232 家（38 / 132 / 62 家），完整条目及别名见[机构名单](institution-list.md)。重复机构取最高档；未在新资料中分级的原有条目保留旧分数。

## 名单外机构的辅助评估

先使用 [ROR 公开机构登记](https://ror.readme.io/docs/api-query)的 affiliation 匹配核实实体；仅接受唯一 chosen 且匹配分至少 0.9 的结果。未命中时再按名称查询登记，要求原始名称与登记的名称或简称完全对应且实体唯一，避免有效简称被 affiliation 检索漏掉。登记归一化名称若命中人工名单，直接使用人工分数。

否则读取登记中关联的机构官网及同域页面，总计最多三页，优先机器人研究、机构主页及研究入口，支持联合门户先跳转至对应大学主页。复用已有的公开地址、重定向、体积和超时限制。提供原文给 DeepSeek，要求按当前研究主题、具体研究成果辅助评为 60 / 80 / 100 分；不凭机构名字、投资金额或宣传口号评分。

80 分需要可核对的官网原文引用，100 分需要两份不同内容的官网页面；引用 URL 必须是实际提供的页面，引用文本必须真实存在。机构评估考察已公开的研究实力，相关机器人及 AI 成果也可作为证据，不要求机构覆盖每个细分兴趣；论文相关性已在前一步独立筛选。模型自报置信度需至少 0.8，否则改用中性分。评分属于阅读偏好启发式，不能解释成客观机构排名或经过校准的质量概率。

ROR 无法确定实体时，DeepSeek 可以提出机构全称和官网线索。程序实际读取该官网，再让模型核对名称关系；只有引用确实来自提供的页面、同时包含原始名称和全称、置信度达到门槛且无歧义，才接受该对应。官网线索本身不算证明，也不因此直接给质量分。这一过程不依赖额外搜索服务，不进行无界网页搜索；部分未登记的新公司仍可能缺少足够资料。

身份有歧义、官网不可读、证据不足或达到本轮查找上限时使用中性分。已有明确成果但水平有限可评为 B / 60 分，与“证据不足，暂用中性分”区分；确认的 B 档参与最低分过滤，未核实不直接当成低质量。

同一机构临时评估默认缓存 30 天，中性结果默认 7 天后重查。模型明确回答资料不足或身份不确定时直接接受中性结论，不为此重复调用；格式错误或不实引用仍按模型输出错误处理。每轮最多查 12 个未缓存机构，超出额度的暂用中性分且不长期缓存。DeepSeek 的身份识别、评分及其重试与摘要共用总调用预算，在 usage.json 中以 `institution_identity` 和 `institution_assessment` 分别记录。API 权限、余额和预算错误仍会使整轮失败。

## 自动扩充机构与简称名单

`learn_institutions = true` 默认开启，配合 `assess_unlisted` 使用：

- 原始名称未命中人工规则、经核实后对应到已有机构：保存“新简称 → 人工机构全称”的关系，继承人工分数。
- 新机构身份及研究成果已核实：保存规范名称、S/A/B 分数、观测到的别名、置信度、身份和评分的引用来源及时间，后续直接复用。
- 只收录本次实际出现并核实的名称及其全称，不批量保存模型随意生成的其他缩写。自动别名采用规范化后的整名匹配，避免一个短缩写误匹配另一个实验室的长名称。
- 同一别名出现多个机构时保留冲突记录、暂停自动对应，不能用新结果覆盖旧机构。证据不足或有歧义的中性结果仅进入短期缓存，不进入确定名单。
- 人工名单始终优先；新增人工规则或修改分数会覆盖已学习评分，移除人工规则不会把旧人工分数永久当成模型评分。

增量名单持久化到 SQLite v5，并导出为 `.state/institutions.json`。工作流将二者一起提交到 `digest-state` 分支，也上传恢复 Artifact；主分支的人工名单继续作为可编辑基准。JSON 是便于查看的导出，数据库是自动名单的来源，不直接编辑导出文件来修改分数；要覆盖结果，在人工名单中填写明确规则。

查看本地已学习名单：

```bash
uv run arxiv-digest institutions
```

手动核查 1–5 个机构或简称，可使用：

```bash
uv run arxiv-digest institutions --assess "MPI-IS" "Tampere University"
```

或在 Actions 选择 `institutions`，在 `institution_names` 中每行填写一个名字。此模式真实调用 DeepSeek、保存确定的机构及别名并生成 `institution-review.json`，不抓取论文、不调用 Jev、不发邮件、不更改发送历史。

不带 `--assess` 的查看命令不调用模型。成功学到的机构与简称不受普通 30 天缓存清理影响；自动评分按分类及研究兴趣标签区分上下文，换研究方向可重新评估。设 `learn_institutions = false` 可关闭持久学习并恢复临时缓存行为。未来机构实力变化不会自动使永久记录失效，应通过人工规则纠正需要调整的分数。

未发送、未入选且仍有可能因补评提高机构分的候选进入独立持久化补评队列：达到额度的 24 小时后重查，资料不足的按 7 天缓存期限重查，每轮最多恢复 20 篇。已处理记录及离开最新公告批次不会阻止补评；到期候选优先取得查找额度。已发送或不再需要补评的论文清理队列，不因此重发；更改分类后不会补查旧分类。

人工名单始终优先；加入或修改人工分数会覆盖模型意见。多个机构仍取最高分，不累计。详细评分、引用及时间只放在 JSON 报告，邮件继续只展示简短机构概览。[当前完整人工名单](institution-list.md)

## 修改名单

可直接编辑打包的 JSON；也可在 `config.toml` 中添加以下表，**替换整份默认名单**：

```toml
[[ranking.institutions]]
name = "MIT"
score = 1.0
aliases = ["Massachusetts Institute of Technology", "MIT", "麻省理工学院"]

[[ranking.institutions]]
name = "Physical Intelligence"
score = 1.0
match_mode = "exact"
aliases = ["Physical Intelligence", "Physical Intelligence, Inc."]

[[ranking.institutions]]
name = "另一个关注机构"
score = 0.8
aliases = ["Example Robotics Institute"]
```

默认 `match_mode = "phrase"`：统一 Unicode、大小写与空白，并对英文短语或缩写检查字符边界，避免 MIT 匹配 MITRE。`exact` 要求整条机构名称匹配，用于容易与普通实验室名称混淆的公司，如 Physical Intelligence（不会把同济的 Physical Intelligence Lab 当成该公司）。不要加入无辨识度的缩写，例如 PI、交大、华盛顿。

`exact` 允许常见公司法律后缀及受限地点后缀，例如 `Figure AI, Inc.`、`Physical Intelligence, San Francisco, USA`；不会忽略任意尾部文字，`Physical Intelligence Lab` 仍不匹配公司。括号内缩写先与前面的完整机构名消歧：台湾大学的 NTU 不会当作南洋理工，分校后附母校缩写也不能提升分数。

同一机构名称中，明确校区 / 下属研究所的更具体匹配优先，覆盖其内部包含的母校 / 总院名字。例如 HKUST 广州为 A、HKUST 本部为 S；中科院计算所为 B，不能被总院的 A 覆盖。独立列出的合作机构仍分别计分、取最高值。通用单词公司名如 Humanoid、Intrinsic、Flexion、ugo 使用整名匹配，避免把研究主题误当公司。

示例配置采用相关性 65% + 机构偏好 35%。邮件聚焦摘要、作者简表、机构概览和论文 / 项目链接，不展示评分依据或逐作者机构对应。`digest.json` 的每篇记录保留完整 `ranking`、机构清单和来源；同目录 `ranking-audit.json` 记录所有通过相关性门槛的候选、机构匹配、是否通过机构过滤、是否入选，便于检查被移除的论文。切换权重、名单或机构判别策略会重新考虑窗口内未发送论文，复用原 Jev 判断；摘要提示词版本变化时生成新版摘要，已发送论文不会自动重发。

## 实现参考

开发前检索了 GitHub 的 arXiv 排序与机构匹配实现，包括 [ROR affiliation matching experimental](https://github.com/ror-community/affiliation-matching-experimental) 和 [affiliation-matching](https://github.com/mkallipo/affiliation-matching)。本项目的需求是少量可编辑偏好规则，并包含具身机器人公司，采用可审计的本地别名匹配，未引入通用机构消歧模型或外部排名服务。

## 2026-10-24 验证

本地 100 项测试通过，新增覆盖了相关性先行、机构低分过滤、Top K 之前重排、机构别名误匹配、缺失来源和作者映射、未知机构策略、缓存兼容、权重修改与邮件去重。

另外离线复用了上次已发送的 8 篇真实论文及其摘要、相关性分和核实机构，未重新筛选完整 69 篇，也未重发邮件。8 篇均达到当前 60 分机构门槛；LocoManiLab 与 Humanoid Mobility 的机构分为 100，综合分同为 98.4；PhysibleMDM 凭 MIT 署名获得机构分 100，综合分 93.6，排在 MPBRS（机构分 60，综合分 89.6）之前。同济 Physical Intelligence Lab 仅匹配同济（80），没有误记为 PI 公司。
