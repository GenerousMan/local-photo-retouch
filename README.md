# 照片精修与调色

让 AI 帮你修照片，也保留照片本来的样子。

`local-photo-retouch` 是一个给 Codex 用的修图技能。上传照片，用平常说话的方式描述想改哪里、想要什么感觉，就能完成局部精修、整体调色，并查看前后对比。

## 它能帮你做什么

- **修好局部，保留周围。** 整理衣服褶皱、调整人物或物体的局部轮廓。只把选定范围的修改拼回原图，并逐像素检查范围外的内容是否保留。
- **让光线和颜色更合心意。** 调整曝光、白平衡、对比和色彩，也可以为一组照片统一观感。
- **看清每一步的效果。** 保留原尺寸成片，按实际处理内容提供对比。精修后再调色时，会留下未调色版本，方便判断和继续修改。

适合那些“整体已经喜欢，只想再修好一点”的照片，也适合从明暗和色彩入手整理成片。

## 安装

将本项目安装到 Codex 的技能目录

```bash
git clone https://github.com/GenerousMan/local-photo-retouch.git ~/.codex/skills/local-photo-retouch
```

也可以[下载 ZIP](https://github.com/GenerousMan/local-photo-retouch/archive/refs/heads/main.zip)，解压后将文件夹命名为 `local-photo-retouch`，放到 `~/.codex/skills/`。

在 Codex 中开始新对话，上传照片并调用 `$local-photo-retouch`。

需要 Python 3.9+，依赖列在 [requirements.txt](requirements.txt) 中。可以让 Codex 在虚拟环境中安装。局部内容生成还需要可用的 `imagegen` 技能和图像编辑工具。

## 试着这样说

> 使用 $local-photo-retouch，把衣服侧边的褶皱理顺一些，保留面料纹理，其他地方保持原样。

> 使用 $local-photo-retouch，整体提亮一点，保留傍晚的暖色和暗部层次，肤色自然。

> 使用 $local-photo-retouch，先精修我圈出的地方，再把整体颜色调柔和一些，给我看前后对比。

也可以附上参考图，说明喜欢的颜色、光线或调整幅度。

## 关于成片

局部精修采用“裁剪 → 生成 → 蒙版拼接 → 校验”的流程；整体调色使用常规图像处理，并保存参数。

默认交付无损 PNG，也可另存 JPEG。范围外像素不变的保证适用于未调色的局部精修 PNG；整体调色会改变颜色，JPEG 压缩也可能改变像素值。脚本处理静态 8 位 RGB/RGBA 照片，RAW 和高位深文件需先用相应工具转换。

完整流程见 [SKILL.md](SKILL.md)。欢迎通过 Issue 分享使用体验、通过 PR 改进方法和文档。

## 许可证

[MIT](LICENSE)
