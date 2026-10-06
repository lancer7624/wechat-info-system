# 沟通技能来源

核对日期：2026-10-02。版本：`communication/1.0.1`。

本项目将下列开源技能的方法改编为微信短消息规则。规则在 `scripts/communication_skills.py` 独立实现，配合本项目的个人口吻模块与原有回复规则使用；没有直接运行上游插件，没有新增 Claude、PACK 或 Jev 服务依赖。本文明确记录了改编，来源作者不为本项目背书。

## 本人表达方式

- 项目：[Percona-Lab/MYNAH](https://github.com/Percona-Lab/MYNAH)。固定提交：`470c63b7ca0a3a66477c3072ab8680d19cb11371`。
- 核实文件：`skills/mynah/SKILL.md`、`skills/mynah/references/style-dimensions.md`。
- 借鉴范围：从本人重复出现的句长、语气、标点、幽默与表达方式提取紧凑档案，并按当前场合使用。原有 Claude/PACK 存储流程和连接器采集流程未引入；不把样本事实当作当前会话事实。
- 许可：MIT，原文保存在 [LICENSE.MYNAH](LICENSE.MYNAH)。

## 职场与客户

- 项目：[anthropics/knowledge-work-plugins](https://github.com/anthropics/knowledge-work-plugins)。固定提交：`8444efcd48f7012f09797778a36a33e73d0861f4`。
- `product-management/skills/stakeholder-update/SKILL.md`：借鉴按接收者需要表达进展、影响、阻碍及具体确认点；压缩为微信短消息，不套周报格式，也不额外搜索工作平台。
- `customer-support/skills/draft-response/SKILL.md`：借鉴回应具体问题、控制承诺范围、保持先前沟通一致，并按聊天渠道缩短表达；不导入 CRM、邮件或工单工具，不采用固定催办节奏。
- 许可：两个目标插件均为 Apache-2.0，原文分别保存在 [LICENSE.anthropic-product-management](LICENSE.anthropic-product-management) 与 [LICENSE.anthropic-customer-support](LICENSE.anthropic-customer-support)。已核对该固定提交完整树，未发现 `NOTICE` 文件。
- 改动说明：本项目仅保留上述沟通方法，重写中文规则、输入选择和本地场景路由；原上游技能没有逐字整体复制到运行时。

## 中文自然表达

- 项目：[op7418/Humanizer-zh](https://github.com/op7418/Humanizer-zh)。固定提交：`f4518a8eab97b8bfebc66a89d34320a89bef6930`。
- 核实文件：`SKILL.md`，其自身修订标记为 2026-09-23。
- 借鉴范围：先保留事实、归因、否定与确定程度，再修改空话和重复；尊重作者声音，不机械禁用正常词语和标点。没有移入长文示例、检测承诺或文件修改工具流程。
- 许可：MIT，原文保存在 [LICENSE.Humanizer-zh](LICENSE.Humanizer-zh)。

## 自然接话与情感沟通

沿用本项目已有的狗头军师固定来源：`shengjidaguai-china/goutoujunshi-jev-chat`，提交 `d19fd84305b5baa2f4b0f1147f177b163fef70a0`。其自然接话、单一主动作、按情绪收放与停止边界已在 [原有来源说明](../chat-reply/ORIGIN.md) 记录，完整 [MIT 许可](../chat-reply/LICENSE.goutoujunshi) 继续随项目分发。

本次依用户要求增加情感场景，将接梗、暧昧、邀约、安慰和修复写成独立规则；明确场景不等于关系认定，拒绝后停止推进，不新增 MBTI 问卷、关系打分、操控或施压方法。不声称已安装或改编此前提及但本次未核实的 `coach-skill`。

`communication/1.0.1` 根据虚构安慰场景的模型输出补充校准：允许正常关心，但持续待命和陪伴承诺需要本人明确愿意表达，不能当作所有安慰的默认收尾。此改动由本项目自行编写，上游固定来源与许可保持原样。

## 许可证文件校验

以下 SHA-256 对应从固定提交下载并原样保存的文件：

| 本地文件 | SHA-256 |
|---|---|
| LICENSE.MYNAH | `8878a715ccb5b30875878a78b80d01b279f011fb62a43b42b7699e90b8d62007` |
| LICENSE.anthropic-product-management | `cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30` |
| LICENSE.anthropic-customer-support | `cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30` |
| LICENSE.Humanizer-zh | `aa00e74769e1b9d8e7fa7094dbfcca9b129a0ded6dce1cf4da050b99146d2fa7` |
