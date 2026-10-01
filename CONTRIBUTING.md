# 参与贡献

本项目是脱敏分享版（微信 4.x 本地导出 → AI 分析分流 → 核对闸门 → HTML 看板 + 飞书提醒），欢迎三类参与：**报问题、提改动、给建议**。以下是从零到合并的完整流程，不需要任何权限申请——有 GitHub 账号就能开始。

## 一、遇到问题：提 Issue

1. **先自查**：翻 README「常见问题」和已有的已关闭 issue，大概率有人踩过
2. 进仓库 **Issues** → New issue → 选 **Bug 报告** 模板
3. 按模板填环境信息：微信版本、Python 版本与**完整路径**（`where python` 的结果——多版本机器这是头号坑）、用的哪个命令行智能体、卡在哪一步
4. **脱敏再发**：真实群名、公众号名、wxid、Windows 用户名、聊天记录、webhook、密钥一律删掉或打码；日志只贴报错段

> 新功能想法走 **功能建议** 模板——重点说清使用场景（谁、什么情况、想要什么结果），比「加个 XX 功能」有用得多。

## 二、小改动 / 修 bug：提 PR

### 1. 准备

点仓库右上角 **Fork**——你名下会有一份自己的副本，然后克隆到本地：

```bash
git clone https://github.com/你的用户名/wechat-info-system.git
cd wechat-info-system
```

本项目没有任何构建步骤，检出即用（脚本按自身位置自动定位，放哪都能跑）。

> 长期跟进的贡献者，建议再加一个上游 remote，之后随时同步主仓库最新代码：
>
> ```bash
> git remote add upstream https://github.com/lancer7624/wechat-info-system.git
> git fetch upstream
> git merge upstream/master
> ```

### 2. 开分支

```bash
git checkout -b fix/xxx     # 分支名说清楚改什么，如 fix/psi-encoding、feat/gemini-support
```

### 3. 动手改

先读下面的**红线**和**开发约定**（脱敏、bat/ps1 编码、路径自定位——这些是别人踩过坑的地方）。改动尽量聚焦：一个 PR 解决一件事，别做大杂烩。

### 4. 本地验证

```bash
python -m compileall -q scripts wechat-export   # Python 编译检查
```

- 改脚本的：在 Windows 上把对应的路径真跑一遍
- 改 prompt 的：手动跑一次对应班次流程，确认「核对闸门」（`scripts/verify_entries.py`）仍然拦得住假数据

### 5. 提交并推送到你的 fork

```bash
git add -A
git commit -m "修复 xxx：一句话说清改了什么、为什么"
git push origin fix/xxx
```

### 6. 开 PR

推送后 GitHub 页面会提示 **Compare & pull request**，点进去（或到主仓库 Pull requests → New）：

- 目标分支选主仓库的 `master`（不是你自己 fork 的）
- 按 PR 模板填全：改了什么 / 怎么验证的 / 逐条勾自查清单

之后的修改直接继续 push 到同一个分支，PR 会自动更新，不用重开。

### 7. Review 与合并

- 维护者会看 diff，可能留评论请你补充或调整——按要求再推一版即可
- 通过后合并进 `master`：**合并的改动会随维护者的日常同步进入实际运行的系统**，不是躺在仓库里的死代码
- 提交记录里保留你的 GitHub 署名

## 三、大改动：先开 Issue 讨论

新功能、重构、换技术路线这类，先开一个 **功能建议** Issue 聊方案再动手——维护者知道一些文档里没有的约束（班次链路、核对闸门、编码坑），方案对齐后 PR 会顺畅很多，也避免白干。

## 四、红线（碰什么都别碰这三条）

1. **隐私脱敏**：本仓库是脱敏版。任何代码、文档、示例、测试数据都不得出现真实群名/公众号名、wxid、Windows 用户名、聊天内容、webhook、密钥、私人绝对路径——一律用占位符：`wxid_YOURWXID`、`YOURNAME`、`<SKILL_DIR>`、`REPLACE_WITH_YOUR_WEBHOOK`
2. **不写死路径**：所有脚本按 `__file__` 自定位；文档里的安装目录用 `<SKILL_DIR>` 占位符
3. **脚本编码**（Windows 硬约束，改之前先读 `.gitattributes` 的注释）：
   - `.bat` / `.cmd`：**GBK + CRLF**（UTF-8 中文在 cmd 下乱码，LF 行尾会让多行块解析错乱）
   - `.ps1`：**UTF-8 with BOM + CRLF**（无 BOM 会被 PowerShell 5.1 按系统 ANSI 解码，中文乱码直接炸脚本）
   - 仓库对三类脚本声明了 `-text`（git 不转换行尾），但编辑器保存时可能悄悄改编码——提交前对比一眼字节

## 五、开发约定

- **Python**：新增第三方依赖同步加进 `requirements.txt`；`scripts/setup_wizard.py` 与 `一键部署.bat` 链条必须保持**纯标准库**（它们要在 pip 装依赖之前就能跑起来）
- **改脚本**：在 Windows 上真跑一遍改动的路径，别只做静态检查
- **改 prompt**：`prompts/*.md` 是无人值守班次的大脑。改完至少手动跑一次对应流程，确认「核对闸门」（`scripts/verify_entries.py`）仍然拦得住假数据
- **文档**：README / SKILL 保持中文、简体、口语化技术风格，与现有一致

## 六、提交前自查

```bash
python -m compileall -q scripts wechat-export
```

（`__pycache__` 会被生成，`.gitignore` 已挡，别手动提交）

- 改过 `.bat` / `.ps1` 的话，提交前看一眼 `git diff --stat`：仓库的 `.gitattributes` 已保护行尾，但**编码**被编辑器改掉它挡不住
- 自查一遍没有引入真实隐私信息（对照上面「红线」第 1 条的关键词列表）
