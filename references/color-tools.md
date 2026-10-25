# 常规调色工具与参数

`scripts/color_grade.py` 提供 `grade` 与 `compare`。依赖 Pillow、NumPy，调色另需 OpenCV，完整清单见 [requirements.txt](../requirements.txt)。先复用环境，缺依赖时在任务目录的隔离环境安装，不改全局 Python。所有路径通过参数传入，不依赖某次任务目录。下面的 `python3` 应替换为已确认具备依赖的解释器。

复制 `assets/color-recipes/neutral.json` 到任务目录，从中性状态根据观察结果设置参数。方法选择见 [global-color.md](global-color.md)，具体问题与验收见 [color-cookbook.md](color-cookbook.md)。

按空间区域调整时，在同一参数文件中添加 `regions`，用法见 [spatial-color.md](spatial-color.md)。仍使用下面的 `grade` 命令。

```bash
python3 /path/to/skill/scripts/color_grade.py grade \
  --source /path/to/detail.png --recipe work/color-recipe.json \
  --output work/color-preview.png --preview-max-edge 1600

python3 /path/to/skill/scripts/color_grade.py grade \
  --source /path/to/detail.png --recipe work/color-recipe.json \
  --output outputs/graded.png --jpeg outputs/graded.jpg

python3 /path/to/skill/scripts/color_grade.py compare \
  --original /path/to/original.jpg --detail /path/to/detail.png \
  --graded outputs/graded.png --output outputs/comparison.jpg \
  --font /path/to/chinese-font.ttf --title '整图三阶段对比'
```

对比始终提供 `--original` 和 `--output`，其余输入按实际处理选择

| 任务 | 阶段输入 |
|---|---|
| 仅局部精修 | `--detail` |
| 仅调色 | `--graded` |
| 精修后调色 | `--detail` 与 `--graded` |

局部对比追加 `--box LEFT TOP RIGHT BOTTOM`；整图省略它。`--font` 指定含中文字形的字体；省略时输出英文阶段标签。旧参数 `--no-local-edits` 仍可用于纯调色任务；若同时传入 `--detail`，先核对它与原图的解码像素、模式及 ICC 完全相同，再省略该重复阶段。全尺寸文件应先校验尺寸一致，脚本不会为凑齐对比而拉伸、对齐或裁掉某个阶段。RGBA 对比显示在浅色底板上；PNG 母版保留透明度，JPEG 导出须另行明确背景。

默认仅直接接受编码数据与内置 sRGB 配置一致的 ICC，识别依据包括色彩转换标签，不以配置名称判断。其他配置可显式追加 `--profile-policy convert-srgb`，报告记录转换，输出使用 sRGB ICC。确认未标记输入为 sRGB 后才使用 `--assume-srgb`。脚本拒绝覆盖已有文件，预览与成片使用不同路径。

## 参数含义与处理顺序

所有数值必须有限，未知字段会报错，避免拼写错误悄悄失效。先处理曝光/通道增益，再转为 CIELAB 调整明暗与颜色，最后转换回 sRGB。参数命名不等同于 Lightroom 或 Photoshop 滑块数值。

| 字段 | 含义 |
|---|---|
| `version` | 参数格式，当前为 `1` |
| `description` | 本次目标、参考依据及保护要求的简短说明 |
| `exposure_ev` | 在线性 RGB 中乘以 `2^EV`；JPEG 调亮不会恢复已丢失细节 |
| `linear_rgb_gains` | 红、绿、蓝通道的相对增益，用于温度/色偏微调；不是以开尔文计的真实相机白平衡 |
| `l_curve` | CIELAB L* 的 `[输入, 输出]` 控制点；输入严格从 0 到 100 递增，输出非递减。只改亮度轴，避免独立 RGB 曲线引起不必要色偏 |
| `saturation` | 对 Lab a*/b* 色度的统一缩放，`1` 不变，`0` 去色 |
| `color_adjustments` | 按亮度与颜色范围选择性调整 a*/b*，包括缩放与平移；并非人物识别或空间蒙版 |
| `regions` | 可选的有序区域列表；全局处理完成后，分别按灰度蒙版应用区域参数 |

Lab 的 a* 正向偏红、负向偏绿；b* 正向偏黄、负向偏蓝。颜色选区 `select` 可含 `l`、`a`、`b`；每个范围为 `[外下界, 内下界, 内上界, 外上界]`，内区权重为 1，边缘平滑渐变，外部为 0，多条件取乘积。例如 `l: [45,90,100,100]` 逐渐选中亮部，`b: [2,12,128,128]` 逐渐选中黄色。缺少 `select` 表示全图。

每个调整使用 `a_scale`、`b_scale`（默认 1）以及 `a_offset`、`b_offset`（默认 0）。各选区始终依据曝光处理后、曲线与选色处理前的 Lab 计算；多个调整的增量相加，避免顺序导致选区漂移。重叠调整会叠加，应控制总幅度。中性参数直接保留进入调色步骤的 sRGB 像素；如需输入色彩空间转换，该转换仍会执行。


## 能力范围

此工具用于已渲染的 8 位 sRGB 图片，支持曝光增益、通道比例、单调 Lab 亮度曲线、色度缩放与按颜色/亮度范围的选色，也可将这些操作限定到用户提供的空间蒙版内。它不实现 RAW 解码、相机标定、局部自适应高光恢复、Vibrance、HSL 全部功能、自动语义蒙版、LUT 或软打样。遇到这些需求时使用相应的常规处理软件或合适工具，不把简化处理称为专业软件的等效算法。

默认保存原 ICC；显式转 sRGB 时保存新的 sRGB ICC。预览会缩小尺寸；最终运行省略 `--preview-max-edge`。校验源尺寸、PNG 解码值、ICC 与 alpha，并记录端点比例和色域裁切。统计采用未重采样的步进取样，亮度统计为编码 RGB 的加权值，仅用于前后对照，不等同于线性物理亮度或显示器测量。

报告同时记录实际参数、输入输出与参数文件的 SHA-256、ICC 哈希及运行库版本。使用 `--assume-srgb` 的无配置输入会保留无配置状态，并在报告中记录解释依据；发布前需要嵌入配置时，应明确另行导出。脚本会拒绝可识别的高位深 PNG/TIFF，避免隐式降低精度。

分区任务另记录各蒙版的路径与哈希、作用及变化像素数，以及相对于全局处理结果的蒙版并集外变化数。顶层裁切比例只描述全局步骤；各区域的裁切数量描述混合前的局部候选，区域重叠时不能相加作为整图比例。
