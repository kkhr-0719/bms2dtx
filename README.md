# bms2dtx

轻量级 Windows 桌面与命令行工具，用于将 **BMS / BME / BML** 谱面高效转换为 **DTXMania / DTXCreator** 可直接编辑和游玩的 `.dtx` 工程目录（Toggle = OFF 离线混音工作流）。

---

## 核心特性

- **零损只读原谱**：绝不修改或覆盖原始 BMS 文件夹内的任何文件，自动在独立输出目录生成完整 `.dtx` 与资源。
- **智能解析与内存修正**：
  - 编码优先级：`CP932` → `UTF-8-BOM` → `CP932(replace)`；导出支持传统 `CP932` 与 `UTF-8`。
  - 自动在内存中修正扩展名不一致的音频/图片/BGA（例如谱面写 `.wav` 而磁盘为 `.ogg`/`.flac`/`.mp3`/`.xa`）。
  - 严格保留 `:00` 占位行以对齐 iBMSC / μBMSC 的 `B1..Bn` BGM 列号，同时剔除全曲空轨。
  - 完整支持 `#BASE 62` 与超限检测、精确 BPM/exBPM 变速与粘性 `02` 拍号还原、`#LNTYPE 1/2` 与 `#LNOBJ` 长条去重触发。
- **15-Tab 视觉映射与单音试听**：
  - 覆盖 12 鼓轨（`LC`, `HH Close`, `HH Open`, `LP`, `LBD`, `SD`, `HT`, `BD`, `LT`, `FT`, `CY`, `RD`）及 `Other`（`SE6`）、`Guitar`（`SE11-12`）、`Bass`（`SE13-14`），默认隐藏并自动合并 `LBD` 至 `BD`（可勾选单独定义）。
  - 左侧 4 列 `Treeview` 支持扩展多选、已分配置灰提示、同名物理文件自动折叠去重、按文件名/片假名/编号序列（`tom1/tom2/tom3`）智能猜轨，以及左/右列表联动单音试听与 `Ctrl+Z` / `Ctrl+Y` 撤销重做。
- **`Auto-LP`（开镲转闭镲自动补足左脚踩镲）**：
  - 永久保留 `#WAVZY` 与 `#WAVZZ` 槽位。当从开镲（`OHH / 18`）转为闭镲（`CHH / 11`）且间隔 `<=` 设定阈值（支持**乐理音符时值**如 `1/4` 或**物理毫秒 `ms`**），且期间及闭镲同拍位没有原谱 `LP` 时，自动在闭镲位补入 `1B` 音符（可选真实 LP 切片音量 `0..100` 写入 `ZY`，或生成 10ms 极短静音 `_silent_zz.wav` 写入 `ZZ`）。
- **高质量离线混音与导出增强**：
  - 未选入 15-Tab 的全部剩余音符与同轨并发溢出音符，按精确时间轴离线混音导出为高质量 `bgm.ogg`（同拍同 ID 防相位爆音 + 仅向下峰值防削波保护）。
  - 支持全局 **`左右声道互换 (Swap L/R)`**（同步作用于试听、`bgm.ogg`、`#PREVIEW` 及保留立体声切片，单声道切片直拷零重编码）。
  - 支持原生 `#GENRE:`、`#PREVIEW` 两级发现、封面/背景图三档适配（原图直拷 / `204x269`+`640x480` 补黑边 / 不搬运）、实验性 BGA 视频/图片透传，以及 `#WAVxx` 行尾 `\t;<LABEL>` 注释自定义。
  - **内置工程自动存档**：每次导出自动在输出目录生成 `__bms2dtx.json`（含分轨、默认芯片、`Auto-LP` 与 `Swap L/R` 参数），随时可用「载入映射 JSON...」一键还原重导。
  - 支持 **简体中文 / English / 日本語** 界面实时热切换，设置持久化保存于 `%APPDATA%\bms2dtx\settings.json`。

---

## FFmpeg 配置

程序打包时**不内嵌** `ffmpeg.exe` 体积，运行离线混音（`bgm.ogg`）、`.wav → .ogg` 转码、图片缩放、非 `.wav` 试听及 `Swap L/R` 时需依赖 `ffmpeg`。程序按以下优先级自动探测：

1. 可执行文件同级目录（`bms2dtx.exe` 旁）及其 `bin/` 子目录；
2. 系统环境变量 `PATH`；
3. GUI 右上角 **`⚙ 設定 / Config`** 弹窗内手动指定路径或一键复制安装命令：

```powershell
winget install Gyan.FFmpeg.Essentials
```

---

## 界面使用简要指南

1. **导入谱面**：点击左上角打开文件，或直接将 `.bms` / `.bme` / `.bml` 文件拖入窗口。
2. **分轨映射**：
   - 在左侧列表选择切片（`Shift`/`Ctrl` 多选，或用「按文件名猜轨」辅助初始填充），按中间绿色箭头加入右侧当前 Tab。
   - 右侧 12 个鼓轨支持选择默认绑定芯片（用于生成 `#DTXC_LANEBINDEDCHIP`，默认自动挑选使用频率最高切片）。
3. **配置 `Auto-LP`（可选）**：
   - 切换至右侧 **`LP`** Tab，第 1 行可选择判定阈值模式（`音符时值` / `ms`），第 2 行在 **`LP 补足:`** 选择 `无`、`自动`、`ZZ: 空音` 或具体 LP 切片，设定音量（`0~100`）与上限阈值（鼠标悬停 1 秒可查看说明）。
4. **导出与回溯修改**：
   - 确认输出目录、编码、`#DLEVEL`、图片处理方式等选项后点击「开始导出」。
   - 导出后输出目录顶部的 **`__bms2dtx.json`** 会完整记录本次方案；如需微调，直接通过「载入映射 JSON...」（默认已定位到输出目录）一键加载即可。对外发布曲包时可按需删去该工程 JSON 文件。

---

## 从源码运行、测试与打包

### 运行桌面 GUI
```powershell
python bms2dtx_gui.py
```

### 运行命令行转换（CLI）
```powershell
python cli.py path\to\chart.bms --out path\to\output_dtx
# 携带已保存的映射 JSON：
python cli.py path\to\chart.bms --out path\to\output_dtx --map-json path\to\__bms2dtx.json --transcode-ogg
```

### 运行单元测试（严格零警告模式）
```powershell
python -W error -m unittest discover -s tests -p "test_*.py" -v
```
如希望额外对本地真实谱面目录执行可选的批量只读回归测试，可设置环境变量后运行：
```powershell
$env:BMS2DTX_REAL_CHARTS_DIR = "D:\Charts\SomeBMSSong"
python -W error -m unittest tests/test_real_charts_batch.py -v
```

### 一键打包单文件 `dist\bms2dtx.exe`
```powershell
powershell -ExecutionPolicy Bypass -File .\build_exe.ps1 -CleanBuildDir
```
打包脚本会自动构建无控制台窗口单文件 `.exe`（若存在 `assets\icon.ico` 则自动嵌入图标），并运行无黑框 `--smoke-test` 验证。
