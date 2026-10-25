# 拼接工具

本页说明局部裁剪、蒙版、拼接与校验。常规全局调色及前后对比导出见 [global-color.md](global-color.md)。

脚本使用 Python 3.9+、Pillow 和 NumPy；完整依赖见 [requirements.txt](../requirements.txt)。先复用现有运行环境；缺少依赖时在任务目录创建虚拟环境并安装，不修改用户的全局 Python 环境。

下方以安装在默认技能目录为例，实际执行时使用当前技能所在位置。每轮使用新的工作目录和输出文件名，脚本拒绝覆盖已有文件。

```bash
RET_TOOL="$HOME/.codex/skills/local-photo-retouch/scripts/retouch.py"
python3 "$RET_TOOL" --help
```

## 裁剪

```bash
python3 "$RET_TOOL" crop \
  --source /absolute/path/photo.jpg \
  --box 400 200 1400 1200 \
  --workdir work/retouch-001
```

`box` 为左、上、右、下，右和下不包含在矩形内。坐标基于读取 EXIF 方向后的显示画面。脚本记录归一化后的尺寸、源文件哈希和绝对路径，生成 `crop.png`、`selection.png`、`session.json`。支持静态 8 位 RGB/RGBA 照片；高位深 PNG/TIFF 会明确报错，其他模式需单独决定转换方式。

## 蒙版

创建 JSON 文件，所有点均为裁剪图内部的坐标。下面的多边形仅展示数据结构，实际范围必须根据照片确定。

```json
{
  "include": [[[100, 100], [700, 100], [750, 800], [100, 800]]],
  "protect": [[[120, 120], [200, 120], [200, 400], [120, 400]]],
  "feather": 8,
  "cutoff": 3
}
```

```bash
python3 "$RET_TOOL" mask \
  --session work/retouch-001/session.json \
  --spec work/retouch-001/mask-spec.json \
  --output work/retouch-001/mask.png
```

`include` 中各多边形取并集，羽化后将小于 `cutoff` 的值置零，再把 `protect` 区域严格置零。`feather` 的单位是原始裁剪图像素。已有蒙版可以跳过此命令，但必须是与裁剪图等大的 8 位灰度图。

## 拼接和复核

```bash
python3 "$RET_TOOL" compose \
  --session work/retouch-001/session.json \
  --patch /absolute/path/aligned-generated-patch.png \
  --mask work/retouch-001/mask.png \
  --output outputs/retouched-photo.png
```

候选图与裁剪尺寸不同时默认报错。已确认构图和宽高比一致后，增加 `--resize-patch` 可等比例恢复尺寸。脚本不自动推断对齐、生成蒙版、补全背景或判断人体比例。

输出旁边保存同名 `.verification.json`，记录原图与输出尺寸、源图和蒙版哈希、修改像素数、修改包围框，以及蒙版外变化像素数。`passed` 仅表示像素保留检查通过；`has_changes` 为假时还没有实际修图效果。

可以对已有成片重新校验

```bash
python3 "$RET_TOOL" verify \
  --session work/retouch-001/session.json \
  --mask work/retouch-001/mask.png \
  --output outputs/retouched-photo.png
```

源文件自裁剪后改变、尺寸错误、源图与输出 ICC 不一致，或蒙版外存在差异都会使验证失败。PNG 保留源图 ICC 配置与可用的 EXIF、DPI；EXIF 方向按显示画面归一化。候选图与底图的 ICC 不同会先转换候选图，未标记的 RGB 图按 sRGB 解释。RGBA 原图的 alpha 通道原样保留，本工具不处理透明轮廓增删。候选图在修改区域需要是不透明的。

多处独立拼接后的总校验，需要另外建立以最初原图为底图、`box` 覆盖完整画面的会话。把每处蒙版按各自坐标放入整图大小的黑色灰度图，重叠位置取最大值，得到最终允许修改范围，再用 `verify` 检查最终成片。绘制或合并蒙版不需要调用图像生成工具。
