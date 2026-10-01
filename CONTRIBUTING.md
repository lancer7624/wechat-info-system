# 参与贡献

感谢你愿意动手改进这个项目。Issue 和 PR 都欢迎。

## 提 Issue

- Bug 请用仓库的 **Bug 报告** 模板，带上环境信息：微信版本、Python 版本与**完整路径**（多版本机器这是头号坑）、用的哪个命令行智能体、卡在哪一步
- **先脱敏**：不要粘贴真实群名、公众号名、微信 wxid、Windows 用户名、聊天记录原文、飞书 webhook、任何密钥；日志贴报错段即可
- 提之前先扫一眼 README 的「常见问题」，可能已经有人踩过

## 提 PR

1. Fork 本仓库，在独立分支上改（别直接改 master）
2. 提交信息简明说清**改了什么、为什么**
3. 开 PR 时用模板，把自查清单逐条勾上

## 红线（碰什么都别碰这三条）

1. **隐私脱敏**：本仓库是脱敏版。任何代码、文档、示例、测试数据都不得出现真实群名/公众号名、wxid、Windows 用户名、聊天内容、webhook、密钥、私人绝对路径——一律用占位符：`wxid_YOURWXID`、`YOURNAME`、`<SKILL_DIR>`、`REPLACE_WITH_YOUR_WEBHOOK`
2. **不写死路径**：所有脚本按 `__file__` 自定位；文档里的安装目录用 `<SKILL_DIR>` 占位符
3. **脚本编码**（Windows 硬约束，改之前先读 `.gitattributes` 的注释）：
   - `.bat` / `.cmd`：**GBK + CRLF**（UTF-8 中文在 cmd 下乱码，LF 行尾会让多行块解析错乱）
   - `.ps1`：**UTF-8 with BOM + CRLF**（无 BOM 会被 PowerShell 5.1 按系统 ANSI 解码，中文乱码直接炸脚本）
   - 仓库对三类脚本声明了 `-text`（git 不转换行尾），但编辑器保存时可能悄悄改编码——提交前对比一眼字节

## 开发约定

- **Python**：新增第三方依赖同步加进 `requirements.txt`；`scripts/setup_wizard.py` 与 `一键部署.bat` 链条必须保持**纯标准库**（它们要在 pip 装依赖之前就能跑起来）
- **改脚本**：在 Windows 上真跑一遍改动的路径，别只做静态检查
- **改 prompt**：`prompts/*.md` 是无人值守班次的大脑。改完至少手动跑一次对应流程，确认「核对闸门」（`scripts/verify_entries.py`）仍然拦得住假数据
- **文档**：README / SKILL 保持中文、简体、口语化技术风格，与现有一致

## 提交前自查

```powershell
# Python 编译检查（__pycache__ 会被生成，.gitignore 已挡，别手动提交）
python -m compileall -q scripts wechat-export
```

- 改过 `.bat` / `.ps1` 的话，提交前看一眼 `git diff --stat`：仓库的 `.gitattributes` 已保护行尾，但**编码**被编辑器改掉它挡不住
- 自查一遍没有引入真实隐私信息（对照上面「红线」第 1 条的关键词列表）
