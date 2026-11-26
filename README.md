# bms2dtx

轻量级 Windows 桌面与命令行工具，将 **BMS / BME / BML / PMS** 谱面无损转换为 **DTXMania / DTXCreator** 可直接编辑和游玩的 `.dtx` 工程目录（Toggle = OFF 离线混音工作流，内置音频引擎，**开箱即用无需安装 FFmpeg**）。

---

## 核心特性

- **原谱只读与宽容解析**：绝不修改源 BMS 目录内任何文件；支持 `CP932` / `UTF-8-BOM` 自动解码（导出可选 `CP932` 或 `UTF-8`）、内存自动修正音频/图片扩展名、对齐 iBMSC/μBMSC 的 `B1..Bn` BGM 列号、`.bml` 长押（`#LNTYPE 1/2`、`#LNOBJ`）与 `.pms` 9 键全 18 轨。
- **15-Tab 可视化映射与物理冲突处理**：
  - 覆盖 12 鼓轨（默认隐藏并合并 `LBD` 至 `BD`，可勾选单独定义）与 `Other`（`SE6`）、`Guitar`（`SE11–12`）、`Bass`（`SE13–14`）。
  - 导入期即在「查看警告」上方用红字标出原谱健康摘要（如未声明定义、缺失音频、`#STOP`），导出完成弹窗仅展示导出期新增警告。
  - **Hi-Hat 互斥拦截与碰撞处理**：同拍 `HH Open (18)` 自动删除碰撞的 `HH Close (11)` 与 `LP (1B)`；Tom 轨（`HT`/`LT`/`FT`）同拍溢出分流 1 音至 `SE7 (67)` 保持独立键音；其余鼓轨同拍多音按 `(wav_id, L/R)` 严格去重缓存、先反转声道再混音并 `0.99` 防削波。
  - 左侧 5 列（`ID` | `文件名` | `次数` | `目标轨` | `L/R`）与右侧 3 列（`ID` | `文件名` | `L/R`）保持**严格选中互斥**，切换右侧 Tab 时保留左侧选中并清空右侧高亮，点击列表空白区取消选中。
  - 支持按文件名/片假名/编号序列（`tom1/2/3`）及立体声声像（`LC`/`CY`）自动猜轨（含人工复查提示）、单切片 **`⇄ L/R`** 与全局 **`所有切片左右声道互换`** 异或叠加，以及设置中可选开启「允许从左侧 WAV 列表移除」。
- **`Auto-LP`（开镲转闭镲自动补左脚踩镲 `1B`）**：
  - 候选池覆盖 **`LP` + `HH Close`**（下拉标注 `[LP]` / `[HH Close]` 来源并随两轨增删实时刷新）；`[自动]` 优先选用 `LP`，若 `LP` 为空则降级挑选时长最短的 `HH Close` 闭镲。
  - 支持按**音符时值**（默认 `<= 1/4`）或**毫秒（`ms`）**判定；不勾选 **`使用 ZY`** 直接复用目标切片原槽位（原音量 `100%`），勾选后写入 `#WAVZY` / `#VOLUMEZY` 并可通过底部栏 **`▶`** 直接试听。
- **离线混音、`drums.ogg` 干声与四难度导出**：
  - 未映射音符自动混音生成 `bgm.ogg`；默认开启 **`drums.ogg`** 鼓组+`Other` 干声导出（从最早有效鼓/`Other` 音符精确零点混缩，以音量 `10` 在 `SE8 (68)` 触发）。
  - 默认勾选**四难度底谱批量生成**：导出相同内容的 `__bsc.dtx` / `__adv.dtx` / `__extr.dtx` / `__mstr.dtx` 与保持 DTXMania 原生配色的 `set.def`（取消勾选则维持单 `.dtx`）。
  - 支持 `#PREVIEW` 两级自动发现、图片三档处理、**非正方形封面自动补黑边 (1:1)**（内置 Pillow，正方形原样直拷不重编码，非正方形按原分辨率居中生成纯黑 1:1 `*_square.png` 并合成透明通道至 `#PREIMAGE`）、实验性 BGA 透传及 `#WAVxx` 行尾 `;LABEL` 自定义；每次导出生成置顶映射存档 **`__bms2dtx.json`** 与 UTF-8 转换日志 **`__bms2dtx_log.txt`**（对外发布曲包时可按需删去，加载旧版 JSON 历史字段静默兼容）。
- **多语言与日志**：支持 **简体中文 / English / 日本語** 实时切换（保存于 `%APPDATA%\bms2dtx\settings.json`），崩溃日志记录于 `%APPDATA%\bms2dtx\crash.log`（可在设置中一键打开）。

---

## 快速上手

1. **导入谱面**：点击左上角按钮选择，或直接将 `.bms` / `.bme` / `.bml` / `.pms` 文件拖入窗口。
2. **分轨映射**：在左侧列表挑选切片（或点击「按文件名猜轨」辅助初始填充后手动复查），点击绿色箭头或按 **`→`** 加入当前目标 Tab；鼓轨可在顶部选择默认绑定芯片（`#DTXC_LANEBINDEDCHIP`）。
3. **配置 `Auto-LP`（可选）**：切至右侧 **`LP`** Tab，选择 `LP 补足` 目标与判定阈值；如需独立调整补足音量，勾选 **`使用 ZY`** 并在底部控制栏设定百分比及点击 **`▶`** 试听。
4. **导出工程**：指定独立的输出目录，确认编码、音频转码、图片处理等选项后点击「开始导出」（默认不再填充易失真的 `#DLEVEL`；生成的 `__bms2dtx.json` 与 `__bms2dtx_log.txt` 可留作回溯检查，对外发布曲包时可按需删除）。

### 常用操作与快捷键

| 操作 / 按键 | 说明 |
| --- | --- |
| `↑` / `↓` / `空格` | 切换或试听当前切片（配合「自动试听」勾选实时发声） |
| `→` / `←` | 加入所选切片 / 从当前选中列表移除（输入框聚焦时不拦截） |
| 点击列表空白区 | 清除当前列表选中状态 |
| `Ctrl+Z` / `Ctrl+Y`（`Ctrl+Shift+Z`） | 撤销 / 重做映射修改（输入框聚焦时不拦截） |

---

## 可选 FFmpeg 说明

内置的 `soundfile` + `soxr` + `sounddevice`（WASAPI Shared / DirectSound / MME）已完整支持常见 `.wav` / `.ogg` / `.flac` / `.mp3` 的试听、重采样、`L/R` 翻转与 `bgm.ogg` 混音。  
仅当需要读取 `.m4a` / `.wma` 等冷门音频，或启用「适配老版 DTX 尺寸（等比缩放 + 补黑边）」图片模式时，才需安装并配置 `ffmpeg.exe`（程序目录 / `bin\` / `PATH` / 设置弹窗）：

```powershell
winget install Gyan.FFmpeg.Essentials
```

---

## 从源码运行、测试与打包

### 安装依赖与启动桌面 GUI

```powershell
pip install -r requirements.txt
python bms2dtx_gui.py
```

### 命令行转换（CLI）

```powershell
# 查看谱面解析摘要（JSON）
python cli.py path\to\chart.bms --inspect

# 执行转换（可选附带映射 JSON 与 .ogg 转码）
python cli.py path\to\chart.bms --out path\to\output_dtx
python cli.py path\to\chart.bms --out path\to\output_dtx --map-json path\to\__bms2dtx.json --transcode-ogg
```

### 运行单元测试（严格零警告模式）

```powershell
python -W error -m unittest discover -s tests -p "test_*.py" -v
```

可选本地真谱只读回归测试（需自行指定外部测试目录，未设置时自动跳过）：

```powershell
$env:BMS2DTX_REAL_CHARTS_DIR = "path\to\local_charts"
python -W error -m unittest tests/test_real_charts_batch.py -v
```

### 一键打包单文件 `dist\bms2dtx.exe`

```powershell
powershell -ExecutionPolicy Bypass -File .\build_exe.ps1 -CleanBuildDir
```

脚本将构建无控制台窗口的单文件可执行程序并自动执行 `--smoke-test` 冒烟验证。
