# 微信信息管理系统 · 部署指南（脱敏版）

> 本项目为**脱敏版**：已移除原作者的学校/校区/群名/人名/微信 ID/webhook 等全部私人信息（见文末「脱敏说明」）。
> 使用者需自己抓微信 DB key（各机器密钥独立），并按本指南替换占位符。

## 一、环境要求

- Windows 10/11 + Python 3（无需管理员权限）
- 微信 **4.x** 电脑版（3.x 不适用），每天开机登录
- 一个命令行智能体（自动探测，任选其一）：Claude Code（默认；自动找 VSCode 扩展里的 claude.exe，回退 PATH 里的 `claude`）/ Gemini CLI / Codex CLI / Cursor Agent / Aider / opencode——想固定用哪个或接任意 CLI，见常见问题「用别的智能体」
- Obsidian（可选，线二知识库用；`winget install Obsidian.Obsidian` 装）
- ffmpeg（可选，录音转 m4a 用）

```powershell
python -m pip install frida pycryptodome win11toast requests keyboard sounddevice numpy faster-whisper zstandard
python -c "import frida, Crypto, win11toast, requests, keyboard, sounddevice, numpy, faster_whisper, zstandard; print('依赖 OK')"
```

> **多版本 Python 的机器注意**：用 `python -m pip` 保证依赖装进 `python` 命令对应的解释器；装完 `where python` 记下它的完整路径，第 5 步计划任务里直接填完整路径——防 PATH 里别的 python 抢位报 `ModuleNotFoundError`。一键部署会把所选解释器额外记到 `scripts\python_path.txt`，分析班次（`run_analysis.bat`）优先用它并把其目录前置进 PATH——机器没勾 Add to PATH 也不影响班次。

## 二、安装步骤

### 0. 一键部署（推荐）

双击根目录的 **`一键部署.bat`**，向导自动完成全部部署：

1. 自检环境 → 列出本机所有 Python 供选择（推荐 3.10+）
2. 探测微信安装目录 / 版本 / 账号数据目录
3. `pip` 装依赖（失败自动切清华镜像重试）
4. 生成 config.json、分类规则.md、行程表.md、kanban\data.json，自动替换全部占位符（wxid、微信路径、`<SKILL_DIR>` 等）
5. **自动抓 DB 密钥**（按版本选路线）：
   - 微信 ≤ 4.1.13：Frida 路线——向导会提示先完全退出微信，然后自动走 spawn → 登录 → 验证全流程
   - 微信 ≥ 4.1.14：wcdb 只读扫描——微信保持登录，会弹一次 UAC（向导自动提权，工具只读、不注入）
6. 验证解密 + 首次导出
7. 启动看板 + **桌面生成「微信看板」快捷方式**（以后双击即开）
8. 注册 7 个 Windows 计划任务，最后打印 Claude Cron 说明（仅 Claude Code）——包内已附 `CLAUDE.md`，在该目录打开 Claude Code 即自动创建这 4 个 Cron（也可复制那段文本发给它手动建；用其它智能体靠计划任务即可）

> 向导可反复运行：已完成的步骤秒过；整个文件夹搬到新路径后重跑一次会自动修正所有旧路径。
> 跑完还剩两件非自动化小事：按你的场景改 config.json 的公众号/群聊名单；改 分类规则.md 的 ⚙️ 标注处。

> 以下 1~8 为手工步骤，仅当一键部署失败、或想逐步理解时参考。

### 1. 复制到安装目录

把本项目文件夹（克隆仓库或解压 ZIP）放到你的工作目录，如 `D:\wechat-assistant`（文件夹名随意）。

### 2. 填写配置

```powershell
copy config.example.json config.json        # 然后按 config.json 里的注释填写：
                                            # 飞书webhook、群聊名单、公众号名单、
                                            # 忽略校区、vault目录、whisper模型目录、
                                            # 审核期结束日（建议设为今天+3天）
copy 分类规则模板.md 分类规则.md             # 按你的场景改写（所有 ⚙️ 标注处）
copy 行程表模板.md 行程表.md
copy kanban\data.example.json kanban\data.json
```

> 可选：看板「通知」区会按群名自动分组（💰 赚钱相关 / 📢 班群通知…）。分组匹配词在 `kanban\index.html`（搜 `⚙️`）——按你的群名改；不改则通知统一归入 📌 其他，不影响其他功能。

### 3. 替换占位符

把 `<SKILL_DIR>` 全部替换成你的安装路径（出现在 `prompts\*.md` 和根目录 `CLAUDE.md`）：

```powershell
$dir = "D:\wechat-assistant"
Get-ChildItem -Path "$dir\prompts\*.md", "$dir\CLAUDE.md" -File | ForEach-Object {
    $c = [System.IO.File]::ReadAllText($_.FullName)
    [System.IO.File]::WriteAllText($_.FullName, $c.Replace('<SKILL_DIR>', $dir), [System.Text.UTF8Encoding]::new($false))
}
```

> 脚本（scripts/）不需要改路径——全部按自身文件位置自动定位（`__file__` 推导），包放哪都能跑。

### 4. wechat-export 抓 key（一次性，约 15 分钟）

先看微信版本（设置 → 关于微信）：

- **微信 ≤ 4.1.13.x**：见 `wechat-export\接手说明.md` 的 Frida 流程：
  1. 全局搜索替换你的 wxid 目录名 / 微信安装路径 / 微信版本号（文档里有对照表；一键部署已自动完成；锚点表 `anchors_focus.json` 随包携带，放在 wechat-export\ 内勿动）
  2. 完全退出微信 → `python phase2e_login.py`（spawn 微信 + 扫码登录 + 观察 120 秒）→ `python phase3_verify3.py`（离线验证出 db_key.json）
- **微信 4.1.14+**：Frida 链未适配新版，改用 wcdb-key-tool（**已内置** `wechat-export\tools\wcdb_key_tool_windows.py`，MIT 协议，来源 github.com/TANGandXue/wcdb-key-tool；纯只读扫描、不注入、无需装依赖）：
  1. 微信保持登录，管理员终端跑 `python tools\wcdb_key_tool_windows.py extract` → 产出 `all_keys.json`
  2. `python import_dbkey.py all_keys.json` → 生成 db_key.json（后续流程完全一致）
  3. 兜底：DbkeyHook 的 DbkeyHookCMD exe（不注入 DLL），或把微信降级到 4.1.13 走 Frida 流程（key 跨版本/跨重登不变，抓到后升回新版仍有效）

收尾（两种版本通用）：`python decrypt_db.py --count` 验证解密 → `python daily_export.py` 全量导出；图片归档另跑 `python find_image_key.py`（先点开 2-3 张聊天图片）

### 5. 配置 Windows 计划任务（机器活）

| 时间 | 动作 | 命令 |
|------|------|------|
| 12:00 | 增量导出 | `python -u daily_export.py`（工作目录 `wechat-export`） |
| 18:00 | 增量导出 | 同上 |
| 22:00 | 增量导出 | 同上 |
| 12:10 | 午班分析 | `scripts\run_analysis.bat noon` |
| 18:10 | 晚班分析 | `scripts\run_analysis.bat evening` |
| 21:00 | 行程提醒 | `scripts\run_analysis.bat trip` |
| 22:10 | 日报班 | `scripts\run_analysis.bat daily` |

PowerShell 示例（注意别用 schtasks 包 cmd，Win11 上解析会失败）。`$py` 填上面查到的解释器完整路径，`$dir` 填你的安装目录：

```powershell
$py  = "C:\Path\To\python.exe"        # where python 查到、装了依赖的那个
$dir = "D:\wechat-assistant"

# 导出三班（12:00 / 18:00 / 22:00）
foreach ($t in @("12:00","18:00","22:00")) {
    $a = New-ScheduledTaskAction -Execute $py -Argument "-u daily_export.py" -WorkingDirectory "$dir\wechat-export"
    Register-ScheduledTask -TaskName ("WeChatExport_" + $t.Replace(":","")) -Action $a -Trigger (New-ScheduledTaskTrigger -Daily -At $t) -Force
}

# 分析四班（12:10 午 / 18:10 晚 / 21:00 行程 / 22:10 日报）
foreach ($j in @(@("1210","noon"),@("1810","evening"),@("2100","trip"),@("2210","daily"))) {
    $a = New-ScheduledTaskAction -Execute "cmd.exe" -Argument ('/c "' + $dir + '\scripts\run_analysis.bat" ' + $j[1])
    $h = $j[0].Substring(0,2) + ":" + $j[0].Substring(2,2)
    Register-ScheduledTask -TaskName ("WeChatAnalysis_" + $j[0]) -Action $a -Trigger (New-ScheduledTaskTrigger -Daily -At $h) -Force
}
```

### 6. 配置 Claude Cron（脑子活，仅 Claude Code）

> 会话级 Cron 是 Claude Code 独有的机制；用 Gemini CLI / Codex CLI 等其它智能体时**本节整节跳过**——计划任务（机器活）已覆盖全部 4 个班次。

包内已附 `CLAUDE.md`——在该目录打开 Claude Code 的新会话时，它会**自动检查并创建下面这 4 个 Cron**（会话级，Claude 关闭即失效，每次新会话自动检查重建），无需手动粘贴；也可把文末那段直接发给 Claude 手动建（等效）：

| Cron 表达式 | 班次 | 干什么 |
|------------|------|--------|
| `10 12 * * *` | 午班 | 按 prompts/prompt_noon.md 流程：分析→核对→入库→推送 |
| `10 18 * * *` | 晚班 | 同午班 |
| `0 21 * * *` | 行程 | 按 prompts/prompt_trip.md：明天+未来3天行程提醒 |
| `10 22 * * *` | 日报 | 按 prompts/prompt_daily.md：日报五件套+复盘底稿 |

（备选）把下面这段直接发给 Claude 让它建（路径换成你的安装目录）：

```text
请建 4 个会话级 Cron：
10 12 * * * → 读 <安装目录>\prompts\prompt_noon.md 并严格按其流程执行
10 18 * * * → 读 <安装目录>\prompts\prompt_evening.md 并严格按其流程执行
0 21 * * * → 读 <安装目录>\prompts\prompt_trip.md 并严格按其流程执行
10 22 * * * → 读 <安装目录>\prompts\prompt_daily.md 并严格按其流程执行
并且每次新会话开始时检查这 4 个 Cron 是否还在，缺失就重建。
```

> 计划任务（机器活）与 Cron（脑子活）都跑同一份 prompt 文件，靠 `analysis_watermark.json` 水位防重复，同一班次只跑一个。

### 7. 首次审核期（前 3 天）

- 审核期内所有新信息先出「待审清单」给你过审（对话里回复"全过 / 删N号 / N号改XXX"）
- 每次纠正都会被写进 分类规则.md，AI 判断标准越跑越准
- 3 天后全自动模式：班次核对通过直接入库，你随时抽查

### 8. 日常使用

- 开看板：双击桌面「微信看板」快捷方式（一键部署自动创建；或 `scripts\打开看板.bat`、`python scripts\open_kanban.py`）；**不要直接双击 index.html**（file:// 下浏览器读不到 data.json）
- 录音：全局热键 `Ctrl+Alt+R` 或看板按钮，停止后自动转文字 + 入库复盘
- 看板由 recorder.py 常驻进程提供本地服务（127.0.0.1:8710），挂了就重开

## 三、脱敏说明

本脱敏版已做以下处理：

| 类别 | 处理方式 |
|------|---------|
| 飞书 webhook | 换成 `REPLACE_WITH_YOUR_WEBHOOK` 占位符 |
| 微信 wxid / Windows 用户名 | 换成 `wxid_YOURWXID` / `YOURNAME` 占位符 |
| 全部群聊名单（17 个） | 换成「示例群A/B…」占位 |
| 公众号名单 | 换成示例名称 |
| 学校名 / 校区名 | 从代码和文档中移除，改为 config 可配置项（忽略校区） |
| 群名相关的看板过滤规则 | 换成通用关键词 + 注释 |
| 人名 / 工资 / 课程等私人内容 | 从分类规则中全部移除 |
| 看板数据（data.json）/ 待审 / 日志 | 不打包，只给空的 data.example.json |
| 磁盘绝对路径 | 脚本改为 `__file__` 自动定位；文档用 `<SKILL_DIR>` 占位 |

## 四、常见问题

- **计划任务每天返回码 1、日志不写**：大概率是用 `schtasks /TR` 包了 cmd；用 PowerShell 的 Register-ScheduledTask 重建
- **.ps1 一跑就报 `The string is missing the terminator` 之类解析错误 / 中文乱码 / 计划任务一直返回码 1**：脚本是 UTF-8 **不带 BOM** 存的（多数编辑器、AI 生成的默认如此），Windows PowerShell 5.1（计划任务里直接写 `powershell` 用它）会按系统 ANSI 代码页（中文系统 = GBK）解码——中文注释/字符串的字节被拆错，引号配对失败，整个脚本解析都过不去（现场：录音保活脚本 `recorder_keepalive.ps1` 的每小时计划任务每次都退 1）。解法：把 .ps1 一律转存为 **UTF-8 with BOM + CRLF**：
  ```powershell
  $p = "D:\wechat-assistant\recorder_keepalive.ps1"   # 换成实际路径
  $t = [IO.File]::ReadAllText($p, [Text.Encoding]::UTF8)
  [IO.File]::WriteAllText($p, ($t -replace "`r?`n", "`r`n"), (New-Object Text.UTF8Encoding $true))
  ```
  ⚠️ 别和 .bat 搞混：批处理要 **GBK + CRLF**，PowerShell 要 **UTF-8 with BOM + CRLF**——两类脚本在 `.gitattributes` 里都禁止 Git 转换行尾。PowerShell 7（`pwsh`）默认按 UTF-8 读、不中招，踩坑的只有 5.1
- **`ModuleNotFoundError: No module named 'Crypto'`（或 frida/zstandard 等）**：依赖装到了另一个 Python。`python -m pip install ...` 重装，并把计划任务/命令里的解释器换成装了依赖那个的完整路径（`where python` 查）
- **班次日志出现 `'python' 不是内部或外部命令` 或 exit=9009**：本机 Python 没进 PATH（安装时没勾 Add python.exe to PATH）。一键部署会自动把所选解释器写进 `scripts\python_path.txt`，`run_analysis.bat` 优先用它、并把它前置进 PATH（智能体在班次里跑 `python verify_entries.py` 等命令也因此可用）；手工部署补一个同名文件、内容一行解释器完整路径即可，或把 Python 加进 PATH
- **用别的智能体（Gemini CLI / Codex CLI / Cursor Agent / Aider / opencode 等）**：headless 班次的「大脑」不绑死 Claude Code。想固定用哪个，在 config.json 顶层加「智能体」节，例如：
  ```json
  "智能体": { "名称": "Gemini CLI", "命令": "gemini", "参数": ["--yolo"] }
  ```
  - 「命令」填命令名（走 PATH）或 exe 绝对路径（含路径分隔符即按路径找）；「名称」只影响日志显示
  - 提示词怎么喂看「参数」：**不含占位符 = 从标准输入喂**（对 npm 装的 `.cmd` 外壳最稳）；含 `{prompt}` = 提示词内容当参数；含 `{prompt_file}` = 提示词文件绝对路径当参数（适合支持读文件的 CLI，如 aider）
  - 「参数」不写就用内置预设；各家 CLI 参数随版本可能变，跑不通看 analysis.log 里的报错再调
  - 限制：npm 装的 CLI 在 Windows 上是 `.cmd` 外壳，多行提示词没法当命令行参数传（系统限制，遇到会在日志里明确报错并给替代方案），这类 CLI 用 stdin 方式
  - 会话级 Cron 是 Claude Code 独有机制；用其它智能体无需理会第 6 节，计划任务已覆盖全部 4 个班次
- **analysis.log 里写「没探测到任何命令行智能体」**：headless 班次需要一个命令行智能体，脚本按顺序自动探测本机可用的（claude → gemini → codex → cursor-agent → aider → opencode，用第一个找到的）；一个都没装就先装一个并登录，或用上一条在 config.json「智能体」节显式指定
- **看板打不开**：用 `scripts\打开看板.bat`（或 `python scripts\open_kanban.py`）打开——会自动保活 recorder；仍不行再 `python scripts\recorder.py` 手动拉起
- **密钥失效 / 换机器**：重跑 wechat-export 第一步（两机密钥独立）；微信 4.1.14+ 机型按接手说明里的 wcdb-key-tool 路线抓
- **WAL 滞后**：微信 WAL 环形复用不合并，主库快照可能滞后几条消息，次日 checkpoint 自动补齐
- **公众号白名单一条都匹配不上 / 名字显示成 gh_xxx**：**别去解析推送 XML 里的 nickname**——微信 4.x 那些中文字段是 GBK 字节、且多数条目根本没有该字段，解析必然失败（乱码 → fallback 成 ID → 名单按中文名比对全不中）。正确做法是走 contact 库的名字映射：跑 `python wechat-export\export_biz.py`（独立公众号导出工具，名单读 config.json「公众号」，产物 `export\<日期>\biz_articles.json`）。输出里 `contact 库: N 个名字映射` 若为 0，说明 db_key.json 没覆盖 contact 库，按第 4 步补抓
- **班次失败了会通知吗**：会。智能体自动重试（隔 60 秒）仍失败时推一条飞书告警（`⚠ 微信班次失败`，带班次名和退出码），排查看 `analysis.log` 尾部；成功和"没新消息"都不打扰。链路自测：`python scripts\notify_fail.py noon 5 --dry`（只打印；去掉 `--dry` 会真推一条）
- **headless 班次没跑**：先看 analysis.log 尾部报错；走 Claude Code 时再检查会话级 Cron 是否存活（会话一关就死，让 Claude 每次新会话开始时检查重建），用其它智能体无此机制、靠计划任务即可
- **headless 班次日志写 ANTHROPIC_* MISSING 或认证失败（仅 Claude Code 通道）**：headless 用 Claude Code 登录态或环境变量。官方订阅登录即可用；第三方 API 端点把 `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_MODEL` 配在系统环境变量里，或 VSCode settings.json 的 `claude-code.environmentVariables`（`scripts\load_env.py` 会自动读取注入）

## 五、参与贡献

欢迎 Issue 和 PR——其它智能体的适配、新场景的 prompt、踩坑修复都收。

- **提 Issue**：用仓库的模板，带上环境信息（微信版本 / Python 完整路径 / 用的智能体 / 卡在哪一步）；**别贴真实群名、wxid、聊天记录、密钥**，脱敏后粘贴
- **提 PR**：fork → 改动 → 按模板自查 → 开 PR。红线是**脱敏**（示例一律用占位符）和**脚本编码**（bat=GBK+CRLF、ps1=UTF-8 BOM+CRLF）；细则见 [CONTRIBUTING.md](CONTRIBUTING.md)
- **协议**：MIT（见 [LICENSE](LICENSE)）——随便用、随便改，保留版权声明即可
