## 改了什么

<!-- 一两句话。有关联 issue 用 #编号 -->


## 怎么验证的

<!-- 跑了哪些命令 / 手测了哪条路径 -->


## 自查清单

- [ ] 无真实隐私信息：没有真实群名 / 公众号名、wxid、Windows 用户名、聊天内容、webhook、密钥、私人绝对路径（示例一律用占位符，见 CONTRIBUTING.md「红线」）
- [ ] 脚本路径全部按 `__file__` 自定位，没有写死盘符 / 绝对路径；文档里的安装目录用 `<SKILL_DIR>`
- [ ] 改过 `.bat` / `.cmd` / `.ps1` 的话：编码与行尾保持不变（GBK/UTF-8-BOM + CRLF，见 `.gitattributes` 注释），提交前对比过字节
- [ ] Python 改动跑过 `python -m compileall`
- [ ] 新增第三方依赖已加进 `requirements.txt`；`scripts/setup_wizard.py` 与 `一键部署.bat` 保持纯标准库
- [ ] 文档改动与现有 README / SKILL 风格一致
