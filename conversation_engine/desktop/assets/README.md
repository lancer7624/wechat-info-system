# 会话笺生成素材

以下三张图片由内置 `image_gen` 工具于 2026-10-01 生成，已经直接用于桌面界面；没有远程图片请求。小鹊保留 PNG 的透明通道。原始生成图保留在 Codex 的 generated_images 目录，本目录保存运行所用的原样副本。

| 文件 | 用途 |
| --- | --- |
| courier-magpie.png | 页眉传话鹊、等待读取状态插画 |
| jade-bamboo.png | 墨绿页眉、主要按钮与收起书签的纸面竹影 |
| ivory-paper.png | 主窗纸纹与浅青回复笺背景 |

三份 PNG 和本说明进入桌面构建资源白名单。静态资源服务只开放这三张指定 PNG，不扫描目录。PNG 像素不承担联系人、聊天内容、状态和按钮文字；这些仍是可选择、可编辑的真实页面控件。

## 生成提示词

参考图：上一轮会话笺概念稿，只作材质和插画风格参考。

### courier-magpie.png

```text
Use case: illustration-story. Asset type: transparent raster UI illustration, a tiny courier magpie for the Chinese desktop application 会话笺. Input image is STYLE REFERENCE ONLY: use the magpie illustration and Song-inspired editorial material language from the reference interface; do not reproduce the interface. Generate one isolated small expressive black-and-white magpie in three-quarter side view facing right, flying gently with one gracefully raised wing and one lower wing, carrying a tiny folded ivory envelope with a vermilion wax dot in its beak. Ink-black head, cream-white chest and feather tips, muted petrol blue long tail, delicate layered feather shapes, warm charming expression. Refined softly textured hand-painted editorial illustration with a sharp readable silhouette that still works at 48 to 64 CSS pixels, clear eye and beak, compact composition centered and filling most of the canvas. Real alpha transparency around the bird, no background, no scenery, no drop shadow, no text, no lettering, no border, no UI. No tiny decorative clutter, no exaggerated cartoon face.
```

### jade-bamboo.png

```text
Use case: stylized-concept. Asset type: quiet horizontal background texture for the header of a Chinese Song-inspired desktop assistant. Use the reference image only for its dark forest-jade paper material and very subtle bamboo motif; do not reproduce any interface, text, bird, button, border or shadow. Produce a wide 3:1 rectangular flat material surface, deep muted forest-jade #2e4b3f with fine softly visible handmade paper fibres, tactile matte finish, restrained naturally uneven ink density. At far right only, a few graceful bamboo leaves and a thin bamboo branch in an even deeper jade tone, extremely understated tone on tone (8 to 12 percent visual contrast), tapering diagonally from top right toward center right. Left two thirds almost empty and uniform for white interface text. Flat front-on view, no perspective, no vignette, no bright spots, no ornamental border, no typography, no interface elements, no strong patterns, no photographic objects. It must be a refined background asset that stays quiet when scaled to 300 by 80 pixels.
```

### ivory-paper.png

```text
Use case: stylized-concept. Asset type: square background material for a Song-inspired Chinese reading interface, a fine warm ivory paper texture. Flat front-on material swatch only, uniform very light warm off-white #f7f5ef handmade washi or fine rice paper with subtle delicate short fibres and faint natural variation. It must stay mostly smooth and nearly white, quiet enough behind dark 16px reading text. A very faint pale grey-green bamboo leaf silhouette in ONLY the extreme bottom right corner, occupying less than 12 percent of the image and less than 5 percent contrast, blending into the paper. Reference image is material and palette reference only, do not reproduce the interface. No text, no lettering, no border, no fold, no shadows, no wrinkles, no stains, no illustration elsewhere, no noticeable repeat seams, no perspective, no vignette, no visible object, no bird. Rest of surface completely clean. The visual should be refined and restrained, not aged yellow parchment.
```


## 工作台复用

完整工作台复用这三张原图：小鹊用于页眉，墨绿竹影用于页眉、整理按钮与证据侧栏，纸纹用于摘要、整理入口与侧栏纸面。看板渲染器只读取指定图片，嵌入 HTML 后离线也可显示；素材继续只作装饰，不承载模型结果或按钮文字。
