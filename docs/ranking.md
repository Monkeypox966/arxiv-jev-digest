# 机构偏好排序与过滤

流程为：全部新增论文 → Jev 相关性判断与排除规则 → 相关性门槛 → 所有达标论文的机构核实 → 机构最低分过滤 → 综合排序 → Top K → 中文摘要与邮件。

机构是用户指定的论文质量代理指标，分值是可修改的阅读偏好，**不是客观大学排名，也不是对论文实验、创新性或可信度的评审**。不根据作者姓名、项目所用机器人品牌、摘要提到的公司或被引用论文推断作者机构。

## 默认设置

```toml
[ranking]
enabled = true
institution_weight = 0.20
min_institution_score = 0.60
unknown_policy = "keep"
```

`enrichment.enabled = true` 是开启机构排序的前提。所有达到相关性门槛的论文都先核实机构，不在旧的 Top K 中二次重排。只有最终入选论文生成摘要；机构提取本身可能调用 DeepSeek，查阅量与费用因此增加。机构结果单独缓存，可复用。

- 综合分 = `(1 − institution_weight) × Jev 相关性分 + institution_weight × 机构分`。机构权重取值 0–0.5，确保相关性权重至少一半。
- 先检查相关性 `selection.min_score` 与排除规则。高机构分不能让不相关论文通过。
- 机构分取已核实作者所属机构的**最高分**，不累计机构数量，不区分一作或通讯作者，不凭作者名猜测机构。
- 已核实机构但未命中名单：分值为 0，默认低于 0.6 门槛，会过滤。报告审计文件标记 `unlisted`，便于发现漏收机构。
- 机构资料无法核实、缺少作者对应关系或来源：标记 `unknown`，默认保留但无机构加分。设 `unknown_policy = "exclude"` 可严格移除。这样可以区分“未进入偏好名单”和“没有取得可靠资料”。
- `min_institution_score` 取值 0–1；提高到 0.8 会移除 0.6 档，设为 0 则不按机构分过滤（未知机构仍由 `unknown_policy` 决定）。
- 同分按相关性、arXiv ID 稳定排序。邮件保留原相关性分，另显示综合排序分、机构分及匹配依据。

默认名单位于 [`institutions.json`](../src/arxiv_digest/data/institutions.json)：

| 分值 | 默认偏好示例 |
| --- | --- |
| 1.0 | Stanford、CMU、MIT、UC Berkeley、清华、北大、上海交大、浙大、ETH Zurich；NVIDIA、Physical Intelligence、Figure、银河通用、Google DeepMind、Boston Dynamics、Unitree |
| 0.8 | Princeton、Harvard、Caltech、Cornell、UPenn、UW、Georgia Tech、UIUC、Michigan、UCSD、UCLA、中科大、复旦、南大、哈工大、同济、中科院、港大、港科大、港中文、NUS、NTU、EPFL、牛津、剑桥、帝国理工、TUM、Delft、Korea University、KAIST、MBZUAI 等；Apple、Microsoft Research、Meta AI、Disney Research、TeleAI、Agility、Apptronik、1X、傅利叶、优必选、智元等 |
| 0.6 | ShanghaiTech、University of Nevada Reno、ROBOTIS |

名单不穷尽所有优秀机构；尤其“未列入”仅表示当前规则未收录。建议根据实际研究偏好增补，不必照搬分档。

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

`digest.json` 的每篇记录含 `ranking`；同目录 `ranking-audit.json` 记录所有通过相关性门槛的候选、机构匹配、是否通过机构过滤、是否入选，便于检查被移除的论文。切换权重或名单会重新考虑未发送论文，复用原 Jev 判断与摘要缓存；已发送论文不会重发。

## 实现参考

开发前检索了 GitHub 的 arXiv 排序与机构匹配实现，包括 [ROR affiliation matching experimental](https://github.com/ror-community/affiliation-matching-experimental) 和 [affiliation-matching](https://github.com/mkallipo/affiliation-matching)。本项目的需求是少量可编辑偏好规则，并包含具身机器人公司，采用可审计的本地别名匹配，未引入通用机构消歧模型或外部排名服务。

## 2026-10-24 验证

本地 100 项测试通过，新增覆盖了相关性先行、机构低分过滤、Top K 之前重排、机构别名误匹配、缺失来源和作者映射、未知机构策略、缓存兼容、权重修改与邮件去重。

另外离线复用了上次已发送的 8 篇真实论文及其摘要、相关性分和核实机构，未重新筛选完整 69 篇，也未重发邮件。8 篇均达到当前 60 分机构门槛；LocoManiLab 与 Humanoid Mobility 的机构分为 100，综合分同为 98.4；PhysibleMDM 凭 MIT 署名获得机构分 100，综合分 93.6，排在 MPBRS（机构分 60，综合分 89.6）之前。同济 Physical Intelligence Lab 仅匹配同济（80），没有误记为 PI 公司。
