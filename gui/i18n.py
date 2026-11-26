"""Multi-language internationalization (i18n) dictionary and preference persistence for bms2dtx GUI."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from core.constants import DEFAULT_WAV_LABELS, WAV_LABEL_KEYS, sanitize_wav_label

SUPPORTED_LANGUAGES: Tuple[Tuple[str, str], ...] = (
    ("zh_CN", "简体中文"),
    ("en", "English"),
    ("ja", "日本語"),
)

LANGUAGE_CODES: Tuple[str, ...] = tuple(code for code, _label in SUPPORTED_LANGUAGES)
LANGUAGE_LABELS: Dict[str, str] = {code: label for code, label in SUPPORTED_LANGUAGES}
LABEL_TO_LANGUAGE: Dict[str, str] = {label: code for code, label in SUPPORTED_LANGUAGES}

DEFAULT_LANGUAGE = "zh_CN"
_current_language: str = DEFAULT_LANGUAGE

TRANSLATIONS: Dict[str, Dict[str, str]] = {
    "zh_CN": {
        "open_bms_btn": "选择 .bms / .bme / .bml / .pms 文件",
        "no_chart_selected": "点击选择或拖入 .bms / .bme / .bml / .pms 文件",
        "config_btn": "⚙ 設定 / Config",
        "ffmpeg_warn_badge": "⚠ ffmpeg 未就绪",
        "ffmpeg_ok": "内置音频引擎已就绪（WASAPI Shared）；FFmpeg（可选）: {path}",
        "ffmpeg_missing_short": "内置音频引擎已就绪（WASAPI Shared），FFmpeg 仅用于冷门格式与老版图片缩放（可选，当前未配置）",
        "pick_ffmpeg_btn": "指定 ffmpeg.exe...",
        "lang_label": "🌐 Language:",
        "filetypes_bms": "BMS / BME / BML / PMS 谱面",
        "filetypes_all": "所有文件",
        "health_frame_title": "谱面体检",
        "health_placeholder": "导入谱面后将在此显示：标题 / 艺术家 / BPM / 编码 / 进制 / WAV 统计 / BGM 列统计 / 警告",
        "view_warnings_btn": "查看警告 ({count})",
        "health_title": "标题",
        "health_artist": "艺术家",
        "health_bpm": "BPM",
        "health_encoding": "编码",
        "health_radix": "进制",
        "radix_base36_ok": "Base-36 兼容",
        "radix_base62_ext": "Base-62 (扩展)",
        "health_wav_count": "WAV 数量",
        "health_wav_value": "共 {total} / 后缀已内存修正 {corrected} / 缺失 {missing}",
        "health_bgm_cols": "BGM 列",
        "health_bgm_value": "非空 B1..Bn 共 {count} 列 / 已剔除空轨 {pruned}",
        "health_warnings": "警告",
        "health_warnings_value": "{count} 条",
        "warn_tag_prefix": "导入警告：",
        "warn_tag_undef_wav": "未声明定义",
        "warn_tag_missing_audio": "缺失音频",
        "warn_tag_stop": "含 #STOP",
        "warn_tag_random": "未展开 #RANDOM",
        "warn_tag_unclosed_ln": "未闭合长押",
        "warn_tag_base62": "62 进制",
        "warn_tag_wav_limit": "切片超限",
        "warn_tag_missing_preview": "缺失试听",
        "warn_tag_other": "其他 {count} 项",
        "radix_warn_base62": "  ⚠ 检测到 62 进制，超出 DTX 原生 36 进制上限",
        "radix_warn_limit": "  ⚠ 切片总数 > 1293（v0.3 固定 Toggle = OFF 离线混音 bgm.ogg）",
        "left_frame_title": "源 #WAV 列表（Shift/Ctrl 多选；单击 / 方向键 / 空格 试听）",
        "col_id": "ID",
        "col_filename": "文件名",
        "col_count": "次数",
        "col_target": "目标轨",
        "col_lr": "L/R",
        "search_label": "搜索:",
        "clear_search_btn": "清除",
        "only_used_chk": "仅显示被谱面使用的切片",
        "play_btn": "▶ 试听",
        "stop_btn": "■ 停止",
        "auto_preview_chk": "自动试听",
        "auto_preview_tooltip": "勾选后，单击左/右列表项或按方向键上下移动时会自动试听单个切片",
        "swap_lr_chk": "所有切片左右声道互换",
        "left_count_summary": "显示 {shown} / {total} · 已分配 {assigned}",
        "tag_missing": "[缺失]",
        "tag_undeclared": "(未声明)",
        "add_btn": "➜ 加入",
        "remove_btn": "✖ 移除",
        "swap_slice_lr_btn": "⇄ L/R",
        "swap_slice_lr_tooltip": "切换所选切片的独立左右声道互换状态（与全局左右声道互换做异或 XOR 计算）",
        "guess_btn": "按文件名猜轨",
        "clear_all_btn": "清空全部分配",
        "right_frame_title": "15 Tab 映射（Tab 1–12 鼓 / 13 Other / 14 Guitar / 15 Bass）",
        "separate_lbd_chk": "单独定义 LBD",
        "default_bind_label": "默认绑定 WAV (#DTXC_LANEBINDEDCHIP):",
        "lp_default_bind_short": "默认绑定:",
        "auto_lp_label": "LP 补足:",
        "auto_lp_chip_none": "无",
        "auto_lp_chip_auto": "自动",
        "auto_lp_chip_auto_prefix": "[自动] ",
        "auto_lp_chip_empty_zz": "ZZ: 空音",
        "auto_lp_vol_label": "音量:",
        "auto_lp_use_zy_chk": "使用 ZY",
        "auto_lp_use_zy_tooltip": "勾选后使用独立保留槽位 #WAVZY + #VOLUMEZY 控制补足音量；不勾选则直接复用原 LP 切片槽位（100% 原音量）",
        "auto_lp_play_tooltip": "按当前音量与生效的 L/R 声道状态试听 Auto-LP 目标切片",
        "auto_lp_target_none": "(未启用 Auto-LP)",
        "auto_lp_target_zz": "ZZ _silent_zz.wav (空音)",
        "auto_lp_target_no_slice": "(LP / HH Close 轨无可用切片)",
        "auto_lp_mode_note": "音符时值",
        "auto_lp_mode_ms": "ms",
        "auto_lp_tooltip": (
            "当开镲 (OHH, 18) 转为闭镲 (CHH, 11) 且间距 <= 设定阈值，且期间与闭镲同拍位均无原谱 LP 时，"
            "自动在闭镲位同步补入左脚踩镲 (LP, 1B) 音符（使用永久保留槽位 ZY 或静音占位 ZZ）。"
        ),
        "no_bind_label": "(未指定 / 00)",
        "auto_bind_prefix": "[自动最高频] ",
        "status_bind_auto_label": "自动最高频",
        "tab_note_other": "固定分配至 SE6 (通道 66)",
        "tab_note_guitar": "可选：留空即禁用并自动释放 SE11–SE12",
        "tab_note_bass": "可选：留空即禁用并自动释放 SE13–SE14",
        "save_map_btn": "另存为 JSON...",
        "load_map_btn": "载入映射 JSON...",
        "export_frame_title": "导出",
        "output_dir_label": "输出目录:",
        "browse_btn": "浏览...",
        "start_export_btn": "开始导出",
        "open_output_btn": "打开输出文件夹",
        "utf8_chk": "导出 UTF-8 编码 (--utf8)",
        "transcode_ogg_chk": "将保留的 .wav 切片自动转码为 .ogg",
        "four_diffs_chk": "生成四难度底谱与 set.def",
        "dlevel_label": "鼓难度 (#DLEVEL):",
        "image_mode_label": "图片处理:",
        "image_mode_keep": "保持原图直接搬运",
        "image_mode_resize": "适配老版 DTX 尺寸（等比缩放 + 补黑边）",
        "image_mode_none": "不搬运图片",
        "pad_square_preimage_chk": "非正方形封面自动补黑边 (1:1)",
        "pad_square_preimage_tooltip": "将非 1:1 的 #STAGEFILE 封面按原分辨率居中补纯黑边生成 1:1 #PREIMAGE（已是正方形则原样复制不重编码）",
        "export_bga_chk": "实验性 BGA 透传",
        "settings_title": "設定 / Settings",
        "settings_general_section": "界面语言",
        "settings_ffmpeg_section": "音频引擎与 FFmpeg（可选）",
        "settings_ffmpeg_redetect_btn": "自动重新检测",
        "settings_ffmpeg_hint": "如需解码 .m4a/.wma 等冷门格式或启用老版图片缩放，可将 ffmpeg.exe 放在程序同级目录或 bin\\，或运行：",
        "settings_winget_cmd": "winget install Gyan.FFmpeg.Essentials",
        "settings_copy_cmd_btn": "复制命令",
        "settings_copied_cmd": "已复制 winget 命令",
        "settings_bind_section": "默认绑定芯片策略 (#DTXC_LANEBINDEDCHIP)",
        "settings_bind_auto": "自动选择最高频切片",
        "settings_bind_manual": "不自动绑定（手动指定）",
        "settings_labels_section": "#WAVxx 导出行尾 ;LABEL 注释",
        "settings_reset_labels_btn": "恢复默认",
        "settings_allow_left_remove_chk": "允许在左侧 WAV 列表选中后点击「移除」取消轨道分配",
        "settings_export_drums_ogg_chk": "导出 drums.ogg 鼓组干声 (音量 10)",
        "settings_open_logs_btn": "打开日志目录",
        "settings_save_btn": "保存并关闭",
        "settings_cancel_btn": "取消",
        "status_ready": "就绪",
        "status_undone": "已撤销上一步映射操作",
        "status_redone": "已重做映射操作",
        "status_slice_lr_toggled": "已切换 {count} 个切片的左右声道互换状态",
        "status_logs_opened": "已打开日志目录: {path}",
        "dialog_crash_logged_title": "运行异常已记录",
        "dialog_crash_logged_body": "发生未预期的错误:\n{exc}\n\n详细崩溃堆栈已写入日志:\n{path}",
        "status_bind_updated": "{tab} 默认绑定: {wav}",
        "status_select_left_first": "请先在左侧列表选择要加入的切片",
        "status_added_to_tab": "已加入 {added} 个切片到 {tab}",
        "status_added_skipped_suffix": "（{skipped} 个已分配至其他 Tab，已跳过）",
        "status_select_right_first": "请先在右侧当前 Tab 选择要移除的切片",
        "status_removed_from_tab": "已从 {tab} 移除 {removed} 个切片",
        "status_unassigned_from_left": "已取消 {removed} 个切片的轨道分配",
        "status_cleared_all": "已清空全部分配",
        "status_guessed": "按文件名猜轨：已填充 {total} 个未分配切片（请检查结果）",
        "status_select_slice_first": "请先选择一个切片",
        "status_file_missing_no_preview": "[{wav_id}] 文件缺失，无法试听",
        "status_parsing": "解析中: {name} ...",
        "status_parse_failed": "解析失败",
        "status_loaded": "已载入 {name}：{wavs} 个 #WAV，{events} 个音符",
        "status_exporting": "导出中（离线混音可能需要数十秒）...",
        "status_export_done": "导出完成: {path}",
        "status_export_failed": "导出失败",
        "status_ffmpeg_enabled": "已启用 ffmpeg: {path}",
        "status_map_saved": "映射已保存: {path}",
        "status_map_loaded": "映射已载入（{count} 个切片）",
        "dialog_close_btn": "关闭",
        "dialog_warnings_title": "谱面警告 ({count})",
        "dialog_ffmpeg_missing_title": "未找到 ffmpeg",
        "dialog_ffmpeg_missing_body": (
            "未在程序目录 / bin 子目录 / 系统 PATH 中找到 ffmpeg.exe。\n\n"
            "在指定有效的 ffmpeg.exe 之前，以下功能被禁用：\n"
            "  • 离线混音导出 bgm.ogg\n  • .wav → .ogg 转码\n  • 非 .wav 切片试听 / 左右声道互换\n\n"
            "请点击右上角“⚙ 設定 / Config”指定 ffmpeg.exe，或将其放在 bms2dtx.exe 旁边 / bin 文件夹。"
        ),
        "dialog_ffmpeg_invalid_title": "ffmpeg 无效",
        "dialog_ffmpeg_invalid_body": "所选文件不是可用的 ffmpeg:\n{exc}",
        "dialog_file_not_found_title": "文件不存在",
        "dialog_file_not_found_body": "找不到文件:\n{path}",
        "dialog_drop_invalid_title": "不支持的文件类型",
        "dialog_drop_invalid_body": "请拖入 .bms / .bme / .bml / .pms 谱面文件:\n{name}",
        "dialog_radix_title": "进制提示",
        "dialog_radix_body": (
            "检测到 Base-62 谱面或切片总数超过 DTX 36 进制可用上限 (1293，其中 ZY/ZZ 保留给 Auto-LP)。\n\n"
            "GUI 固定使用 Toggle = OFF：只有你选入 15 个 Tab 的切片会保留为独立切片，\n"
            "其余音符将离线混音为 bgm.ogg。"
        ),
        "dialog_clear_title": "清空分配",
        "dialog_clear_body": "确定清空全部 Tab 的分配？",
        "dialog_dirty_close_title": "未导出的映射修改",
        "dialog_dirty_close_body": "当前谱面有尚未导出的轨道映射修改，确定直接关闭程序吗？",
        "dialog_guess_none_body": "没有根据文件名识别出的未分配切片。",
        "dialog_guess_confirm_body": (
            "当前已有 {assigned} 个手动分配。\n\n"
            "将仅为 {total} 个【尚未分配】的切片按文件名填充，已有分配不会被覆盖。继续？"
        ),
        "dialog_guess_done_title": "自动猜轨完成",
        "dialog_guess_done_summary": "已按文件名与声像规则自动分配 {total} 个切片。",
        "dialog_guess_done_warning": (
            "注意：自动分配仅基于文件名规则与基础声像的非智能检测，存在误判可能，强烈建议人工逐轨筛选并复查一遍。"
        ),
        "dialog_load_map_title": "载入映射",
        "dialog_load_map_first_body": "请先导入谱面。",
        "dialog_save_failed_title": "保存失败",
        "dialog_load_failed_title": "载入失败",
        "dialog_map_notes_title": "映射载入提示",
        "dialog_invalid_out_title": "输出目录无效",
        "dialog_select_out_title": "输出目录",
        "dialog_select_out_body": "请选择输出目录。",
        "dialog_dlevel_err_title": "#DLEVEL",
        "dialog_dlevel_err_body": "#DLEVEL 必须为非负整数（或留空）。",
        "dialog_export_no_ffmpeg_body": (
            "未找到 ffmpeg：将无法离线混音 bgm.ogg，导出的 .dtx 会引用不存在的 bgm.ogg。\n\n"
            "仍要只导出 .dtx 与保留的切片吗？（选“否”可先前往设置指定 ffmpeg.exe）"
        ),
        "dialog_export_resize_no_ffmpeg_body": (
            "当前选择了“适配老版 DTX 尺寸（等比缩放 + 补黑边）”，但本机未配置 ffmpeg.exe。\n\n"
            "若继续导出，图片将按原图直接复制；选“否”可先在右上角“⚙ 設定 / Config”中配置 ffmpeg.exe。"
        ),
        "dialog_export_done_title": "导出完成",
        "dialog_open_folder_err_title": "打开文件夹失败",
        "no_warnings_text": "无警告。",
        "err_out_same_as_src": "输出目录不能是 BMS 源文件夹本身。请选择一个独立的输出文件夹。（铁律 1：原始 BMS 文件夹只读）",
        "err_out_in_audio_subdir": "输出目录不能是 BMS 音频所在的子文件夹。（铁律 1：原始 BMS 文件夹只读）",
        "result_dtx_line": "输出 .dtx: {path}",
        "result_encoding_line": "DTX 编码: {encoding}",
        "result_bgm_line": (
            "bgm.ogg: {path} (时长 {duration:.2f}s, 混入音符 {events}, "
            "峰值 {peak:.3f}, RMS {rms:.4f}, 缺失 {missing})"
        ),
        "result_bgm_none": "bgm.ogg: 未生成",
        "result_drums_line": "drums.ogg: {path} (SE8/通道68, 音量 10, 时长 {duration:.2f}s, 混入 {events} 音符, 峰值 {peak:.3f})",
        "result_set_def_line": "四难度底谱与 set.def: {path} (__bsc / __adv / __extr / __mstr.dtx)",
        "result_preview_line": "#PREVIEW: {path}",
        "result_placed_notes": "已放置音符: {count}",
        "result_auto_lp_line": "Auto-LP 自动补足: {count} 个音符",
        "result_hihat_removed_line": "Hi-Hat 冲突删音 (HH Open 优先): {count} 个",
        "result_tom_se7_line": "Tom 溢出分流 SE7 (67): {count} 个",
        "result_merged_slices_line": "同轨去重混音合并: {unique} 个合成切片（全曲复用 {total} 次）",
        "result_project_json_line": "自动存档映射 JSON: {path}",
        "result_log_line": "转换日志: {path}",
        "result_copied_transcoded": "复制切片: {copied}   转码 .ogg: {transcoded}",
        "result_lane_binds": "#DTXC_LANEBINDEDCHIP 行数: {count}",
        "result_toggle_off_forced": "注意: 已强制 Toggle = OFF",
        "result_warnings_header": "导出警告 ({count}):",
        "result_import_warnings_note": "（另含 {count} 条源谱导入期警告，见顶部「查看警告」及 __bms2dtx_log.txt）",
        "preview_no_winsound": "当前系统不支持试听 (winsound 不可用)",
        "preview_file_missing": "文件不存在，无法试听: {name}",
        "preview_no_ffmpeg": "未找到 ffmpeg: 无法试听非 .wav 切片或启用声道互换 (请指定 ffmpeg.exe)",
        "preview_decoding": "解码中: {name} ...",
        "preview_stopped": "已停止",
        "preview_decode_failed": "解码失败: {exc}",
        "preview_playing": "正在播放: {name}",
        "preview_play_failed": "无法播放 {name}: {exc}",
        "map_note_unknown_tab": "忽略未知 Tab: {tab}",
        "map_note_undeclared_slice": "忽略当前谱面中未声明的切片: {wav_id}",
        "map_note_duplicate_slice": "忽略重复分配的切片: {wav_id}",
        "map_note_unknown_bind_tab": "忽略未知 Tab 的默认绑定: {tab}",
        "map_note_invalid_bind": "忽略无效默认绑定: {tab} -> {wav_id}",
        "map_note_toggle_on_ignored": "映射文件含 toggle_on=true，v1 GUI 仅支持 Toggle = OFF，已忽略。",
    },
    "en": {
        "open_bms_btn": "Open .bms / .bme / .bml / .pms",
        "no_chart_selected": "Click to choose or drag & drop a .bms / .bme / .bml / .pms file",
        "config_btn": "⚙ 設定 / Config",
        "ffmpeg_warn_badge": "⚠ ffmpeg missing",
        "ffmpeg_ok": "Built-in audio engine ready (WASAPI Shared); FFmpeg (optional): {path}",
        "ffmpeg_missing_short": "Built-in audio engine ready (WASAPI Shared); FFmpeg only needed for rare codecs & legacy image resize (optional, not configured)",
        "pick_ffmpeg_btn": "Locate ffmpeg.exe...",
        "lang_label": "🌐 Language:",
        "filetypes_bms": "BMS / BME / BML / PMS Charts",
        "filetypes_all": "All Files",
        "health_frame_title": "Chart Inspection",
        "health_placeholder": "Import a chart to view: Title / Artist / BPM / Encoding / Radix / WAV Stats / BGM Columns / Warnings",
        "view_warnings_btn": "Warnings ({count})",
        "health_title": "Title",
        "health_artist": "Artist",
        "health_bpm": "BPM",
        "health_encoding": "Encoding",
        "health_radix": "Radix",
        "radix_base36_ok": "Base-36 (Standard)",
        "radix_base62_ext": "Base-62 (Extended)",
        "health_wav_count": "WAV Count",
        "health_wav_value": "Total {total} / Suffix auto-fixed {corrected} / Missing {missing}",
        "health_bgm_cols": "BGM Columns",
        "health_bgm_value": "Non-empty B1..Bn: {count} cols / Pruned empty: {pruned}",
        "health_warnings": "Warnings",
        "health_warnings_value": "{count} item(s)",
        "warn_tag_prefix": "Import alerts: ",
        "warn_tag_undef_wav": "Undeclared #WAV",
        "warn_tag_missing_audio": "Missing audio",
        "warn_tag_stop": "Unsupported #STOP",
        "warn_tag_random": "Unexpanded #RANDOM",
        "warn_tag_unclosed_ln": "Unclosed LN",
        "warn_tag_base62": "Base-62",
        "warn_tag_wav_limit": "Slice limit exceeded",
        "warn_tag_missing_preview": "Missing #PREVIEW",
        "warn_tag_other": "+{count} more",
        "radix_warn_base62": "  ⚠ Base-62 detected, exceeds native DTX Base-36 limit",
        "radix_warn_limit": "  ⚠ Total slices > 1293 (Toggle = OFF offline bgm.ogg mix locked)",
        "left_frame_title": "Source #WAV List (Shift/Ctrl multi-select; Click / Arrows / Space to preview)",
        "col_id": "ID",
        "col_filename": "Filename",
        "col_count": "Count",
        "col_target": "Target",
        "col_lr": "L/R",
        "search_label": "Search:",
        "clear_search_btn": "Clear",
        "only_used_chk": "Show only slices used in chart",
        "play_btn": "▶ Play",
        "stop_btn": "■ Stop",
        "auto_preview_chk": "Auto-preview",
        "auto_preview_tooltip": "When checked, clicking or moving with Up/Down arrow keys in either list auto-previews the slice",
        "swap_lr_chk": "Swap L/R for All Slices",
        "left_count_summary": "Showing {shown} / {total} · Assigned {assigned}",
        "tag_missing": "[Missing]",
        "tag_undeclared": "(Undeclared)",
        "add_btn": "➜ Add",
        "remove_btn": "✖ Remove",
        "swap_slice_lr_btn": "⇄ L/R",
        "swap_slice_lr_tooltip": "Toggle per-slice L/R channel swap override for selected slice(s) (XOR with global Swap L/R)",
        "guess_btn": "Guess by Filename",
        "clear_all_btn": "Clear All",
        "right_frame_title": "15-Tab Mapping (Tabs 1–12 Drums / 13 Other / 14 Guitar / 15 Bass)",
        "separate_lbd_chk": "Separate LBD",
        "default_bind_label": "Default Bound WAV (#DTXC_LANEBINDEDCHIP):",
        "lp_default_bind_short": "Default:",
        "auto_lp_label": "Auto-LP:",
        "auto_lp_chip_none": "None",
        "auto_lp_chip_auto": "Auto",
        "auto_lp_chip_auto_prefix": "[Auto] ",
        "auto_lp_chip_empty_zz": "ZZ: Silent",
        "auto_lp_vol_label": "Vol:",
        "auto_lp_use_zy_chk": "Use ZY",
        "auto_lp_use_zy_tooltip": "When checked, emits reserved #WAVZY + #VOLUMEZY to apply custom Auto-LP volume; when unchecked, reuses the LP slice's own WAV slot directly",
        "auto_lp_play_tooltip": "Preview the resolved Auto-LP target slice with current volume and effective L/R swap",
        "auto_lp_target_none": "(Auto-LP disabled)",
        "auto_lp_target_zz": "ZZ _silent_zz.wav (Silent)",
        "auto_lp_target_no_slice": "(No slice in LP / HH Close)",
        "auto_lp_mode_note": "Note Value",
        "auto_lp_mode_ms": "ms",
        "auto_lp_tooltip": (
            "Automatically inserts a left-pedal (LP, 1B) note on the first Closed Hi-Hat (CHH, 11) "
            "after an Open Hi-Hat (OHH, 18) when within the threshold and no original LP exists "
            "between them or on that beat (uses reserved slot ZY or silent placeholder ZZ)."
        ),
        "no_bind_label": "(None / 00)",
        "auto_bind_prefix": "[Auto Most-Used] ",
        "status_bind_auto_label": "Auto (most-used)",
        "tab_note_other": "Fixed to SE6 (Channel 66)",
        "tab_note_guitar": "Optional: leave empty to disable and release SE11–SE12",
        "tab_note_bass": "Optional: leave empty to disable and release SE13–SE14",
        "save_map_btn": "Save JSON As...",
        "load_map_btn": "Load Mapping JSON...",
        "export_frame_title": "Export",
        "output_dir_label": "Output Dir:",
        "browse_btn": "Browse...",
        "start_export_btn": "Start Export",
        "open_output_btn": "Open Output Folder",
        "utf8_chk": "Export UTF-8 encoding (--utf8)",
        "transcode_ogg_chk": "Transcode retained .wav slices to .ogg",
        "four_diffs_chk": "Generate 4-difficulty DTX + set.def",
        "dlevel_label": "Drums Level (#DLEVEL):",
        "image_mode_label": "Images:",
        "image_mode_keep": "Keep original images",
        "image_mode_resize": "Resize for legacy DTX (pad to 204×269 / 640×480)",
        "image_mode_none": "Do not copy images",
        "pad_square_preimage_chk": "Auto-pad non-square cover to 1:1",
        "pad_square_preimage_tooltip": "Pad non-square #STAGEFILE covers with pure black borders at native resolution for 1:1 #PREIMAGE (copies already-square covers verbatim)",
        "export_bga_chk": "Experimental BGA passthrough",
        "settings_title": "Settings / 設定",
        "settings_general_section": "Language",
        "settings_ffmpeg_section": "Audio Engine & FFmpeg (Optional)",
        "settings_ffmpeg_redetect_btn": "Re-detect",
        "settings_ffmpeg_hint": "Optional: for rare formats (.m4a/.wma) or legacy image resize, place ffmpeg.exe beside the app or in bin\\, or run:",
        "settings_winget_cmd": "winget install Gyan.FFmpeg.Essentials",
        "settings_copy_cmd_btn": "Copy",
        "settings_copied_cmd": "winget command copied",
        "settings_bind_section": "Default Bound Chip Strategy (#DTXC_LANEBINDEDCHIP)",
        "settings_bind_auto": "Auto-select most frequent slice",
        "settings_bind_manual": "Do not auto-bind (manual)",
        "settings_labels_section": "#WAVxx Export Inline ;LABEL Comments",
        "settings_reset_labels_btn": "Reset Defaults",
        "settings_allow_left_remove_chk": "Allow removing lane assignments from left WAV list selection",
        "settings_export_drums_ogg_chk": "Export drums.ogg drum stem (Vol 10)",
        "settings_open_logs_btn": "Open Logs",
        "settings_save_btn": "Save & Close",
        "settings_cancel_btn": "Cancel",
        "status_ready": "Ready",
        "status_undone": "Undone last mapping change",
        "status_redone": "Redone mapping change",
        "status_slice_lr_toggled": "Toggled L/R swap on {count} slice(s)",
        "status_logs_opened": "Opened logs folder: {path}",
        "dialog_crash_logged_title": "Error Logged",
        "dialog_crash_logged_body": "An unexpected error occurred:\n{exc}\n\nFull traceback written to:\n{path}",
        "status_bind_updated": "{tab} default bind: {wav}",
        "status_select_left_first": "Please select slice(s) in the left list first",
        "status_added_to_tab": "Added {added} slice(s) to {tab}",
        "status_added_skipped_suffix": " ({skipped} already assigned to other Tabs, skipped)",
        "status_select_right_first": "Please select slice(s) in the current Tab first",
        "status_removed_from_tab": "Removed {removed} slice(s) from {tab}",
        "status_unassigned_from_left": "Unassigned {removed} slice(s) from lanes",
        "status_cleared_all": "Cleared all assignments",
        "status_guessed": "Guessed {total} unassigned slice(s) by filename (please review)",
        "status_select_slice_first": "Please select a slice first",
        "status_file_missing_no_preview": "[{wav_id}] File missing, cannot preview",
        "status_parsing": "Parsing: {name} ...",
        "status_parse_failed": "Parse failed",
        "status_loaded": "Loaded {name}: {wavs} #WAVs, {events} notes",
        "status_exporting": "Exporting (offline mixing may take a few seconds)...",
        "status_export_done": "Export complete: {path}",
        "status_export_failed": "Export failed",
        "status_ffmpeg_enabled": "Enabled ffmpeg: {path}",
        "status_map_saved": "Mapping saved: {path}",
        "status_map_loaded": "Mapping loaded ({count} slice(s))",
        "dialog_close_btn": "Close",
        "dialog_warnings_title": "Chart Warnings ({count})",
        "dialog_ffmpeg_missing_title": "ffmpeg Not Found",
        "dialog_ffmpeg_missing_body": (
            "ffmpeg.exe was not found in the app folder, bin/ subfolder, or system PATH.\n\n"
            "Until a valid ffmpeg.exe is specified, the following features are disabled:\n"
            "  • Offline bgm.ogg mixing\n  • .wav → .ogg transcoding\n  • Non-.wav slice preview / L-R channel swap\n\n"
            "Click '⚙ 設定 / Config' at the top right to locate ffmpeg.exe, or place it beside bms2dtx.exe / in bin/."
        ),
        "dialog_ffmpeg_invalid_title": "Invalid ffmpeg",
        "dialog_ffmpeg_invalid_body": "Selected file is not a valid ffmpeg executable:\n{exc}",
        "dialog_file_not_found_title": "File Not Found",
        "dialog_file_not_found_body": "Could not find file:\n{path}",
        "dialog_drop_invalid_title": "Unsupported File",
        "dialog_drop_invalid_body": "Please drop a .bms / .bme / .bml / .pms chart file:\n{name}",
        "dialog_radix_title": "Radix Notice",
        "dialog_radix_body": (
            "Detected a Base-62 chart or total slices exceeding the usable DTX Base-36 limit (1293; ZY/ZZ reserved for Auto-LP).\n\n"
            "GUI uses Toggle = OFF: only slices mapped into the 15 Tabs are kept as individual files; "
            "remaining notes are offline-mixed into bgm.ogg."
        ),
        "dialog_clear_title": "Clear All",
        "dialog_clear_body": "Clear all Tab assignments?",
        "dialog_dirty_close_title": "Unsaved Mapping Changes",
        "dialog_dirty_close_body": "You have unexported mapping changes. Are you sure you want to exit?",
        "dialog_guess_none_body": "No unassigned slices could be guessed from filenames.",
        "dialog_guess_confirm_body": (
            "There are currently {assigned} manual assignment(s).\n\n"
            "Only {total} unassigned slice(s) will be filled by filename; existing assignments will not be overwritten. Continue?"
        ),
        "dialog_guess_done_title": "Auto-Guess Complete",
        "dialog_guess_done_summary": "Automatically assigned {total} slice(s) by filename and stereo panning rules.",
        "dialog_guess_done_warning": (
            "Note: Auto-assignment relies only on non-AI filename rules and basic stereo panning detection and may misclassify slices. "
            "Please manually review and verify each lane."
        ),
        "dialog_load_map_title": "Load Mapping",
        "dialog_load_map_first_body": "Please import a chart first.",
        "dialog_save_failed_title": "Save Failed",
        "dialog_load_failed_title": "Load Failed",
        "dialog_map_notes_title": "Mapping Load Notes",
        "dialog_invalid_out_title": "Invalid Output Directory",
        "dialog_select_out_title": "Output Directory",
        "dialog_select_out_body": "Please select an output directory.",
        "dialog_dlevel_err_title": "#DLEVEL",
        "dialog_dlevel_err_body": "#DLEVEL must be a non-negative integer (or empty).",
        "dialog_export_no_ffmpeg_body": (
            "ffmpeg not found: bgm.ogg cannot be rendered, and the exported .dtx will reference a non-existent bgm.ogg.\n\n"
            "Export .dtx and retained slices anyway? (Choose 'No' to configure ffmpeg.exe first)"
        ),
        "dialog_export_resize_no_ffmpeg_body": (
            "'Resize for Legacy DTX' image mode is selected, but ffmpeg.exe is not configured.\n\n"
            "Continue exporting (images will be copied as originals), or choose 'No' to configure ffmpeg.exe in Settings first?"
        ),
        "dialog_export_done_title": "Export Complete",
        "dialog_open_folder_err_title": "Failed to Open Folder",
        "no_warnings_text": "No warnings.",
        "err_out_same_as_src": "Output directory cannot be the BMS source folder itself. Please choose a separate folder. (Rule 1: original BMS folder is read-only)",
        "err_out_in_audio_subdir": "Output directory cannot be a BMS audio subfolder. (Rule 1: original BMS folder is read-only)",
        "result_dtx_line": "Output .dtx: {path}",
        "result_encoding_line": "DTX Encoding: {encoding}",
        "result_bgm_line": (
            "bgm.ogg: {path} (duration {duration:.2f}s, mixed notes {events}, "
            "peak {peak:.3f}, RMS {rms:.4f}, missing {missing})"
        ),
        "result_bgm_none": "bgm.ogg: Not generated",
        "result_drums_line": "drums.ogg: {path} (SE8/ch 68, Vol 10, duration {duration:.2f}s, events {events}, peak {peak:.3f})",
        "result_set_def_line": "4-difficulty set.def: {path} (__bsc / __adv / __extr / __mstr.dtx)",
        "result_preview_line": "#PREVIEW: {path}",
        "result_placed_notes": "Placed notes: {count}",
        "result_auto_lp_line": "Auto-LP inserted: {count} note(s)",
        "result_hihat_removed_line": "Hi-Hat conflict removals (HH Open priority): {count}",
        "result_tom_se7_line": "Tom overflow routed to SE7 (67): {count}",
        "result_merged_slices_line": "Deduplicated merged slices: {unique} composite ({total} hits)",
        "result_project_json_line": "Project mapping JSON: {path}",
        "result_log_line": "Conversion log: {path}",
        "result_copied_transcoded": "Copied slices: {copied}   Transcoded .ogg: {transcoded}",
        "result_lane_binds": "#DTXC_LANEBINDEDCHIP lines: {count}",
        "result_toggle_off_forced": "Note: Toggle = OFF was forced",
        "result_warnings_header": "Export Warnings ({count}):",
        "result_import_warnings_note": "({count} source import warning(s) omitted here; see top 'Warnings' button & __bms2dtx_log.txt)",
        "preview_no_winsound": "Audio preview not supported on this OS (winsound unavailable)",
        "preview_file_missing": "File not found, cannot preview: {name}",
        "preview_no_ffmpeg": "ffmpeg not found: cannot preview non-.wav slices or swap L/R (please locate ffmpeg.exe)",
        "preview_decoding": "Decoding: {name} ...",
        "preview_stopped": "Stopped",
        "preview_decode_failed": "Decode failed: {exc}",
        "preview_playing": "Playing: {name}",
        "preview_play_failed": "Cannot play {name}: {exc}",
        "map_note_unknown_tab": "Ignored unknown Tab: {tab}",
        "map_note_undeclared_slice": "Ignored slice not declared in current chart: {wav_id}",
        "map_note_duplicate_slice": "Ignored duplicate slice assignment: {wav_id}",
        "map_note_unknown_bind_tab": "Ignored default bind for unknown Tab: {tab}",
        "map_note_invalid_bind": "Ignored invalid default bind: {tab} -> {wav_id}",
        "map_note_toggle_on_ignored": "Mapping file has toggle_on=true; v1 GUI supports Toggle = OFF only (ignored).",
    },
    "ja": {
        "open_bms_btn": ".bms / .bme / .bml / .pms を選択",
        "no_chart_selected": "クリックで選択、または .bms / .bme / .bml / .pms をドロップ",
        "config_btn": "⚙ 設定 / Config",
        "ffmpeg_warn_badge": "⚠ ffmpeg 未検出",
        "ffmpeg_ok": "内蔵オーディオエンジン準備完了（WASAPI Shared）；FFmpeg（任意）: {path}",
        "ffmpeg_missing_short": "内蔵オーディオエンジン準備完了（WASAPI Shared）、FFmpeg は特殊形式と旧版画像リサイズ用（任意・未設定）",
        "pick_ffmpeg_btn": "ffmpeg.exe を指定...",
        "lang_label": "🌐 Language:",
        "filetypes_bms": "BMS / BME / BML / PMS 譜面",
        "filetypes_all": "すべてのファイル",
        "health_frame_title": "譜面チェック",
        "health_placeholder": "譜面を読み込むと表示されます：タイトル / アーティスト / BPM / 文字コード / 基数 / WAV統計 / BGM列 / 警告",
        "view_warnings_btn": "警告を表示 ({count})",
        "health_title": "タイトル",
        "health_artist": "アーティスト",
        "health_bpm": "BPM",
        "health_encoding": "文字コード",
        "health_radix": "基数",
        "radix_base36_ok": "Base-36 互換",
        "radix_base62_ext": "Base-62 (拡張)",
        "health_wav_count": "WAV 数",
        "health_wav_value": "計 {total} / 拡張子メモリ補正 {corrected} / 欠損 {missing}",
        "health_bgm_cols": "BGM 列",
        "health_bgm_value": "非空 B1..Bn 計 {count} 列 / 空トラック除外 {pruned}",
        "health_warnings": "警告",
        "health_warnings_value": "{count} 件",
        "warn_tag_prefix": "読込警告：",
        "warn_tag_undef_wav": "未定義#WAV",
        "warn_tag_missing_audio": "音声欠損",
        "warn_tag_stop": "#STOPあり",
        "warn_tag_random": "未展開#RANDOM",
        "warn_tag_unclosed_ln": "未終了LN",
        "warn_tag_base62": "62進数",
        "warn_tag_wav_limit": "スライス上限超過",
        "warn_tag_missing_preview": "#PREVIEW欠損",
        "warn_tag_other": "他{count}件",
        "radix_warn_base62": "  ⚠ 62進数を検出（DTX標準の36進数上限を超過）",
        "radix_warn_limit": "  ⚠ スライス総数 > 1293（Toggle = OFF で bgm.ogg にオフラインミックス）",
        "left_frame_title": "元 #WAV リスト（Shift/Ctrl 複数選択；クリック / 矢印 / Space で試聴）",
        "col_id": "ID",
        "col_filename": "ファイル名",
        "col_count": "回数",
        "col_target": "割当先",
        "col_lr": "L/R",
        "search_label": "検索:",
        "clear_search_btn": "クリア",
        "only_used_chk": "譜面で使用されているスライスのみ表示",
        "play_btn": "▶ 試聴",
        "stop_btn": "■ 停止",
        "auto_preview_chk": "自動試聴",
        "auto_preview_tooltip": "有効にすると、左右リストのクリックまたは上下キー移動時に単一スライスを自動試聴します",
        "swap_lr_chk": "全スライスのL/Rを反転",
        "left_count_summary": "表示 {shown} / {total} · 割当済 {assigned}",
        "tag_missing": "[欠損]",
        "tag_undeclared": "(未定義)",
        "add_btn": "➜ 追加",
        "remove_btn": "✖ 削除",
        "swap_slice_lr_btn": "⇄ L/R",
        "swap_slice_lr_tooltip": "選択したスライスの個別左右チャンネル入替を切替（全体の左右チャンネル入替と XOR 演算）",
        "guess_btn": "ファイル名で自動割当",
        "clear_all_btn": "すべてクリア",
        "right_frame_title": "15 Tab マッピング（Tab 1–12 ドラム / 13 Other / 14 Guitar / 15 Bass）",
        "separate_lbd_chk": "LBD を個別定義",
        "default_bind_label": "デフォルトバインド WAV (#DTXC_LANEBINDEDCHIP):",
        "lp_default_bind_short": "既定:",
        "auto_lp_label": "LP 補足:",
        "auto_lp_chip_none": "なし",
        "auto_lp_chip_auto": "自動",
        "auto_lp_chip_auto_prefix": "[自動] ",
        "auto_lp_chip_empty_zz": "ZZ: 無音",
        "auto_lp_vol_label": "音量:",
        "auto_lp_use_zy_chk": "ZY を使用",
        "auto_lp_use_zy_tooltip": "有効にすると予約スロット #WAVZY + #VOLUMEZY で音量調整します。無効時は元の LP スライスを直接再利用します",
        "auto_lp_play_tooltip": "現在の音量と有効な L/R 反転設定で Auto-LP 対象スライスを試聴します",
        "auto_lp_target_none": "(Auto-LP 無効)",
        "auto_lp_target_zz": "ZZ _silent_zz.wav (無音)",
        "auto_lp_target_no_slice": "(LP / HH Close トラックにスライスなし)",
        "auto_lp_mode_note": "音符長",
        "auto_lp_mode_ms": "ms",
        "auto_lp_tooltip": (
            "オープン (OHH, 18) から設定しきい値以内で最初のクローズ (CHH, 11) に移行し、"
            "その間および同時拍に元譜の LP がない場合、閉じた位置へ自動で LP (1B) ノーツを補完します"
            "（予約スロット ZY または無音 ZZ を使用）。"
        ),
        "no_bind_label": "(未指定 / 00)",
        "auto_bind_prefix": "[自動最多頻度] ",
        "status_bind_auto_label": "自動最多頻度",
        "tab_note_other": "SE6 (チャンネル 66) に固定割当",
        "tab_note_guitar": "任意：空欄で無効化（SE11–SE12 を自動解放）",
        "tab_note_bass": "任意：空欄で無効化（SE13–SE14 を自動解放）",
        "save_map_btn": "JSONに別名保存...",
        "load_map_btn": "マッピング JSON を読込...",
        "export_frame_title": "エクスポート",
        "output_dir_label": "出力先フォルダ:",
        "browse_btn": "参照...",
        "start_export_btn": "エクスポート開始",
        "open_output_btn": "出力フォルダを開く",
        "utf8_chk": "UTF-8 で出力 (--utf8)",
        "transcode_ogg_chk": "保持した .wav スライスを .ogg に自動変換",
        "four_diffs_chk": "4難易度ベース譜面と set.def を生成",
        "dlevel_label": "ドラム難易度 (#DLEVEL):",
        "image_mode_label": "画像処理:",
        "image_mode_keep": "元画像をそのままコピー",
        "image_mode_resize": "旧DTXサイズ変換（比率維持＋黒帯）",
        "image_mode_none": "画像をコピーしない",
        "pad_square_preimage_chk": "非正方形ジャケットに自動黒帯追加 (1:1)",
        "pad_square_preimage_tooltip": "非正方形の #STAGEFILE を元解像度のまま中央配置＋純黒余白で 1:1 の #PREIMAGE PNG に出力（正方形は再エンコードせずコピー）",
        "export_bga_chk": "実験的 BGA パススルー",
        "settings_title": "設定 / Settings",
        "settings_general_section": "表示言語",
        "settings_ffmpeg_section": "オーディオエンジンと FFmpeg（任意）",
        "settings_ffmpeg_redetect_btn": "自動再検出",
        "settings_ffmpeg_hint": "任意：.m4a/.wma 等の特殊形式や旧版画像リサイズを使う場合は ffmpeg.exe を本体と同階層または bin\\ に配置、または実行：",
        "settings_winget_cmd": "winget install Gyan.FFmpeg.Essentials",
        "settings_copy_cmd_btn": "コピー",
        "settings_copied_cmd": "winget コマンドをコピーしました",
        "settings_bind_section": "デフォルトチップ割当方針 (#DTXC_LANEBINDEDCHIP)",
        "settings_bind_auto": "最多頻度のスライスを自動選択",
        "settings_bind_manual": "自動バインドしない（手動指定）",
        "settings_labels_section": "#WAVxx エクスポート行末 ;LABEL コメント",
        "settings_reset_labels_btn": "初期値に戻す",
        "settings_allow_left_remove_chk": "左側 WAV リストの選択から「削除」で割当解除を許可する",
        "settings_export_drums_ogg_chk": "drums.ogg ドラムステムを出力 (音量 10)",
        "settings_open_logs_btn": "ログフォルダを開く",
        "settings_save_btn": "保存して閉じる",
        "settings_cancel_btn": "キャンセル",
        "status_ready": "準備完了",
        "status_undone": "直前のマッピング操作を元に戻しました",
        "status_redone": "マッピング操作をやり直しました",
        "status_slice_lr_toggled": "{count} 件のスライスの左右チャンネル入替を切り替えました",
        "status_logs_opened": "ログフォルダを開きました: {path}",
        "dialog_crash_logged_title": "エラーログ記録",
        "dialog_crash_logged_body": "予期しないエラーが発生しました:\n{exc}\n\n詳細ログを保存しました:\n{path}",
        "status_bind_updated": "{tab} デフォルトバインド: {wav}",
        "status_select_left_first": "左リストから追加するスライスを選択してください",
        "status_added_to_tab": "{tab} に {added} 個のスライスを追加しました",
        "status_added_skipped_suffix": "（他 Tab に割当済の {skipped} 個をスキップ）",
        "status_select_right_first": "現在の Tab から削除するスライスを選択してください",
        "status_removed_from_tab": "{tab} から {removed} 個のスライスを削除しました",
        "status_unassigned_from_left": "{removed} 件のスライスの割当を解除しました",
        "status_cleared_all": "すべての割当をクリアしました",
        "status_guessed": "ファイル名から {total} 個の未割当スライスを自動割当しました",
        "status_select_slice_first": "スライスを選択してください",
        "status_file_missing_no_preview": "[{wav_id}] ファイル欠損のため試聴できません",
        "status_parsing": "解析中: {name} ...",
        "status_parse_failed": "解析失敗",
        "status_loaded": "{name} を読込完了：{wavs} 個の #WAV、{events} ノーツ",
        "status_exporting": "エクスポート中（オフラインミックスには数十秒かかる場合があります）...",
        "status_export_done": "エクスポート完了: {path}",
        "status_export_failed": "エクスポート失敗",
        "status_ffmpeg_enabled": "ffmpeg を有効化: {path}",
        "status_map_saved": "マッピングを保存しました: {path}",
        "status_map_loaded": "マッピングを読込完了（{count} スライス）",
        "dialog_close_btn": "閉じる",
        "dialog_warnings_title": "譜面警告 ({count})",
        "dialog_ffmpeg_missing_title": "ffmpeg が見つかりません",
        "dialog_ffmpeg_missing_body": (
            "アプリフォルダ / bin サブフォルダ / システム PATH に ffmpeg.exe が見つかりません。\n\n"
            "有効な ffmpeg.exe を指定するまで、以下の機能は無効になります：\n"
            "  • bgm.ogg のオフラインミックス出力\n  • .wav → .ogg 自動変換\n  • 非 .wav スライスの試聴 / 左右チャンネル入替\n\n"
            "右上の「⚙ 設定 / Config」から ffmpeg.exe を指定するか、bms2dtx.exe の隣または bin フォルダに配置してください。"
        ),
        "dialog_ffmpeg_invalid_title": "無効な ffmpeg",
        "dialog_ffmpeg_invalid_body": "選択されたファイルは有効な ffmpeg ではありません:\n{exc}",
        "dialog_file_not_found_title": "ファイルが見つかりません",
        "dialog_file_not_found_body": "ファイルが見つかりません:\n{path}",
        "dialog_drop_invalid_title": "未対応のファイル",
        "dialog_drop_invalid_body": ".bms / .bme / .bml / .pms 譜面ファイルをドロップしてください:\n{name}",
        "dialog_radix_title": "基数に関する通知",
        "dialog_radix_body": (
            "Base-62 譜面、またはスライス総数が利用可能な DTX 36進数上限 (1293; ZY/ZZ は Auto-LP 用に予約) を超えています。\n\n"
            "GUI は Toggle = OFF 固定です：15 個の Tab に割り当てたスライスのみ個別ファイルとして保持され、"
            "残りのノーツは bgm.ogg にオフラインミックスされます。"
        ),
        "dialog_clear_title": "すべてクリア",
        "dialog_clear_body": "すべての Tab の割当をクリアしますか？",
        "dialog_dirty_close_title": "未エクスポートの変更",
        "dialog_dirty_close_body": "現在のマッピング変更はまだエクスポートされていません。終了しますか？",
        "dialog_guess_none_body": "ファイル名から判別できる未割当スライスはありませんでした。",
        "dialog_guess_confirm_body": (
            "現在 {assigned} 個の手動割当があります。\n\n"
            "未割当の {total} 個のみファイル名で自動割当します（既存の割当は上書きされません）。続行しますか？"
        ),
        "dialog_guess_done_title": "自動割当完了",
        "dialog_guess_done_summary": "ファイル名とパンニング規則により {total} 件のスライスを自動割当しました。",
        "dialog_guess_done_warning": (
            "注意：自動割当はファイル名規則と簡易パンニング判定のみに基づく非AI検出のため、誤判定の可能性があります。必ず各トラックを手動で確認・見直してください。"
        ),
        "dialog_load_map_title": "マッピング読込",
        "dialog_load_map_first_body": "先に譜面を読み込んでください。",
        "dialog_save_failed_title": "保存失敗",
        "dialog_load_failed_title": "読込失敗",
        "dialog_map_notes_title": "マッピング読込通知",
        "dialog_invalid_out_title": "無効な出力先フォルダ",
        "dialog_select_out_title": "出力先フォルダ",
        "dialog_select_out_body": "出力先フォルダを選択してください。",
        "dialog_dlevel_err_title": "#DLEVEL",
        "dialog_dlevel_err_body": "#DLEVEL には 0 以上の整数を入力してください（空欄可）。",
        "dialog_export_no_ffmpeg_body": (
            "ffmpeg が見つからないため bgm.ogg を生成できません（出力される .dtx は存在しない bgm.ogg を参照します）。\n\n"
            ".dtx と保持スライスのみエクスポートしますか？（「いいえ」で先に ffmpeg.exe を指定できます）"
        ),
        "dialog_export_resize_no_ffmpeg_body": (
            "「旧版 DTX サイズに合わせる（リサイズ + 黒帯）」が選択されていますが、ffmpeg.exe が設定されていません。\n\n"
            "このまま元画像をコピーして続行しますか？（「いいえ」で設定画面から ffmpeg.exe を指定できます）"
        ),
        "dialog_export_done_title": "エクスポート完了",
        "dialog_open_folder_err_title": "フォルダを開けませんでした",
        "no_warnings_text": "警告はありません。",
        "err_out_same_as_src": "出力先に BMS 元フォルダ自体は指定できません。別のフォルダを選択してください。（鉄則 1：元 BMS フォルダは読み取り専用）",
        "err_out_in_audio_subdir": "出力先に BMS 音声サブフォルダは指定できません。（鉄則 1：元 BMS フォルダは読み取り専用）",
        "result_dtx_line": "出力 .dtx: {path}",
        "result_encoding_line": "DTX 文字コード: {encoding}",
        "result_bgm_line": (
            "bgm.ogg: {path} (長さ {duration:.2f}s, ミックスノーツ {events}, "
            "ピーク {peak:.3f}, RMS {rms:.4f}, 欠損 {missing})"
        ),
        "result_bgm_none": "bgm.ogg: 未生成",
        "result_drums_line": "drums.ogg: {path} (SE8/ch 68, 音量 10, 長さ {duration:.2f}s, ミックスノーツ {events}, ピーク {peak:.3f})",
        "result_set_def_line": "4難易度 set.def: {path} (__bsc / __adv / __extr / __mstr.dtx)",
        "result_preview_line": "#PREVIEW: {path}",
        "result_placed_notes": "配置ノーツ数: {count}",
        "result_auto_lp_line": "Auto-LP 自動補足: {count} ノーツ",
        "result_hihat_removed_line": "Hi-Hat 競合削除 (HH Open 優先): {count} 件",
        "result_tom_se7_line": "Tom 溢れ SE7 (67) 送出: {count} 件",
        "result_merged_slices_line": "同トラック重複なし統合ミックス: {unique} 種生成（全曲 {total} 回再利用）",
        "result_project_json_line": "自動保存マッピング JSON: {path}",
        "result_log_line": "変換ログ: {path}",
        "result_copied_transcoded": "スライスコピー: {copied}   .ogg 変換: {transcoded}",
        "result_lane_binds": "#DTXC_LANEBINDEDCHIP 行数: {count}",
        "result_toggle_off_forced": "注意: Toggle = OFF が強制適用されました",
        "result_warnings_header": "エクスポート警告 ({count}):",
        "result_import_warnings_note": "（元の譜面の読込警告 {count} 件は上部の「警告を表示」と __bms2dtx_log.txt をご参照ください）",
        "preview_no_winsound": "現在の環境では試聴がサポートされていません (winsound 利用不可)",
        "preview_file_missing": "ファイルが存在しないため試聴できません: {name}",
        "preview_no_ffmpeg": "ffmpeg 未検出: 非 .wav スライスやチャンネル入替を試聴できません (ffmpeg.exe を指定してください)",
        "preview_decoding": "デコード中: {name} ...",
        "preview_stopped": "停止しました",
        "preview_decode_failed": "デコード失敗: {exc}",
        "preview_playing": "再生中: {name}",
        "preview_play_failed": "{name} を再生できません: {exc}",
        "map_note_unknown_tab": "不明な Tab を無視しました: {tab}",
        "map_note_undeclared_slice": "現在の譜面で未定義のスライスを無視しました: {wav_id}",
        "map_note_duplicate_slice": "重複割当されたスライスを無視しました: {wav_id}",
        "map_note_unknown_bind_tab": "不明な Tab のデフォルトバインドを無視しました: {tab}",
        "map_note_invalid_bind": "無効なデフォルトバインドを無視しました: {tab} -> {wav_id}",
        "map_note_toggle_on_ignored": "マッピングファイルに toggle_on=true が含まれていますが、v1 GUI は Toggle = OFF 専用のため無視しました。",
    },
}


def get_language() -> str:
    """Return the active language code ('zh_CN', 'en', or 'ja')."""
    return _current_language


def set_language(lang: str) -> str:
    """Set active language code (accepts code or display label). Falls back to zh_CN if unknown."""
    global _current_language
    cleaned = lang.strip()
    if cleaned in LABEL_TO_LANGUAGE:
        cleaned = LABEL_TO_LANGUAGE[cleaned]
    if cleaned not in TRANSLATIONS:
        cleaned = DEFAULT_LANGUAGE
    _current_language = cleaned
    return _current_language


def tr(key: str, *, lang: Optional[str] = None, **kwargs: Any) -> str:
    """Translate ``key`` into ``lang`` (or current active language) and format optional kwargs."""
    active = lang if (lang and lang in TRANSLATIONS) else _current_language
    table = TRANSLATIONS.get(active, TRANSLATIONS[DEFAULT_LANGUAGE])
    template = table.get(key, TRANSLATIONS[DEFAULT_LANGUAGE].get(key, key))
    if kwargs:
        return template.format(**kwargs)
    return template


def get_default_config_path() -> Path:
    """Return user-local settings JSON path (never inside any BMS folder)."""
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "bms2dtx" / "settings.json"
    return Path.home() / ".bms2dtx" / "settings.json"


def load_app_settings(config_path: Optional[Path | str] = None) -> Dict[str, Any]:
    """Load persisted settings JSON, filling missing fields with safe defaults."""
    settings: Dict[str, Any] = {
        "language": DEFAULT_LANGUAGE,
        "ffmpeg_path": None,
        "default_bind_mode": "auto",
        "swap_lr": False,
        "allow_left_remove": False,
        "export_drums_ogg": True,
        "export_four_diffs": True,
        "pad_square_preimage": True,
        "wav_labels": dict(DEFAULT_WAV_LABELS),
    }
    target = Path(config_path) if config_path is not None else get_default_config_path()
    try:
        if target.is_file():
            data = json.loads(target.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                lang = str(data.get("language", DEFAULT_LANGUAGE)).strip()
                if lang in TRANSLATIONS:
                    settings["language"] = lang
                raw_ff = data.get("ffmpeg_path")
                if isinstance(raw_ff, str) and raw_ff.strip():
                    settings["ffmpeg_path"] = raw_ff.strip()
                bind_mode = str(data.get("default_bind_mode", "auto")).strip().lower()
                if bind_mode in ("auto", "manual"):
                    settings["default_bind_mode"] = bind_mode
                if "allow_left_remove" in data:
                    settings["allow_left_remove"] = bool(data["allow_left_remove"])
                if "export_drums_ogg" in data:
                    settings["export_drums_ogg"] = bool(data["export_drums_ogg"])
                if "export_four_diffs" in data:
                    settings["export_four_diffs"] = bool(data["export_four_diffs"])
                if "pad_square_preimage" in data:
                    settings["pad_square_preimage"] = bool(data["pad_square_preimage"])
                raw_labels = data.get("wav_labels")
                if isinstance(raw_labels, dict):
                    merged = dict(DEFAULT_WAV_LABELS)
                    for key in WAV_LABEL_KEYS:
                        if key in raw_labels and isinstance(raw_labels[key], str):
                            merged[key] = sanitize_wav_label(raw_labels[key])
                    settings["wav_labels"] = merged
    except Exception:
        pass
    return settings


def save_app_settings(
    updates: Dict[str, Any], config_path: Optional[Path | str] = None
) -> bool:
    """Merge and persist settings into local settings.json."""
    target = Path(config_path) if config_path is not None else get_default_config_path()
    try:
        existing: Dict[str, Any] = {}
        if target.is_file():
            try:
                loaded = json.loads(target.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    existing = loaded
            except Exception:
                existing = {}
        for k, v in updates.items():
            if k == "language":
                norm = str(v).strip()
                existing["language"] = norm if norm in TRANSLATIONS else DEFAULT_LANGUAGE
            elif k == "default_bind_mode":
                bm = str(v).strip().lower()
                existing["default_bind_mode"] = bm if bm in ("auto", "manual") else "auto"
            elif k == "wav_labels" and isinstance(v, dict):
                cleaned_labels: Dict[str, str] = {}
                for lk in WAV_LABEL_KEYS:
                    raw_val = v.get(lk, DEFAULT_WAV_LABELS[lk])
                    cleaned_labels[lk] = sanitize_wav_label(str(raw_val))
                existing["wav_labels"] = cleaned_labels
            elif k == "ffmpeg_path":
                existing["ffmpeg_path"] = str(v).strip() if v else None
            elif k == "allow_left_remove":
                existing["allow_left_remove"] = bool(v)
            elif k == "export_drums_ogg":
                existing["export_drums_ogg"] = bool(v)
            elif k == "export_four_diffs":
                existing["export_four_diffs"] = bool(v)
            elif k == "pad_square_preimage":
                existing["pad_square_preimage"] = bool(v)
            elif k == "swap_lr":
                continue
            else:
                existing[k] = v
        existing.pop("swap_lr", None)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


def load_language_preference(config_path: Optional[Path | str] = None) -> str:
    """Load saved language code from disk; returns DEFAULT_LANGUAGE if not found or invalid."""
    return str(load_app_settings(config_path).get("language", DEFAULT_LANGUAGE))


def save_language_preference(lang: str, config_path: Optional[Path | str] = None) -> bool:
    """Persist language preference to local settings JSON."""
    return save_app_settings({"language": lang}, config_path)
