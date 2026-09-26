---
name: wechat-export
description: 从微信 Windows 本地加密数据库导出聊天记录为 Markdown（微信 4.x，SQLCipher）。登录时 Frida 一次性捕获 master key → 之后完全离线解密导出。含封号安全铁律、每日定时导出、WAL 限制说明。是 wechat-assistant 的采集层依赖。
---

# 微信聊天记录导出（分享版，已脱敏）

> 详细操作见同目录 `接手说明.md`。本文件补充核心知识。
> 本包不含任何密钥。目标机器需自己抓 key（两机密钥各自独立）。

## 完整数据流

```
微信登录 → Frida（一次性捕获 master key）→ db_key.json
    ↓（离线，任意时刻）
db_storage/*.db 快照 → decrypt_db.py 解密 → sqlite3 读取
    ↓
export_chat.py → 按会话导出 Markdown（export\YYYY-MM-DD\）
    ↓
daily_export.py 每日三班增量（水位过滤）：快照 6 库→解密→聊天导出（白名单）
    → 订阅号推送清单（biz 库 zstd 解压）→ 关键词兜底 toast → 媒体归档 → 新消息检测
```

## 已验证的加密方案（核心知识，勿丢失）

- **master key**（32B）在登录时捕获（本包用 phase2e_login.py spawn + 登录感知捕捞 → phase3_verify3.py 离线验证），一次捕获长期有效
- **enc_key** = PBKDF2-HMAC-SHA512(master, 页1[0:16], dklen=32, count=256000)
- **mac_key** = PBKDF2-HMAC-SHA512(enc_key, 页1salt⊕0x3a, dklen=32, count=2)
- **页面布局**（页大小 ps=4096，reserve=80，usable=ps−80=4016）：
  - 页 1：`salt(16) | ct[16:usable] | IV(16) | MAC(64)`，pt[0:16] = SQLite 魔数（"SQLite format 3\x00"）
  - 页 ≥2：`ct[0:usable] | IV(16) | MAC(64)`（无 salt）
- **页密码**：AES-256-CBC，key=enc_key，IV=每页 [usable:usable+16] 处的存储值
- **MAC**：HMAC-SHA512(mac_key, ct区+IV+LE32(页号))[:64] == 页内 [usable+16:usable+80]
- 数据库文件 = N×4096 字节的整页序列，无文件头

## 目标数据库

| 库 | 路径（db_storage/ 下） | 内容 |
|----|----------------------|------|
| message_0.db | message\ | 聊天消息（每会话一张 Msg_\<md5\> 表） |
| biz_message_0.db | message\ | 订阅号推送（zstd 压缩 XML：标题/摘要/链接） |
| contact.db | contact\ | 联系人、群成员 |
| session.db | session\ | 会话列表、未读数 |
| sns.db | sns\ | 朋友圈 |
| favorite.db | favorite\ | 收藏 |

> db_storage 根目录形如：`C:\Users\<用户名>\Documents\xwechat_files\<你的wxid目录>\db_storage\`

## 封号安全铁律（大号专用，最高优先级）

1. **只读捕获**：Frida 只 hook 编解码函数（SQLCipher 密钥设置、页加密函数），**绝不 hook 或修改消息发送/接收路径**
2. **一次性**：登录时捕获 master key 成功后**立即 detach**，不驻留、不重复 hook
3. **不注入任何数据**：不修改内存中的任何值，只读寄存器/缓冲区
4. **离线为主**：日常导出完全离线解密，不再碰微信进程
5. 捕获脚本只在"换机器/密钥失效"时才需重跑

## 图片 .dat 加密格式（4.x，公开研究 ZedeX/weixin-decrypte-script）

- 旧版单字节 XOR：密钥由图片魔数反推（JPEG `FF D8 FF` 等）
- V1（`07 08 56 31 08 07`）：AES-128-ECB（固定密钥 `cfcd208495d565ef`=md5("0")[:16]）+ 尾部 XOR(0x88)
- V2（`07 08 56 32 08 07`）：AES-128-ECB（密钥不固定，在微信进程内存）+ 尾部 XOR(0xED)
- V1/V2 文件结构：`magic(6B) | aes_size(4B LE) | xor_size(4B LE) | 占位(1B)`，body = AES段(16B对齐) + 原始段 + XOR段
- **图片 AES 密钥提取**：微信运行中点开 2-3 张图片（密钥加载进内存）→ hook_image_key3.py 一次性只读 hook（现成偏移只对固定微信版本有效，其他版本需重新静态分析）→ 保存 image_key.json。无 key 时归档原样暂存 .dat

## 已知限制

- master key 与用户账号+本机绑定；换机/重装微信需重新捕获
- 文本消息直接可读；图片/语音/表情为二进制（protobuf），需另行解析
- 微信运行中直接读 .db 可能读到半页，务必先复制快照再解密
- **WAL 合并不可靠（已实测确认）**：微信 WAL 为环形复用（journal_size_limit=4MB），帧在文件中不按帧号顺序排列，有效帧区需 -shm wal-index 定位，而 -shm 是 mmap 文件、微信运行中读取必然撕裂。因此生产路径**默认只用主库快照**（一致但滞后几页）；滞后消息会在微信 checkpoint 后次日自动补齐
- 每日 12:00/18:00/22:00 定时跑 daily_export.py（Windows 计划任务三班），输出 export\YYYY-MM-DD\ 供上层 wechat-assistant 分析
