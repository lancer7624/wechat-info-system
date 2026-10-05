# -*- coding: utf-8 -*-
"""一次性:把双平台铁律插入 分类规则.md 和 3 个班次 prompt 的 QQ 节更新"""
import sys

sys.stdout.reconfigure(encoding="utf-8")

SECTION = """
## 双平台铁律(用户 2026-09-19 定,最高优先级)

**微信 + QQ 两个平台都要分析,一个不能少,但条目必须区分:**

- 每个分析班次:**微信白名单群和 QQ 白名单群都扫**,任一平台有新消息都要产出
- 所有条目必须带 `platform` 字段:"微信" 或 "QQ",写待审 json 时就带上
- 飞书推送标题带平台标记:微信条目标题用 `📋`,QQ 条目标题用 `📋QQ`
- QQ 源文件在 `wechat-export\\qq_export\\<日期>\\`,白名单在 `wechat-export\\qq_config.json`,水位 `qq_analysis_watermark.json`(独立)
- 数据核实铁律同样适用于 QQ:quotes 必须回 QQ 导出文件逐字核对
"""


def main():
    p = "分类规则.md"
    s = open(p, encoding="utf-8").read()
    if "双平台铁律" in s:
        print(p, "已有,跳过")
    else:
        idx = s.index("## 数据核实铁律")
        s = s[:idx] + SECTION.lstrip("\n") + "\n" + s[idx:]
        open(p, "w", encoding="utf-8").write(s)
        print(p, "已插入双平台铁律")


if __name__ == "__main__":
    main()
