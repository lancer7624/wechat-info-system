# 实现来源与整理范围

2026-10-05 用户提供 [lancer7624/wechat-info-system](https://github.com/lancer7624/wechat-info-system) 的公开来源后，已下载固定提交 `056187e9b55cb9181200e0911baab1e2480531d2` 并建立独立本地对照分支。三份图片相关源文件与之前收到的图片包逐字节一致，其公开版本采用 MIT 许可，完整许可保存在 `docs/licenses/lancer7624-wechat-info-system.txt`。这一结论不扩大到包内其他文件或整个本项目。范围与复用脚本见 [上游对照说明](docs/upstream-reference.md)。

2026-10-05 新增 C# / WPF 对照宿主，将本项目现有的保守会话定位、草稿核对和单次发送约束迁移为 C#，通过进程间标准输入输出协议复用原 Python 业务模块。Windows 交互使用 [FlaUI 5.0.0](https://github.com/FlaUI/FlaUI/tree/v5.0.0) 的 UIA3 封装，遵循 MIT 许可；界面宿主使用 [Microsoft.Web.WebView2 1.0.4258.31](https://www.nuget.org/packages/Microsoft.Web.WebView2/1.0.4258.31) 及其 SDK 许可。FlaUI、Interop.UIAutomationClient、System.Management 和 WebView2 的许可与包元数据随 C# 发布目录的 `licenses` 分发。项目没有复制 Kimi 源码；Kimi 官方 Python → Node.js 迁移说明仅作为技术选型案例。

本项目从本机已有的微信相关工作区中按功能白名单提取，用于独立维护聊天分析流程。

- 导出、接入检查、只读密钥读取及离线测试：来自原工作区中已经验证和迭代的对应实现；调整了目录组织、入口和个人数据路径。
- 数据库解密：保留原 wechat-export/decrypt_db.py 中已经验证的 AES-CBC、PBKDF2 和页面 MAC 逻辑，纯净版只提供主库解密，不带 WAL 合并、调试转储和旧版命令入口。
- WCDB 配置定位方法：原实现注明参考 fanyuantaier/wechatauto-replica 的 wechatauto/db.py。保留代码中的参考注释及公开混淆掩码说明；此掩码不是用户凭据。
- 第三方 Python 库：PyCryptodome、zstandard、psutil；版本见 requirements.txt，各自许可随依赖安装保留。
- 看板字体：从 Google Fonts 官方仓库下载 Noto Serif SC 可变字体，用 fontTools 转成 WOFF2，保留完整字库和字形。字体采用 SIL Open Font License 1.1，完整授权见 dashboard/fonts/OFL.txt；生成的 HTML 同时保留授权说明。转换工具只用于构建，不是项目运行依赖。
- 桌面宿主：pywebview、uiautomation 和 PyInstaller，固定版本见 requirements-desktop.txt；pywebview 通过已安装的 Microsoft WebView2 Runtime 展示界面。打包保留字体及依赖自带许可文件，不包含个人配置、数据库或聊天。
- 内置沟通 skill：改编 shengjidaguai-china/goutoujunshi-jev-chat 的实战话术规则及 JevChat-Windows 的本人短句采样思路，保留用户目标优先、自然表达和证据边界。精简为项目自带的 `skills/chat-reply`，由 `scripts/reply_assistant.py` 的版本化系统提示词执行；不安装上游整套 skill，不连接 Jev 服务。固定提交、改编范围和两份完整 MIT 许可见 `skills/chat-reply/ORIGIN.md`。
- 个人口吻与分场景沟通：2026-10-02 核实 MYNAH 提取表达维度的方法、Anthropic 的 stakeholder-update / draft-response 沟通流程及 Humanizer-zh 保留含义的中文编辑方法，独立实现 `scripts/personal_style.py`、`scripts/communication_skills.py` 并接入原有回复调用。固定提交分别为 `470c63b7ca0a3a66477c3072ab8680d19cb11371`、`8444efcd48f7012f09797778a36a33e73d0861f4` 和 `f4518a8eab97b8bfebc66a89d34320a89bef6930`。完整 MIT 与两个目标插件的 Apache-2.0 许可证、核实路径、改编范围及 SHA-256 均见 [新增技能来源](skills/communication/ORIGIN.md)。该 Anthropic 提交树中无 NOTICE。未引入上游 Claude/PACK 存储、平台连接器、自动发送流程或模型服务。
- 联系人记忆与已发送改稿：2026-10-02 按本项目验证过的导出、稳定 UID 和有限桌面接口，独立实现 `scripts/contact_memory.py`。记忆按账号和会话保存有界原文与本人偏好；最终改稿仅在成功填入后，明确匹配到已落盘的本人新消息时学习。检索与核对在本机完成，不使用外部向量服务或关系评分；历史片段只随按需回复送入现有模型。具体范围见 [会话记忆说明](docs/contact-memory.md)。
- 指定私聊自动聊天：2026-10-02 根据用户选择的自动生成并发送、拿不准时本人接管，独立实现 `scripts/auto_chat.py` 与 `scripts/wechat_auto_send.py`。复用原有上下文、模型、口吻、证据和输入框核对；新增稳定等待、独立否决、进程锁、持久化尝试与本机消息核对，仅调用已识别发送控件的 InvokePattern。没有引入其他聊天平台、机器人服务或发送快捷键。范围及验收限制见 [自动聊天说明](docs/automatic-chat.md)。
- 分享卡片处理：参考用户于 2026-10-01 提供的「微信信息看板_解压缩包.zip」中 `collect_lib.py` / `forward_collect.py` 的 XML 字段识别思路，按本项目稳定 UID、统计与模型上下文重新实现 `scripts/message_content.py`。也参考其对标报告指出的 URL 脱敏边界问题，增加合成回归用例。未复制其采集、部署或媒体转写源文件；具体范围见 `docs/group-project-study.md`。
- 纸笺动效：查阅 simeydotme/pokemon-cards-css 的卡牌交互及 micku7zu/vanilla-tilt.js 的指针倾斜思路，自行实现小幅倾斜、阴影与浅色纸面光泽，未复制源文件、图片或引入这些库。参考范围见 `docs/design-references.md`。
- 信息整理与纠正反馈：2026-10-01 查阅 [lancer7624/wechat-info-system](https://github.com/lancer7624/wechat-info-system) 的 README、SKILL、分类模板、看板与证据检查实现，参考其通知/资料分流及用户纠正后积累偏好的思路。按本项目导出合同、稳定 UID、当前模型接口和本地看板独立实现，未复制上游源文件或提示词，未引入其 CLI 代理、定时推送、飞书配置或采集系统。具体范围见 `docs/information-assistant.md`。

原混合工作区未附带覆盖全部源文件的统一许可文件，本次整理不替原作者新增统一授权，也不宣称这些实现均为重新原创。

2026-10-01 收到群友提供的「微信图片解密包.zip」，作为外部参考审查了离线图片解码、WXGF 转码及密钥提取逻辑。包未附许可证，本轮没有将其源文件复制进运行模块；合成数据复现了 5 个解码问题，检查与后续接入顺序见 [图片解密包检查](docs/image-package-study.md)。收到的密钥按未绑定账号的示例保护，不随源码发布。

没有整包复制原有信息管理模板、导出工作流压缩包、其他账号的密钥备份、聊天快照、历史报告、通知配置或演示数据。

2026-10-02 新增 `scripts/image_codec.py` 与 `scripts/image_media.py`，按格式独立实现本地预览，不复制群友未授权的脚本。核对 V1 固定密钥的 ASCII 字节表示时参考 [ZedeX 的解码实现](https://github.com/ZedeX/weixin-decrypte-script/blob/main/decrypt_dat.py)；附件目录与资源摘要机制参考 [wx-cli resolver](https://github.com/umuo/wx-cli/blob/main/src/attachment/resolver.rs)。本机验证发现当前消息行 `packed_info_data` 的字段 3.4 可以直接关联附件，本项目通过有界 protobuf 解析读取，未采用上游任意字符串扫描、时间不一致回退、资源库密钥获取或进程扫描。protobuf 编码依据 [官方文档](https://protobuf.dev/programming-guides/encoding/)。

图片像素校验使用 [Pillow 12.3.0](https://pypi.org/project/pillow/12.3.0/)，遵循 MIT-CMU 许可；完整许可保存在 `docs/licenses/Pillow.txt`，随源码和桌面资源发布。图片功能仅本地查看，群友提供的密钥继续按未绑定示例处理。
