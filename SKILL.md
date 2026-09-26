---
name: wechat-assistant
description: 微信信息管理系统（分享版）——爬取微信本地群聊/公众号数据，定时 AI 分析分流，重要信息桌面 toast + 飞书手机推送，HTML 看板 + Obsidian 知识库 + 语音复盘。全本地零服务器。当用户要搭建微信消息监控、群聊爬取、定期提醒、个人日报/信息看板时使用。
---

# 微信信息管理系统（分享版）

> 轻量脚本 + Claude 后台运作：机器活归脚本，脑子活归 Claude，用户只看（看板）、只写（复盘）。
> 零 API 费用（AI 判断可接任意 Claude 会话）、零服务器、零数据库，全本地运行。

## 一、总架构

两条线 + 一个闭环：

| 线 | 干什么 | 产出到哪 |
|---|---|---|
| 线一 · 信息活动提醒线 | 爬群聊/公众号 → AI 判断 → 活动通知 + 行程安排 + 日报 | toast / 看板 / 手机（仅活动提醒） |
| 线二 · 知识库线 | 公众号推文自动入库 + 指定群聊有价值信息确认后入库 | Obsidian vault |
| 睡前复盘闭环 | 用户手写/口述 + AI 复盘 + 录音 | 看板 + Obsidian 每日复盘 |

```
原料：微信本地库（群聊 + 订阅号推送记录）
工具：Python + Windows 计划任务 + Claude Cron + toast + HTML 看板 + Obsidian + whisper（本地转文字）
```

## 二、目录结构

```
wechat-assistant-skill/
├── SKILL.md              ← 本文件（技能入口）
├── README.md             ← 部署指南（先看这个）
├── config.example.json   ← 配置模板（复制为 config.json 后填写）
├── 分类规则模板.md        ← 分类规则（复制为 分类规则.md 后按你的场景改写）
├── 行程表模板.md          ← 行程表（复制为 行程表.md 启用）
├── prompts/              ← 四个 headless 班次的提示词
│   ├── prompt_noon.md    ←   午班分析（12:10）
│   ├── prompt_evening.md ←   晚班分析（18:10）
│   ├── prompt_trip.md    ←   行程提醒（21:00）
│   └── prompt_daily.md   ←   日报班（22:10）
├── scripts/              ← 机器活脚本
│   ├── feishu_notify.py  ←   飞书群机器人推送（webhook 只存 config.json）
│   ├── feishu_push.py    ←   推送命令行封装
│   ├── verify_entries.py ←   写看板前的核对闸门（数据核实铁律执行器）
│   ├── recorder.py       ←   看板静态服务 + 录音热键（Ctrl+Alt+R）+ whisper 转录
│   ├── toast.ps1         ←   桌面气泡提示
│   ├── load_env.py       ←   从 VSCode settings 读 Claude 环境变量
│   ├── run_analysis.bat  ←   headless 班次入口（claude.exe -p 无人值守）
│   ├── open_kanban.py    ←   一键开看板（保活 recorder + 开浏览器）
│   ├── 打开看板.bat       ←   开看板双击入口（调 open_kanban.py）
│   └── fetch_article.py  ←   公众号正文抓取（直连 + 搜狗旁路），按名单强制拦截
├── kanban/
│   ├── index.html        ←   看板页面（零 CDN 离线可用）
│   └── data.example.json ←   数据接口样例（复制为 data.json）
└── wechat-export/        ← 依赖：微信本地库解密导出（见其 接手说明.md）
```

## 三、数据流

```
微信登录 → 一次性抓 master key：≤4.1.13 用 Frida、4.1.14+ 用 wcdb-key-tool（wechat-export，见子目录接手说明）
    ↓（此后完全离线）
每日 12:00/18:00/22:00 计划任务：快照 → 解密 → 导出 Markdown → export\YYYY-MM-DD\
    ↓
Claude Cron 班次（错峰 10 分钟）读增量产物做 AI 判断：
    ├─ 📢 时效性重要（活动/截止/改时间/@我）→ toast + 飞书推送
    ├─ 📚 知识性内容 → 线二知识库
    └─ 普通消息 → 当日简报，日报汇总
    ↓
核对闸门 verify_entries.py（逐条溯源原文）→ 通过才写看板 data.json
    ↓
看板（本地 HTTP 服务）展示；日报五件套 22:10 生成；行程 21:00 汇总提醒
```

## 四、时间轴总览

| 时间 | 动作 |
|------|------|
| 12:00 / 18:00 / 22:00 | Windows 计划任务增量导出（wechat-export） |
| 12:10 / 18:10 | Claude 分析班次 → 重要信息 toast + 飞书通知 |
| 21:00 | 行程汇总提醒（明天安排 + 未来 3 天） |
| 22:10 | 日报班：通知 + 日报五件套 + AI 复盘底稿 + 公众号入库 + 知识候选清单 |
| 睡前 | 用户看日报写复盘 / 按录音键（Ctrl+Alt+R）口述 |
| 录音停止后 | 自动：看板挂音频 + whisper 转文字 + 转录入库 |
| 全天 | wechat-export 的 activity_check 关键词兜底 toast（不依赖 Claude 会话存活） |

## 五、Claude 班次职责（prompts/ 目录，无人值守）

每个班次由 Windows 计划任务触发 `run_analysis.bat <班次>`（另有 Claude Cron 通道可选，两套共享 `analysis_watermark.json` 水位防重复），用 `claude.exe -p` headless 跑对应 prompt。班次硬性纪律：

1. **先读 分类规则.md + config.json + 水位文件，再干**
2. **分析窗口按水位**：watermark.date ≠ 今天 → 分析今天全部；= 今天 → 只分析时间戳 > watermark.last_ts 的消息
3. **数据核实铁律（最高优先级）**：time / source / quotes 必须回当天导出原件 grep 逐字核实，禁止凭记忆；找不到原文的条目删除（宁缺勿假）
4. **核对闸门**：产出先写 `待审\<日期>_班次待审.json` → 跑 `scripts\verify_entries.py` 核对 → 无 FAIL 才允许合并 data.json / 推送飞书；WARN 逐条处理
5. **审核期**（config.json「审核期结束日」之前）：待审 json 保留等用户过审；之后：核对通过直接入库
6. **无新消息**：只更新水位，不写文件不推送
7. **禁区**：不修改 分类规则.md / index.html / config.json / wechat-export 内任何文件；不创建/删除计划任务与 Cron；密钥不进任何输出

## 六、数据核实铁律（通用方法论，各系统可复用）

- 每条写进看板的数据，time、source、quotes 三字段必须回原始导出文件逐字核实
- 引号里只放原文，禁止把转述内容放进引号
- 归因正确：quote 必须在 source 对应的群里；跨群聚合要注明实际来源
- 时间一致：条目 time 必须等于原文时间戳；行程/活动必须写 HH:MM，查不到标「具体时间不明」，禁止"白天/晚上"等模糊词
- 日期一致：消息必须挂在它实际发生的那天
- 不知道就标注不明：校区、地点、主题等原文没写的，标注「XX不明」，禁止猜测编造
- `verify_entries.py` 是这套铁律的自动执行器：FAIL 拦截写入/推送，核对报告自动留存

## 七、安全纪律

1. **封号安全铁律**（微信大号专用，见 wechat-export）：Frida 只读 hook 编解码函数，绝不碰消息收发路径；命中即 detach；日常导出完全离线
2. **密钥纪律**：飞书 webhook 只存 config.json，不进日志、不进聊天、不进 git；db_key.json / image_key.json / export 目录均不外传
3. **数据全本地**：聊天记录、看板数据、知识库都在本机，不上传任何服务器

## 八、部署

完整步骤见 [README.md](README.md)。**推荐直接双击根目录 `一键部署.bat`**——自动装依赖、替换占位符、抓密钥、生成桌面快捷方式、注册计划任务。手工要点：改 config → 替换 `<SKILL_DIR>` 占位符 → 装依赖 → wechat-export 抓 key → 配 Windows 计划任务（导出 3 班 + 4 个分析班次）+ 4 个 Claude Cron → 前 3 天审核期调优。
