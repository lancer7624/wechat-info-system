# 沟通规则来源

本项目将以下 MIT 项目的沟通思路改编为通用回复规则，使用现有模型完成一次生成。规则随本项目内置，不在运行时读取参考仓库，不调用 Jev API，也不代表上游作者为本项目背书。

- [shengjidaguai-china/goutoujunshi-jev-chat](https://github.com/shengjidaguai-china/goutoujunshi-jev-chat)，提交 `d19fd84305b5baa2f4b0f1147f177b163fef70a0`。主要参考 `references/practical/实战话术编排器：从一句回复到后续分支.md` 中的一轮一个主动作、本人自然口吻、事实约束与停止边界；不引入其默认恋爱推进、MBTI 问卷或长期关系档案。完整 MIT 许可见 `LICENSE.goutoujunshi`。
- [jev-chat/jev-chat-windows](https://github.com/jev-chat/jev-chat-windows)，`v0.1.12`，提交 `c6d3a10474ce1ff3b7f8f6402b3b5e0e58fad5f1`。参考 `core/draft.py` 的本人短句口吻采样和自然表达方向；采样改为仅限本次授权上下文，以 UID 索引复用已提供正文。未移植其网络客户端、宽松解析器或多阶段 Jev 调用。完整 MIT 许可见 `LICENSE.jev-chat-windows`。

基于 JevChat-Windows（https://github.com/jev-chat/jev-chat-windows）二次开发。本项目保留自身的双版本草稿、严格 JSON 合同、证据 UID 校验、按账号及会话隔离、显式调用授权与 HTTP 请求上限。

上游 `NOTICE` 原样保留为 `NOTICE.jev-chat-windows`，随两份许可一同分发。其中 Qt 等第三方组件的说明属于上游 JevChat-Windows 发行版；本项目未引入这些 Qt 组件。

核对日期：2026-10-01。

`chat-reply/1.1.0` 扩充自然中文接话与幽默校准，仍基于上述狗头军师固定提交：以 `SKILL.md` 和 `references/practical/实战话术编排器：从一句回复到后续分支.md` 的自然口吻、单一动作和真实边界为主；从 `references/practical/巧妙接话技巧：让沟通更流畅的实用指南.md` 提取具体关键词顺接，从 `references/practical/场景感、松弛感与社交校准：从接话到关系推进.md` 提取轻微反差、画面感及按反馈收放，从 `references/practical/废话文学回复指南：轻松应对各类场景.md` 只提取轻松表达需要匹配重要性与场合的原则。未采用其中编造个人经历、没空借口、连续追问或机械共情的旧示例。轻松机灵作为本机用户明确提出的表达偏好，用户本次目标仍优先。四个模块完整进入程序的系统提示词，不增加 Jev 或其他模型调用；上游许可与 NOTICE 保持原样。

2026-10-02 的 `chat-reply/1.3.0` 接入 `communication/1.0.1` 场景规则与 `personal-style/1.0.0` 口吻特征。MYNAH、Anthropic stakeholder-update / draft-response、Humanizer-zh 的固定提交、实际借鉴范围和完整许可证另见 [分场景来源](../communication/ORIGIN.md)。口吻学习独立从本人已导出短文本中提取有限表达特征，使用当前已配置模型及项目自己的账号隔离存储；没有移植 Claude/PACK 记忆后端。情感恋爱作为本次目标或明确选择下的可选场景，不改变原有证据、事实和明确拒绝边界。

同日 `chat-reply/1.4.0` 接入本项目独立实现的 `contact-memory/1.0.0`：按账号和会话保存有界原文、联系人表达偏好，以本地文字匹配检索相关历史，并在核对实际新增的本人消息后参考最终改稿。未引入上游记忆后端、关系评分、Jev 服务或自动发送。具体范围见 [会话记忆说明](../../docs/contact-memory.md)。
