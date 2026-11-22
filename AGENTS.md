# Project: bms2dtx (`bms2dtx.exe`)

## 1. 项目目标与版本约定
- 构建一个轻量级 Windows 桌面工具，将 BMS / BME / BML / PMS 谱面转换为 DTXMania / DTXCreator 可读取的 `.dtx` 文件，并支持通过 PyInstaller 打包为单文件无黑框可执行程序 `dist/bms2dtx.exe`（同时保留可运行的 Python CLI、源码与单元测试）。
- 当前版本号规范走 **`0.x.y`**（当前版本 **`0.4.2`**，定义于 `gui/__init__.py` 的 `APP_VERSION`）。

---

## 2. 铁律与绝对约束
1. **原始文件 100% 只读**：严禁修改、重命名或覆盖用户输入的原始 `.bms` / `.bme` 文件及 BMS 母文件夹中的任何现有文件。所有后缀修正、转码、音频混音、图片缩放、静音补位与 `__bms2dtx.json` 均仅作用于内存或写入指定的输出目录（输出目录绝不能与源谱目录或音频子目录相同）。
2. **编码规则**：
   - 读取 BMS / BME 时，优先使用 `cp932`（Windows Shift-JIS 超集）严格解码，失败再回退 `utf-8-sig`，最后兜底 `cp932` + `errors="replace"`。
   - 导出 `.dtx` 默认使用 `cp932` 以兼容传统 DTXCreator / DTXMania；提供 `--utf8` 参数或界面勾选框输出 UTF-8。
3. **母文件夹媒体后缀校验与自动修正**：
   - 解析 `#WAVxx`、`#PREVIEW`、`#STAGEFILE`、`#BACKBMP`、`#BMPxx`/`#AVIxx` 时，检查对应文件在 BMS 母文件夹中是否存在（不区分大小写）。
   - 若引用后缀不存在但同 stem 下存在其他受支持后缀，则在**内存中**自动修正为磁盘上真实存在的后缀。
   - 缺失音频（`exists=False` / `resolved_path is None`）**严禁**参与物理文件去重合并，必须各自按原 `#WAVxx` ID 独立保留。
4. **BGM 列号与 iBMSC / μBMSC 严格对齐**：
   - 解析每小节的 `01`（BGM）通道行时，**绝对不要在解析阶段丢弃 `:00` 占位行**。同一小节内按出现顺序第 1、2、3... 条 `01` 行分别对应 `B1`、`B2`、`B3`...
   - 统计全曲所有轨道时，**自动剔除全曲全程为空的轨道**。
5. **进制检测（Base-36 vs `#BASE 62`）与 `ZY` / `ZZ` 槽位永久保留**：
   - 常规导出槽位永久保留 **`ZY`**（`Auto-LP` 实音/映射音频槽）与 **`ZZ`**（`Auto-LP` 空音占位槽 `_silent_zz.wav`），常规可用切片上限为 **`1293`**（`01`..`ZX`；Toggle = OFF 模式下 `01` 留给 `bgm.ogg`，用户最多映射 **`1292`** 个去重切片 `02`..`ZX`）。
   - 即使原 BMS 谱面声明了 `#WAVZY` 或 `#WAVZZ`，导出重排也一律将其重映射至 `01`..`ZX` 区间内的其他空槽位，杜绝与 `Auto-LP` 冲突。
   - 检测到 `#BASE 62`、隐式大小写索引碰撞或切片总数 `> 1293` 时，提示用户并**强制锁定 Toggle = OFF**（UI 当前也固定仅开放 Toggle = OFF 主流程，继续隐藏 `Toggle = ON` 入口）。
6. **通道合法性与边界约束**：
   - 绝不允许写入不存在的 `SE33`（`93`）及以上通道，绝不允许将多条独立音轨硬塞进同一小节的 `01`（BGM）轨做多行叠加。
   - 遇到 `#STOP` / `09` 通道或未展开的 `#RANDOM` / `#IF` / `#SWITCH` 条件分支时，记录清晰警告。
   - 当 `separate_lbd=False`（默认隐藏 LBD Tab）时，若配置中仍残留 `LBD` 分配或绑定，导出时确定性地将其合并入 `BD`，绝不在 `1C` 轨生成幽灵音符。
7. **内置音频引擎与 `ffmpeg` 可选化底线**：
   - 核心解码、混音、OGG Vorbis 编码、异采样率重采样与立体声 `Swap L/R` 均优先走进程内 **`soundfile` (`libsndfile`) + `soxr`（NumPy 线性兜底）**，常规 `.wav`/`.ogg`/`.flac`/`.mp3` 导出与试听在零 FFmpeg 环境下必须 100% 跑通。
   - 试听端（`gui/preview_player.py`）使用 **`sounddevice` (PortAudio)** 在 Windows 上**明确拉黑 `Windows WDM-KS`**，按 **`Windows WASAPI` Shared（`WasapiSettings(exclusive=False, auto_convert=True)`）→ `Windows DirectSound` → `MME`** 顺序回退选择默认输出设备，并在播放前强制将单声道扩至双声道、`soxr` 重采样至设备 `default_samplerate`；无声卡/设备异常时安全降级、绝不崩溃；`swap_lr` 直接在内存 NumPy 数组翻转且不持久化到全局 `settings.json`（打开新谱面时强制重置为未勾选）。
   - `find_ffmpeg` 仅作为冷门音频格式（如 `.m4a`/`.wma`）与老版图片缩放的可选外挂：在 `sys.frozen` 及开发环境下优先查找 `Path(sys.executable).resolve().parent` 及 `bin/` 子目录，再回退系统 `PATH` 与设置弹窗；Windows 下所有调用 `ffmpeg` / `ffprobe` 的 `subprocess` 必须传入 `CREATE_NO_WINDOW`。
8. **`.bml` 长押与 `.pms` 9 键解析规范**：
   - 可见普通音符通道覆盖 `11`–`19` 与 `21`–`29`（支持 `.pms` 9 键），文件过滤与拖放支持 `*.bms;*.bme;*.bml;*.pms`。
   - `.bml` 长押：`#LNTYPE 1`（`51`–`59` / `61`–`69` 按轨成对，未显式声明 `#LNTYPE` 时默认按 `#LNTYPE 1` 处理）奇数位起音发声、偶数位尾标静默不计数；`#LNTYPE 2`（连续同 ID）仅首音发声；`#LNOBJ xx` 指定的终点标记仅在可见键位通道（`11`–`19`/`21`–`29`）彻底静默、不计入 `use_count`（绝不误杀 `01` BGM 轨）。
9. **`tkinterdnd2` 原生拖放与双层崩溃日志规范**：
   - 废弃手写 `ctypes` `WM_DROPFILES` 窗口子类化，统一采用 `tkinterdnd2`（原生 `tkdnd` C 扩展 + Windows OLE 拖放，`root.tk.splitlist(event.data)` 解析含空格与中日文路径），无头环境静默降级，`bms2dtx.spec` 仅打包 `win-x64` `tkdnd` 二进制。
   - 程序入口启用 `%APPDATA%\bms2dtx\crash.log`（超 1 MB 自动截断）：第 1 层 `faulthandler.enable(file=..., all_threads=True)` 捕获 C 级崩溃堆栈；第 2 层接管 `sys.excepthook`、`threading.excepthook` 与 `root.report_callback_exception` 记录带时间戳与版本号的完整 traceback，并在设置弹窗提供「打开日志目录」按钮。

---

## 3. BMS / DTX 通道与 `Auto-LP` 规范速查
- **DTX 左侧鼓轨**：
  - `11` HH Close (CHH) | `12` SD | `13` BD | `14` HT | `15` LT | `16` CY | `17` FT | `18` HH Open (OHH) | `19` RD | `1A` LC | `1B` LP | `1C` LBD
- **DTX BGM / SE 轨**：
  - `01` BGM（配合 `#BGMWAV: 01`）；`SE1`–`SE32`（`61`–`92`）：`SE6`（`66`）固定归 `Other`，`SE11`–`SE12`（`71`–`72`）优先归 `Guitar`，`SE13`–`SE14`（`73`–`74`）优先归 `Bass`，留空即释放回 Collapse 池。
- **DTXC 绑定芯片**：
  - `#DTXC_LANEBINDEDCHIP: <lane> <main_wav> <sub_wav>`（`01`=LC、`02`=HHC/HHO、`03`=LP/LBD、`04`=SD、`05`=HT、`06`=BD、`07`=LT、`08`=FT、`09`=CY/RD）。`separate_lbd=False` 且 `LBD` 未独立绑定时，`BD` 默认芯片自动同步填入 `03` 轨副槽。
- **`Auto-LP`（OHH `18` → CHH `11` 自动补足 LP `1B` 状态机）**：
  - 配置字段：`chip_mode`（`none` / `auto` / `empty_zz` / 指定 LP 切片）、`volume`（`0..100`，默认 `50`）、`threshold_mode`（`note_value` 默认 / `ms`）、`note_num`/`note_den`（默认 `1/4`，分母可选 `2, 4, 6, 8, 12, 16`）、`threshold_ms`（默认 `250`）。
  - 选音回退链（`chip_mode == "auto"`）：优先取 `LP` Tab 默认绑定芯片；若为“无”则回退 `LP` Tab 谱面使用次数最多切片；若 `LP` Tab 为空则安全退化为不插入。
  - 槽位与音量输出：选真实 LP 音频时写入 `#WAVZY: <路径>\t;<LP Label>` + `#VOLUMEZY: <0~100>`，以 `ZY` 写入 `1B`；选 `empty_zz` 时生成 `_silent_zz.wav`（10ms 16-bit mono PCM 静音），写入 `#WAVZZ: _silent_zz.wav\t;LP_GHOST` + `#VOLUMEZZ: 0`，以 `ZZ` 写入 `1B`。
  - 3 条物理防误插规则（阈值判定均含等号 `<=`）：
    1. 仅在 `OHH` 之后的**第一个** `CHH` 处触发，后续连续 `CHH` 不重复插入；
    2. 若 `OHH` 与该 `CHH` 之间或该 `CHH` 同一拍位**已存在原谱 LP 音符**，跳过不插入、绝不覆盖原谱；
    3. 连续多个 `OHH`（`OHH₁ → OHH₂ → CHH`）时，以最近的 `OHH₂` 作为间距计算起点。

---

## 4. 桌面 GUI 与导出能力要点
- **主流程与界面**：支持点击与 `tkinterdnd2`（`tkdnd` 原生 OLE 拖放，支持 `.bms`/`.bme`/`.bml`/`.pms` 完整 Unicode 路径）拖入导入；左侧 5 列 `ttk.Treeview`（`ID` / `文件名` / `次数` / `目标轨` / `L/R`，扩展多选置灰 + 中间栏 `⇄ L/R` 单切片/批量声道翻转按钮 + 2 行底部控制栏 + 悬停 Tooltip + 左右列表联动 WASAPI Shared 单音试听）；`Ctrl+Z` / `Ctrl+Y` / `Ctrl+Shift+Z` 撤销重做栈（输入控件聚焦时不抢占）与未导出 `dirty` 关闭拦截。
- **LP Tab 双行扩展**：仅在 `LP` Tab 顶部显示默认绑定 + 阈值模式单选（第 1 行）与 `LP 补足` 下拉 / 音量微调框 / 阈值输入框（第 2 行），并为 `LP 补足` 控件挂载 **1000ms** 延迟悬停 Tooltip。
- **格式与媒体增强**：
  - 原生 `#GENRE:`（`;` 转全角 `；`）、固定 `#COMMENT: via bms2dtx`、`#WAVxx` 行尾 `\t;<LABEL>` 注释（支持自定义与清洗）；
  - 2 级 `#PREVIEW`（先头部显式定义，未定义则扫描母目录未入谱 `preview*` 音频）；
  - 全局 `Swap L/R` 与单切片 `⇄ L/R` 异或（`effective_swap = bool(global_swap_lr) ^ bool(item_override)`）左右声道互换（试听、`bgm.ogg` 单切片解码阶段翻转、`#PREVIEW` 及保留切片；单声道切片原样复制）；
  - `#STAGEFILE` / `#PREIMAGE` / `#BACKGROUND` 三档图片处理（原图直拷 / `204x269`+`640x480` 等比缩放补黑边 / 不搬运）与实验性 BGA 透传（视频转 `#AVIxx` + `54` 轨，图片保持 `#BMPxx` + `04`/`07` 轨）；
  - 设置弹窗语言标签固定为跨语言 **`🌐 Language:`**，底部带「打开日志目录」按钮，支持三语（`zh_CN` / `en` / `ja`）热切换与 `%APPDATA%\bms2dtx\settings.json` 持久化。
- **导出自动存档**：每次成功导出自动在输出目录根目录写入完整工程映射 **`__bms2dtx.json`**；界面按钮「另存为 JSON...」保留手动另存，「载入映射 JSON...」默认定位至输出目录。

---

## 5. Git 分支、测试脱敏与协同规则
1. **Git 分支与安全规范**：
   - 新功能或重构**必须**在独立特性分支 `feat/*` 上开发；仅当全量单元测试（`-W error`）与 PyInstaller 打包及冒烟测试全部通过后，再切回 `main` 合并并清理分支。
   - **严禁执行 `git push --force`**、严禁重写已有历史提交；初始化新仓库时可配好 `--local`，后续绝不改动 `git config --global`。
2. **测试与脱敏规范**：
   - **严禁**在 `tests/` 或仓库源码中硬编码任何私人曲包名称或个人本地绝对路径。
   - 可选的真实谱面批量回归测试通过环境变量 `BMS2DTX_REAL_CHARTS_DIR` 动态读取（`tests/test_real_charts_batch.py`），未设置该环境变量时自动 `skip`。
   - `.gitignore` 必须过滤构建产物、媒体/谱面文件以及本地测试曲包目录，避免版权素材误入版本库。
3. **AgentLimb / 浏览器 MCP 协同熔断规则**：
   - **仅当**用户提示词明确要求联系网页端领航员时，才可启用浏览器 / AgentLimb MCP；无人值守或离线任务严禁擅自调用。
   - 启用期间，单次浏览器 / AgentLimb MCP 调用若发生超时或报错，**最多重试 1 次**；若仍失败，立即停止调用并降级为本地直接收尾汇报后停机，严禁在满载上下文下循环重试消耗额度。
