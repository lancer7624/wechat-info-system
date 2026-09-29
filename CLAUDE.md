# 微信信息管理系统 · 会话守则

> 本文件供在该目录打开的 Claude Code 会话自动读取；headless 班次（run_analysis.bat 触发）按 prompts 里的说明跳过本文件。

## 会话开始自检：4 个班次 Cron

在该目录打开 Claude Code 时，用 CronList 检查下列 4 个会话级 Cron 是否存活；缺失或过期就按原表达式立即重建（CronCreate），并告知用户「微信系统 Cron 已重建」。

| 表达式 | 班次 | 执行内容 |
|--------|------|----------|
| `10 12 * * *` | 午班 | 读 `<SKILL_DIR>\prompts\prompt_noon.md` 并严格按其完整流程执行 |
| `10 18 * * *` | 晚班 | 读 `<SKILL_DIR>\prompts\prompt_evening.md` 并严格按其完整流程执行 |
| `0 21 * * *` | 行程 | 读 `<SKILL_DIR>\prompts\prompt_trip.md` 并严格按其流程执行 |
| `10 22 * * *` | 日报 | 读 `<SKILL_DIR>\prompts\prompt_daily.md` 并严格按其完整流程执行 |

须知：

- 会话级 Cron 随会话关闭失效、7 天自动过期，属正常现象——每次新会话重建一遍就行。
- Windows 计划任务（机器线）已覆盖全部 4 个班次；会话 Cron 是第二通道，两套共享 `analysis_watermark.json` 水位防重复，谁先跑谁干活。
- 若上文路径不是本机真实安装路径（占位符没被替换，说明一键部署向导没跑过），先别建 Cron——提醒用户双击 `一键部署.bat`。
- 除上述检查外，别在本目录自动创建/删除计划任务或 Cron；密钥（config.json / db_key.json 等）不进任何输出。
