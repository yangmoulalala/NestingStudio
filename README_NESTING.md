# 板材零件自动套裁与嵌套排版（Auto-Nesting）

该工具读取批量 2D 轮廓，先按真实厚度分组，再对每个厚度组独立选板、优化和输出 DXF。不同厚度的零件绝不会混在同一张板的排版结果中。

## 1. 厚度可信来源

程序不读取、不解析、不推断文件名或目录名中的厚度信息。

| 输入类型 | 厚度来源 |
|---|---|
| STEP / STP | 从 B-Rep 的两个宽大平行面自动计算 |
| DXF / SVG | 必须由 `--thickness` 或清单 `thickness_mm` 明确给出 |
| 多边形 JSON | 必须由 `thickness_mm` 或 `--thickness` 明确给出 |

如果 2D 或坐标输入缺少厚度，程序默认停止并报告缺少厚度的零件。只有显式添加 `--allow-unknown-thickness` 时，才会把这些零件放入独立的 `未指定厚度` 组。

`thickness_mm` 是用户提供的数据，不会被程序当作文件名猜测；STEP 零件即使名称含有数字，也仍以几何厚度为准。

## 2. 支持输入

- DXF：直线、圆弧、圆、椭圆、样条曲线、轻量多段线等，使用 `ezdxf` 展开。
- SVG：`path`、`rect`、`circle`、`ellipse`、`polygon`、`polyline` 等，使用 `svgelements` 解析。
- STEP/STP：自动选择板材宽面，将 3D B-Rep 投影成外轮廓和内孔，并同步计算厚度。
- JSON：直接提供多边形坐标环；内外环通过包含关系自动判断，不要求固定顺序。

曲线进入排版前按 `--flatten-tolerance` 离散，默认 `0.5 mm`。

## 3. 安装

```powershell
python -m pip install -r requirements.txt
```

主要依赖：

- CadQuery/OpenCascade：STEP B-Rep、厚度和宽面投影。
- Shapely/GEOS：多边形布尔运算、外扩碰撞和距离校验。
- ezdxf：DXF 输入与排版 DXF 输出。
- svgelements：SVG 路径解析。
- PyClipper：预留高精度轮廓偏移和共边切割扩展。

## 4. 快速运行

### 处理 STEP 目录

STEP 厚度自动识别，不需要 `--thickness`：

```powershell
python auto_nest.py "D:\零件STEP" `
  --sheet-width 2440 `
  --sheet-height 1220 `
  --margin 10 `
  --clearance 5 `
  --kerf 2 `
  --lead-length 5 `
  --optimizer greedy `
  --output-dir "D:\排版结果"
```

### 处理 DXF/SVG

2D 文件不含厚度，必须明确指定：

```powershell
python auto_nest.py "D:\DXF零件" `
  --thickness 3.0 `
  -W 2440 `
  -H 1220
```

也可以将文件或文件夹拖到 `run_auto_nest.bat` 上。

### 使用 JSON 清单控制厚度、数量与纹理方向

```json
{
  "parts": [
    {
      "name": "large_frame",
      "path": "parts/frame.step",
      "quantity": 2,
      "rotations": [0, 180],
      "grain_locked": true,
      "priority": 10
    },
    {
      "name": "small_plate",
      "path": "parts/small_plate.dxf",
      "quantity": 8,
      "thickness_mm": 3.0,
      "rotations": [0, 90, 180, 270]
    }
  ]
}
```

```powershell
python auto_nest.py --manifest .\manifest.json -W 2440 -H 1220
```

`grain_locked: true` 且未写 `rotations` 时，默认只允许 `0°` 和 `180°`，适合拉丝、轧制或折弯方向受限制的板材。

### 直接使用多边形坐标

```json
{
  "name": "frame",
  "quantity": 2,
  "thickness_mm": 5.0,
  "rotations": [0, 180],
  "polygons": [
    [[0, 0], [300, 0], [300, 200], [0, 200]],
    [[60, 50], [240, 50], [240, 150], [60, 150]]
  ]
}
```

坐标环不必按外环、内环顺序排列；程序根据包含关系重建外轮廓、孔和孔中岛。

## 5. 强制厚度分组流程

```text
读取 STEP / 2D / JSON
        │
        ├─ STEP：几何计算厚度
        ├─ 2D/JSON：读取明确 thickness_mm
        └─ 无法确定：报错，除非明确允许未指定组
        │
按厚度聚类（默认 ±0.05 mm）
        │
每个厚度组建立独立 Bottom-Left 引擎
        │
每个厚度组独立运行 SA/GA/Greedy 和多板材装箱
        │
输出 厚度_1mm、厚度_1.5mm、厚度_2mm ... 独立目录
```

厚度目录标签按 `--round-thickness` 取整，分组误差由 `--thickness-tolerance` 控制。

## 6. 工艺约束实现

### 安全间距和刀缝

零件外轮廓按 `effective_clearance / 2` 外扩，放置时以外扩图形做碰撞检测：

```text
effective_clearance = max(part_clearance, kerf + 2 × lead_length)
```

因此实际轮廓间距不小于有效安全间距，也自动满足穿孔引线空间要求。

### 板材边距

真实轮廓必须位于有效加工区域内：

```text
effective_edge_margin = margin + kerf / 2 + lead_length
```

最终报告会再次测量真实轮廓到板边的最小距离。

### 内孔二次嵌套

碰撞轮廓保留足够大的内孔。小零件只有在完整进入内孔、并且与孔壁满足有效间距时，才会标记为 `nested_in_hole: true`。

### 热变形控制

程序按热影响半径统计局部材料密度。默认半径 `80 mm`、最大密度 `0.90`，超限候选位置会被拒绝。参数：

```powershell
--thermal-radius 100 --thermal-density-limit 0.85
```

设为 `1.0` 可关闭热密度拒绝。

### 共边切割扩展

`nesting/common_line.py` 提供 `CommonLinePolicy` 接口。未来接入精确共边识别后，不需要修改上层优化器和放置引擎。

## 7. 排版算法

1. 按真实厚度分组。
2. 对每个厚度组独立预计算旋转轮廓和碰撞轮廓。
3. Bottom-Left-Fill 生成候选位置：板材边界、接触点、顶点和内孔包围盒。
4. 逐个候选执行边距、碰撞、内孔和热密度检查。
5. Greedy、SA 或 GA 优化零件顺序及旋转角。
6. 优化目标优先减少板材数量，其次减小已用包围面积。
7. 使用真实未外扩轮廓进行最终间距和重叠校验。

当前版本是“外扩大轮廓 + 顶点接触候选”的 Bottom-Left 方法，属于成熟 No-Fit Polygon 思路的高效近似；后续可用精确 NFP 替换候选生成器，上层优化器保持不变。

## 8. 输出结构

```text
nesting_output/
├─ 厚度_1mm/
│  ├─ nesting_sheet_01.dxf
│  ├─ nesting_all_sheets.dxf
│  ├─ nesting_layout.json
│  └─ nesting_placements.csv
├─ 厚度_3mm/
│  ├─ nesting_sheet_01.dxf
│  ├─ nesting_sheet_02.dxf
│  ├─ nesting_all_sheets.dxf
│  ├─ nesting_layout.json
│  └─ nesting_placements.csv
└─ nesting_global_summary.json
```

- 每个 `厚度_*` 目录是独立生产任务。
- 单张 `nesting_sheet_XX.dxf` 用于切割。
- `nesting_all_sheets.dxf` 是并排总览。
- `nesting_placements.csv` 包含 X、Y、旋转角、厚度和孔内嵌套标志。
- `nesting_global_summary.json` 汇总所有厚度组的板材数和利用率。

DXF 图层：

| 图层 | 含义 |
|---|---|
| `SHEET_BORDER` | 板材边界 |
| `USABLE_BOUNDARY` | 有效加工边界 |
| `PART_OUTER` | 零件外轮廓 |
| `PART_HOLE` | 零件内孔 |
| `PART_LABEL` | 零件编号 |
| `LEAD_IN` | 引线预留示意 |

## 9. 关键参数

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `--thickness` | 无 | DXF/SVG/坐标 JSON 的明确厚度 mm |
| `--thickness-tolerance` | 0.05 | 厚度分组误差 mm |
| `--round-thickness` | 0.05 | 厚度目录标签取整步长 |
| `-W, --sheet-width` | 2440 | 板材宽度 mm |
| `-H, --sheet-height` | 1220 | 板材高度 mm |
| `--margin` | 10 | 原始轮廓与板边间距 mm |
| `--clearance` | 5 | 零件间最小安全间距 mm |
| `--kerf` | 0 | 刀缝宽度 mm |
| `--lead-length` | 0 | 穿孔引线长度 mm |
| `--hole-nesting` | 开启 | 是否允许孔内嵌套 |
| `--thermal-radius` | 80 | 热影响密度统计半径 mm |
| `--thermal-density-limit` | 0.90 | 局部最大密度 |
| `--optimizer` | greedy | `greedy`、`sa`、`ga` |
| `--iterations` | 80 | SA 迭代次数 |
| `--population` | 12 | GA 种群大小 |
| `--generations` | 20 | GA 代数 |
| `--time-limit` | 60 | 每个厚度组优化时间上限秒 |
| `--flatten-tolerance` | 0.5 | 曲线离散容差 mm |

## 10. 测试

```powershell
python -m unittest discover -s .\tests -v
```

当前测试覆盖：

- 不同厚度零件必须分到不同组。
- 大零件内孔嵌套小零件。
- 零件安全间距、边距和重叠校验。
- DXF 输入孔洞重建。
- DXF 分层输出。
- STEP 宽面投影和真实几何厚度提取。
- Greedy、SA、GA 优化器。

示例清单位于 `examples/nesting_manifest.json`。

## 11. 开源与工业算法参考

实现参考以下项目的公开设计思想，没有直接复制源码：

- SVGnest / Deepnest：GA 顺序与旋转优化、NFP 放置。
- libnest2d：Bottom-Left、多板材和工业排版接口。
- Clipper / PyClipper：多边形偏移和布尔运算。
- Shapely / GEOS：缓冲区、孔洞、碰撞和距离验证。
- ezdxf：DXF 输入输出与单位管理。

精确 NFP、真实共边刀路和机床控制器后置处理属于后续扩展。
