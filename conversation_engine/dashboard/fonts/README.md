# 看板字体

看板使用 Google Fonts 官方仓库中的 **Noto Serif SC** 可变字体，标题采用 500 字重，摘要和正文采用 400 字重。按钮、日期和数字继续使用系统界面字体。

- 来源：[Google Fonts / Noto Serif SC](https://github.com/google/fonts/tree/main/ofl/notoserifsc)
- 原始文件：`NotoSerifSC[wght].ttf`
- 原始文件 Git blob SHA-1：`eab063faf229160a52d3760f5555150e4eb9e5bf`
- 原始文件大小：25,125,512 字节
- 字体版本：`Version 2.003-H1;hotconv 1.1.1;makeotfexe 2.6.0`
- 授权：SIL Open Font License 1.1，全文见同目录 `OFL.txt`
- 下载日期：2026-09-30

`NotoSerifSC.woff2` 由原始字体通过 fontTools 4.66.1 和 Brotli 1.2.0 转换而来，只压缩文件格式，保留全部 31,058 个字形及 200—900 的字重轴，没有按聊天内容裁剪字库。

- WOFF2 大小：11,032,528 字节
- WOFF2 SHA-256：`81e6beb6443e63b7dde1524e5fc76ec28dad5e015ba472e137c3ecc0166e9c3d`

生成看板时，字体以 data URL 嵌入 HTML，完整授权文本也保留在 HTML 样式注释中。字体无需联网加载或安装到系统；单个 HTML 文件因此增加约 14.7 MB。fontTools 和 Brotli 仅用于本次格式转换，不是项目运行依赖。
