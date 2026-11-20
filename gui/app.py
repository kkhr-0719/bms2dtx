"""bms2dtx desktop GUI (tkinter / ttk). v1.1 scope: Toggle = OFF only."""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import tkinter.font as tkfont

import core
from core import (
    TAB_SPECS,
    BmsChart,
    ConversionOptions,
    FfmpegNotFoundError,
    TabId,
    convert_bms,
    find_ffmpeg,
    inspect_bms,
)

from . import APP_NAME, APP_VERSION
from .auto_guess import guess_assignments
from .helpers import (
    GRAY_FOREGROUND,
    AssignmentModel,
    bind_combobox_values,
    build_health_summary,
    build_result_summary,
    check_output_dir,
    default_dlevel_text,
    filter_wav_ids,
    format_tab_row,
    format_warnings,
    format_wav_row,
    get_no_bind_label,
    get_tab_note,
    load_mapping_json,
    parse_dlevel,
    radix_needs_notice,
    save_mapping_json,
    suggest_output_dir,
    tab_has_bind_combobox,
    tab_label,
    tab_title,
    visible_tab_specs,
    wav_group_for,
    wav_physical_groups,
)
from .i18n import (
    LABEL_TO_LANGUAGE,
    LANGUAGE_LABELS,
    SUPPORTED_LANGUAGES,
    get_language,
    load_language_preference,
    save_language_preference,
    set_language,
    tr,
)
from .preview_player import PreviewPlayer

ERROR_RED = "#c62828"
OK_GREEN = "#2e7d32"
ACTIVE_TAB_BG = "#1565c0"
ACTIVE_TAB_FG = "#ffffff"
INACTIVE_TAB_BG = "#f0f0f0"
INACTIVE_TAB_FG = "#111111"
ASSIGNED_TAB_FG = "#0d47a1"
TAB_GRID_COLUMNS = 5


def enable_hidpi() -> None:
    """Enable Windows DPI awareness; must be called BEFORE ``tk.Tk()``."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


class TabPanel:
    """One right-hand Tab page: note, optional default-bind Combobox, Listbox."""

    def __init__(self, app: "App", parent: ttk.Frame, tab: TabId) -> None:
        self.app = app
        self.tab = tab
        self.frame = ttk.Frame(parent, padding=6)
        self.ids: List[str] = []
        self.bind_ids: List[Optional[str]] = [None]
        self.note_label: Optional[ttk.Label] = None
        self.bind_label: Optional[ttk.Label] = None
        self.combo: Optional[ttk.Combobox] = None
        self.combo_var = tk.StringVar(value=get_no_bind_label())

        row = 0
        note = get_tab_note(tab)
        if note is not None:
            self.note_label = ttk.Label(
                self.frame, text=note, foreground="#1565c0", wraplength=380, justify="left"
            )
            self.note_label.grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 4))
            row += 1
        if tab_has_bind_combobox(tab):
            self.bind_label = ttk.Label(self.frame, text=tr("default_bind_label"))
            self.bind_label.grid(row=row, column=0, columnspan=2, sticky="w")
            row += 1
            self.combo = ttk.Combobox(
                self.frame,
                textvariable=self.combo_var,
                state="readonly",
                values=[get_no_bind_label()],
            )
            self.combo.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(0, 6))
            self.combo.bind("<<ComboboxSelected>>", self._on_bind_selected)
            row += 1

        self.listbox = tk.Listbox(
            self.frame, selectmode=tk.EXTENDED, exportselection=False, activestyle="dotbox"
        )
        scroll = ttk.Scrollbar(self.frame, orient="vertical", command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=scroll.set)
        self.listbox.grid(row=row, column=0, sticky="nsew")
        scroll.grid(row=row, column=1, sticky="ns")
        self.frame.rowconfigure(row, weight=1)
        self.frame.columnconfigure(0, weight=1)

        self.listbox.bind(
            "<Double-Button-1>", lambda _e: (self.app.preview_from_tab(self.tab), "break")[1]
        )
        self.listbox.bind("<space>", lambda _e: (self.app.preview_from_tab(self.tab), "break")[1])

    def _on_bind_selected(self, _event: Any = None) -> None:
        idx = self.combo.current() if self.combo is not None else 0
        wav_id = self.bind_ids[idx] if 0 <= idx < len(self.bind_ids) else None
        try:
            self.app.model.set_bind(self.tab, wav_id)
        except ValueError:
            self.app.model.default_binds.pop(self.tab, None)
        self.app.set_status(
            tr(
                "status_bind_updated",
                tab=tab_label(self.tab),
                wav=wav_id or get_no_bind_label(),
            )
        )

    def update_language_texts(self) -> None:
        if self.note_label is not None:
            note = get_tab_note(self.tab)
            if note is not None:
                self.note_label.configure(text=note)
        if self.bind_label is not None:
            self.bind_label.configure(text=tr("default_bind_label"))

    def refresh(self) -> None:
        model = self.app.model
        chart = self.app.chart
        self.ids = model.display_ids_for_tab(self.tab, chart)
        self.listbox.delete(0, tk.END)
        for wid in self.ids:
            entry = chart.wavs.get(wid) if chart else None
            self.listbox.insert(tk.END, format_tab_row(entry, wid, chart=chart))
        if self.combo is not None:
            values = bind_combobox_values(model, chart, self.tab)
            self.bind_ids = [wid for _text, wid in values]
            self.combo.configure(values=[text for text, _wid in values])
            bound = model.get_bind(self.tab)
            # Match either direct bound ID or its physical-file group representative
            bound_idx = 0
            if bound is not None:
                if bound in self.bind_ids:
                    bound_idx = self.bind_ids.index(bound)
                elif chart is not None:
                    group = set(wav_group_for(chart, bound))
                    for candidate_idx, cid in enumerate(self.bind_ids):
                        if cid is not None and cid in group:
                            bound_idx = candidate_idx
                            break
                    else:
                        model.default_binds.pop(self.tab, None)
                else:
                    model.default_binds.pop(self.tab, None)
            self.combo.current(bound_idx)

    def selected_ids(self) -> List[str]:
        return [self.ids[i] for i in self.listbox.curselection() if i < len(self.ids)]


class App:
    def __init__(
        self,
        root: tk.Tk,
        initial_path: Optional[str] = None,
        *,
        startup_dialogs: bool = True,
        config_path: Optional[Path | str] = None,
    ) -> None:
        self.root = root
        self.startup_dialogs = startup_dialogs
        self.config_path: Optional[Path] = Path(config_path) if config_path is not None else None
        self._persist_lang = (self.config_path is not None) or self.startup_dialogs
        if self._persist_lang:
            set_language(load_language_preference(self.config_path))

        self.chart: Optional[BmsChart] = None
        self.model = AssignmentModel()
        self.ffmpeg_explicit: Optional[str] = None
        self.ffmpeg_path: Optional[str] = None
        self.last_output_dir: Optional[Path] = None
        self._busy = False
        self._left_ids: List[str] = []
        self._active_tab: TabId = TabId.LC
        self._arrow_previewed_idx: Optional[int] = None
        self._queue: "queue.Queue[tuple]" = queue.Queue()
        self._ffmpeg_warned = False

        root.title(f"{APP_NAME} {APP_VERSION} — BMS/BME → DTX")
        self._setup_fonts_and_geometry()
        self.player = PreviewPlayer(
            lambda: self.ffmpeg_path,
            on_status=lambda msg: self._queue.put(("preview_status", msg)),
        )

        self._build_ui()
        self.detect_ffmpeg()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._poll_id: Optional[str] = root.after(80, self._poll_queue)

        if self.startup_dialogs and self.ffmpeg_path is None:
            root.after(300, self._warn_ffmpeg_missing)
        if initial_path:
            root.after(150, lambda: self.load_bms(initial_path))

    # ------------------------------------------------------------------ setup
    def _setup_fonts_and_geometry(self) -> None:
        try:
            families = set(tkfont.families(self.root))
            for fam in ("Microsoft YaHei UI", "Yu Gothic UI", "Segoe UI"):
                if fam in families:
                    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
                        tkfont.nametofont(name).configure(family=fam)
                    break
        except Exception:
            pass
        self.normal_font = tkfont.nametofont("TkDefaultFont").copy()
        self.bold_font = tkfont.nametofont("TkDefaultFont").copy()
        self.bold_font.configure(weight="bold")
        try:
            scale = max(1.0, float(self.root.winfo_fpixels("1i")) / 96.0)
        except Exception:
            scale = 1.0
        width = min(int(1180 * scale), self.root.winfo_screenwidth() - 40)
        height = min(int(780 * scale), self.root.winfo_screenheight() - 80)
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(min(int(960 * scale), width), min(int(640 * scale), height))

    def _build_ui(self) -> None:
        root = self.root
        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)

        # --- top bar: file picker + ffmpeg status + language selector ---
        top = ttk.Frame(root, padding=(8, 8, 8, 2))
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(1, weight=1)
        self.open_btn = ttk.Button(top, text=tr("open_bms_btn"), command=self.on_choose_file)
        self.open_btn.grid(row=0, column=0, sticky="w", ipadx=12, ipady=4)
        self.path_var = tk.StringVar(value=tr("no_chart_selected"))
        ttk.Label(top, textvariable=self.path_var).grid(row=0, column=1, sticky="w", padx=10)
        self.ffmpeg_var = tk.StringVar()
        self.ffmpeg_label = ttk.Label(top, textvariable=self.ffmpeg_var)
        self.ffmpeg_label.grid(row=0, column=2, sticky="e", padx=6)
        self.pick_ffmpeg_btn = ttk.Button(
            top, text=tr("pick_ffmpeg_btn"), command=self.on_pick_ffmpeg
        )
        self.pick_ffmpeg_btn.grid(row=0, column=3, sticky="e")

        self.lang_label = ttk.Label(top, text=tr("lang_label"))
        self.lang_label.grid(row=0, column=4, sticky="e", padx=(12, 4))
        self.lang_var = tk.StringVar(value=LANGUAGE_LABELS.get(get_language(), "简体中文"))
        self.lang_combo = ttk.Combobox(
            top,
            textvariable=self.lang_var,
            state="readonly",
            width=10,
            values=[label for _code, label in SUPPORTED_LANGUAGES],
        )
        self.lang_combo.grid(row=0, column=5, sticky="e")
        self.lang_combo.bind("<<ComboboxSelected>>", self._on_language_selected)

        # --- health check summary ---
        self.health = ttk.LabelFrame(root, text=tr("health_frame_title"), padding=6)
        self.health.grid(row=1, column=0, sticky="ew", padx=8, pady=4)
        self.health.columnconfigure(1, weight=1)
        self._health_widgets: List[tk.Widget] = []
        self.warn_btn = ttk.Button(
            self.health,
            text=tr("view_warnings_btn", count=0),
            command=self.show_chart_warnings,
            state="disabled",
        )
        self._render_health(None)

        # --- main workspace ---
        main = ttk.Frame(root, padding=(8, 0))
        main.grid(row=2, column=0, sticky="nsew")
        main.columnconfigure(0, weight=5, uniform="cols")
        main.columnconfigure(2, weight=5, uniform="cols")
        main.rowconfigure(0, weight=1)
        self._build_left(main)
        self._build_center(main)
        self._build_right(main)

        # --- export bar ---
        self._build_export(root)

        self.status_var = tk.StringVar(value=tr("status_ready"))
        ttk.Label(
            root, textvariable=self.status_var, relief="sunken", anchor="w", padding=(6, 2)
        ).grid(row=4, column=0, sticky="ew")

    def _build_left(self, parent: ttk.Frame) -> None:
        self.left_frame = ttk.LabelFrame(parent, text=tr("left_frame_title"), padding=6)
        self.left_frame.grid(row=0, column=0, sticky="nsew")
        self.left_frame.columnconfigure(0, weight=1)
        self.left_frame.rowconfigure(1, weight=1)

        bar = ttk.Frame(self.left_frame)
        bar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 4))
        bar.columnconfigure(1, weight=1)
        self.search_label = ttk.Label(bar, text=tr("search_label"))
        self.search_label.grid(row=0, column=0)
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_a: self.refresh_left())
        ttk.Entry(bar, textvariable=self.search_var).grid(row=0, column=1, sticky="ew", padx=4)
        self.clear_search_btn = ttk.Button(
            bar, text=tr("clear_search_btn"), width=6, command=lambda: self.search_var.set("")
        )
        self.clear_search_btn.grid(row=0, column=2)
        self.only_used_var = tk.BooleanVar(value=False)
        self.only_used_chk = ttk.Checkbutton(
            bar,
            text=tr("only_used_chk"),
            variable=self.only_used_var,
            command=self.refresh_left,
        )
        self.only_used_chk.grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 0))

        self.left_list = tk.Listbox(
            self.left_frame, selectmode=tk.EXTENDED, exportselection=False, activestyle="dotbox"
        )
        scroll = ttk.Scrollbar(self.left_frame, orient="vertical", command=self.left_list.yview)
        xscroll = ttk.Scrollbar(self.left_frame, orient="horizontal", command=self.left_list.xview)
        self.left_list.configure(yscrollcommand=scroll.set, xscrollcommand=xscroll.set)
        self.left_list.grid(row=1, column=0, sticky="nsew")
        scroll.grid(row=1, column=1, sticky="ns")
        xscroll.grid(row=2, column=0, sticky="ew")
        self.left_list.bind("<Button-1>", self._on_left_click)
        self.left_list.bind("<KeyPress-Up>", self._on_left_arrow_press)
        self.left_list.bind("<KeyPress-Down>", self._on_left_arrow_press)
        self.left_list.bind("<KeyRelease-Up>", self._on_left_arrow_release)
        self.left_list.bind("<KeyRelease-Down>", self._on_left_arrow_release)
        self.left_list.bind(
            "<Double-Button-1>",
            lambda _e: (None if self.auto_preview_var.get() else self.preview_from_left(), "break")[1],
        )
        self.left_list.bind("<space>", lambda _e: (self.preview_from_left(), "break")[1])

        pv = ttk.Frame(self.left_frame)
        pv.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.stop_btn = ttk.Button(pv, text=tr("stop_btn"), command=self.player.stop)
        self.stop_btn.pack(side="left")
        self.auto_preview_var = tk.BooleanVar(value=True)
        self.auto_preview_chk = ttk.Checkbutton(
            pv, text=tr("auto_preview_chk"), variable=self.auto_preview_var
        )
        self.auto_preview_chk.pack(side="left", padx=8)
        self.left_count_var = tk.StringVar(value="")
        ttk.Label(pv, textvariable=self.left_count_var).pack(side="right")

    def _build_center(self, parent: ttk.Frame) -> None:
        center = ttk.Frame(parent, padding=(8, 0))
        center.grid(row=0, column=1, sticky="ns")
        center.rowconfigure(0, weight=1)
        center.rowconfigure(6, weight=1)
        btn_kw = dict(width=12, pady=6, relief="raised", borderwidth=2, cursor="hand2")
        self.add_btn = tk.Button(
            center,
            text=tr("add_btn"),
            bg="#2e9e4f",
            fg="white",
            activebackground="#237a3c",
            activeforeground="white",
            command=self.on_add,
            **btn_kw,
        )
        self.add_btn.grid(row=1, column=0, pady=4)
        self.remove_btn = tk.Button(
            center,
            text=tr("remove_btn"),
            bg="#d32f2f",
            fg="white",
            activebackground="#a82424",
            activeforeground="white",
            command=self.on_remove,
            **btn_kw,
        )
        self.remove_btn.grid(row=2, column=0, pady=4)
        ttk.Separator(center, orient="horizontal").grid(row=3, column=0, sticky="ew", pady=8)
        self.guess_btn = ttk.Button(center, text=tr("guess_btn"), command=self.on_guess)
        self.guess_btn.grid(row=4, column=0, sticky="ew", pady=2)
        self.clear_btn = ttk.Button(center, text=tr("clear_all_btn"), command=self.on_clear_all)
        self.clear_btn.grid(row=5, column=0, sticky="ew", pady=2)

    def _build_right(self, parent: ttk.Frame) -> None:
        self.right_frame = ttk.LabelFrame(parent, text=tr("right_frame_title"), padding=6)
        self.right_frame.grid(row=0, column=2, sticky="nsew")
        self.right_frame.columnconfigure(0, weight=1)
        self.right_frame.rowconfigure(2, weight=1)

        # Top control row inside right panel: "单独定义 LBD" checkbox (default unchecked)
        right_opts = ttk.Frame(self.right_frame)
        right_opts.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        self.separate_lbd_var = tk.BooleanVar(value=False)
        self.separate_lbd_chk = ttk.Checkbutton(
            right_opts,
            text=tr("separate_lbd_chk"),
            variable=self.separate_lbd_var,
            command=self._on_toggle_separate_lbd,
        )
        self.separate_lbd_chk.pack(side="left")

        # Multi-row grid button switcher (3 rows × 5 columns)
        self.tab_grid_frame = ttk.Frame(self.right_frame)
        self.tab_grid_frame.grid(row=1, column=0, sticky="ew", pady=(0, 4))
        for col_idx in range(TAB_GRID_COLUMNS):
            self.tab_grid_frame.columnconfigure(col_idx, weight=1, uniform="tabgrid")

        self.tab_buttons: Dict[TabId, tk.Button] = {}
        for spec in TAB_SPECS:
            btn = tk.Button(
                self.tab_grid_frame,
                text=tab_title(spec.tab_id, 0),
                pady=3,
                relief="raised",
                borderwidth=1,
                cursor="hand2",
                command=lambda t=spec.tab_id: self.select_tab(t),
            )
            self.tab_buttons[spec.tab_id] = btn

        # Shared panel container below the button grid
        self.panel_container = ttk.Frame(self.right_frame)
        self.panel_container.grid(row=2, column=0, sticky="nsew")
        self.panel_container.columnconfigure(0, weight=1)
        self.panel_container.rowconfigure(0, weight=1)

        self.panels: Dict[TabId, TabPanel] = {}
        for spec in TAB_SPECS:
            panel = TabPanel(self, self.panel_container, spec.tab_id)
            self.panels[spec.tab_id] = panel

        self._rebuild_tab_grid()
        self.select_tab(TabId.LC)

        self.map_json_frame = ttk.Frame(self.right_frame)
        self.map_json_frame.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        self.save_map_btn = ttk.Button(
            self.map_json_frame, text=tr("save_map_btn"), command=self.on_save_mapping
        )
        self.save_map_btn.pack(side="left")
        self.load_map_btn = ttk.Button(
            self.map_json_frame, text=tr("load_map_btn"), command=self.on_load_mapping
        )
        self.load_map_btn.pack(side="left", padx=6)

    def _build_export(self, root: tk.Tk) -> None:
        self.export_frame = ttk.LabelFrame(root, text=tr("export_frame_title"), padding=6)
        self.export_frame.grid(row=3, column=0, sticky="ew", padx=8, pady=6)
        self.export_frame.columnconfigure(1, weight=1)
        self.output_dir_label = ttk.Label(self.export_frame, text=tr("output_dir_label"))
        self.output_dir_label.grid(row=0, column=0, sticky="w")
        self.out_var = tk.StringVar()
        ttk.Entry(self.export_frame, textvariable=self.out_var).grid(
            row=0, column=1, sticky="ew", padx=4
        )
        self.browse_btn = ttk.Button(
            self.export_frame, text=tr("browse_btn"), command=self.on_pick_output
        )
        self.browse_btn.grid(row=0, column=2)
        self.export_btn = ttk.Button(
            self.export_frame,
            text=tr("start_export_btn"),
            command=self.on_export,
            state="disabled",
        )
        self.export_btn.grid(row=0, column=3, rowspan=2, padx=(12, 0), ipadx=14, ipady=6)

        opts = ttk.Frame(self.export_frame)
        opts.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        self.utf8_var = tk.BooleanVar(value=False)
        self.utf8_chk = ttk.Checkbutton(opts, text=tr("utf8_chk"), variable=self.utf8_var)
        self.utf8_chk.pack(side="left")
        self.ogg_var = tk.BooleanVar(value=False)
        self.ogg_chk = ttk.Checkbutton(opts, text=tr("transcode_ogg_chk"), variable=self.ogg_var)
        self.ogg_chk.pack(side="left", padx=12)
        self.dlevel_label = ttk.Label(opts, text=tr("dlevel_label"))
        self.dlevel_label.pack(side="left", padx=(12, 2))
        self.dlevel_var = tk.StringVar(value="50")
        ttk.Entry(opts, textvariable=self.dlevel_var, width=6).pack(side="left")

        # Initialize progressbar in determinate mode with value=0 so no green block is shown at rest
        self.progress = ttk.Progressbar(
            self.export_frame, mode="determinate", maximum=100, value=0
        )
        self.progress.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(6, 0))
        self.open_out_btn = ttk.Button(
            self.export_frame,
            text=tr("open_output_btn"),
            command=self.open_output_folder,
            state="disabled",
        )
        self.open_out_btn.grid(row=0, column=4, rowspan=2, padx=(8, 0))

    # ---------------------------------------------------- language preferences
    def _on_language_selected(self, _event: Any = None) -> None:
        selected_label = self.lang_var.get()
        lang_code = LABEL_TO_LANGUAGE.get(selected_label, "zh_CN")
        self.change_language(lang_code)

    def change_language(self, lang_code: str) -> None:
        norm = set_language(lang_code)
        self.lang_var.set(LANGUAGE_LABELS.get(norm, "简体中文"))
        if self._persist_lang:
            save_language_preference(norm, self.config_path)
        self.apply_language()

    def apply_language(self) -> None:
        """Dynamically refresh all UI labels and texts in the current language."""
        self.open_btn.configure(text=tr("open_bms_btn"))
        if self.chart is None:
            self.path_var.set(tr("no_chart_selected"))
        self.pick_ffmpeg_btn.configure(text=tr("pick_ffmpeg_btn"))
        self.lang_label.configure(text=tr("lang_label"))
        self._update_ffmpeg_ui()

        self.health.configure(text=tr("health_frame_title"))
        self._render_health(self.chart)

        self.left_frame.configure(text=tr("left_frame_title"))
        self.search_label.configure(text=tr("search_label"))
        self.clear_search_btn.configure(text=tr("clear_search_btn"))
        self.only_used_chk.configure(text=tr("only_used_chk"))
        self.stop_btn.configure(text=tr("stop_btn"))
        self.auto_preview_chk.configure(text=tr("auto_preview_chk"))

        self.add_btn.configure(text=tr("add_btn"))
        self.remove_btn.configure(text=tr("remove_btn"))
        self.guess_btn.configure(text=tr("guess_btn"))
        self.clear_btn.configure(text=tr("clear_all_btn"))

        self.right_frame.configure(text=tr("right_frame_title"))
        self.separate_lbd_chk.configure(text=tr("separate_lbd_chk"))
        self.save_map_btn.configure(text=tr("save_map_btn"))
        self.load_map_btn.configure(text=tr("load_map_btn"))
        for panel in self.panels.values():
            panel.update_language_texts()

        self.export_frame.configure(text=tr("export_frame_title"))
        self.output_dir_label.configure(text=tr("output_dir_label"))
        self.browse_btn.configure(text=tr("browse_btn"))
        self.export_btn.configure(text=tr("start_export_btn"))
        self.open_out_btn.configure(text=tr("open_output_btn"))
        self.utf8_chk.configure(text=tr("utf8_chk"))
        self.ogg_chk.configure(text=tr("transcode_ogg_chk"))
        self.dlevel_label.configure(text=tr("dlevel_label"))

        self.refresh_all()
        if not self._busy:
            if self.chart is None:
                self.set_status(tr("status_ready"))
            else:
                self.set_status(
                    tr(
                        "status_loaded",
                        name=self.chart.source_path.name,
                        wavs=len(self.chart.wavs),
                        events=len(self.chart.events),
                    )
                )

    # ------------------------------------------------- right tab grid switcher
    def visible_tabs(self) -> List[TabId]:
        return [spec.tab_id for spec in visible_tab_specs(self.separate_lbd_var.get())]

    def _rebuild_tab_grid(self) -> None:
        for btn in self.tab_buttons.values():
            btn.grid_forget()
        vis = visible_tab_specs(self.separate_lbd_var.get())
        for idx, spec in enumerate(vis):
            r, c = divmod(idx, TAB_GRID_COLUMNS)
            self.tab_buttons[spec.tab_id].grid(
                row=r, column=c, sticky="ew", padx=2, pady=2
            )
        self._update_tab_button_styles()

    def _update_tab_button_styles(self) -> None:
        for spec in TAB_SPECS:
            tab = spec.tab_id
            count = self.model.count(tab)
            btn = self.tab_buttons[tab]
            btn.configure(text=tab_title(tab, count))
            if tab == self._active_tab:
                btn.configure(
                    bg=ACTIVE_TAB_BG,
                    fg=ACTIVE_TAB_FG,
                    activebackground=ACTIVE_TAB_BG,
                    activeforeground=ACTIVE_TAB_FG,
                    relief="sunken",
                    font=self.bold_font,
                )
            else:
                fg = ASSIGNED_TAB_FG if count > 0 else INACTIVE_TAB_FG
                font = self.bold_font if count > 0 else self.normal_font
                btn.configure(
                    bg=INACTIVE_TAB_BG,
                    fg=fg,
                    activebackground="#e0e0e0",
                    activeforeground=fg,
                    relief="raised",
                    font=font,
                )

    def select_tab(self, tab: TabId) -> None:
        if tab == TabId.LBD and not self.separate_lbd_var.get():
            self.separate_lbd_var.set(True)
            self.model.set_separate_lbd(True)
            self._rebuild_tab_grid()
        for t, panel in self.panels.items():
            if t == tab:
                panel.frame.grid(row=0, column=0, sticky="nsew")
            else:
                panel.frame.grid_remove()
        self._active_tab = tab
        self._update_tab_button_styles()

    def _on_toggle_separate_lbd(self) -> None:
        enabled = bool(self.separate_lbd_var.get())
        self.model.set_separate_lbd(enabled)
        if not enabled and self._active_tab == TabId.LBD:
            self._active_tab = TabId.LP
        self._rebuild_tab_grid()
        self.select_tab(self._active_tab)
        self.refresh_left()
        self.refresh_tab(TabId.LBD)

    # ----------------------------------------------------------------- helpers
    def set_status(self, text: str) -> None:
        self.status_var.set(text)

    def _poll_queue(self) -> None:
        try:
            while True:
                item = self._queue.get_nowait()
                kind = item[0]
                if kind == "preview_status":
                    self.set_status(item[1])
                elif kind == "job":
                    _kind, on_done, on_error, payload, is_error = item
                    (on_error if is_error else on_done)(payload)
        except queue.Empty:
            pass
        try:
            self._poll_id = self.root.after(80, self._poll_queue)
        except tk.TclError:
            self._poll_id = None

    def run_background(
        self,
        work: Callable[[], Any],
        on_done: Callable[[Any], None],
        on_error: Callable[[BaseException], None],
    ) -> None:
        def runner() -> None:
            try:
                result = work()
            except BaseException as exc:  # noqa: BLE001 - surfaced to UI
                traceback.print_exc() if sys.stderr else None
                self._queue.put(("job", on_done, on_error, exc, True))
            else:
                self._queue.put(("job", on_done, on_error, result, False))

        threading.Thread(target=runner, name="bms2dtx-worker", daemon=True).start()

    def set_busy(self, busy: bool, *, completed: bool = False) -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        for btn in (
            self.open_btn,
            self.add_btn,
            self.remove_btn,
            self.guess_btn,
            self.clear_btn,
        ):
            btn.configure(state=state)
        self.export_btn.configure(
            state="disabled" if busy or self.chart is None else "normal"
        )
        if busy:
            self.progress.configure(mode="indeterminate", maximum=100, value=0)
            self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.configure(
                mode="determinate", maximum=100, value=100 if completed else 0
            )

    # ------------------------------------------------------------------ ffmpeg
    def detect_ffmpeg(self) -> None:
        try:
            self.ffmpeg_path = find_ffmpeg(self.ffmpeg_explicit)
        except FfmpegNotFoundError:
            self.ffmpeg_path = None
        self._update_ffmpeg_ui()

    def _update_ffmpeg_ui(self) -> None:
        if self.ffmpeg_path:
            self.ffmpeg_var.set(tr("ffmpeg_ok", path=self.ffmpeg_path))
            self.ffmpeg_label.configure(foreground=OK_GREEN)
            self.ogg_chk.configure(state="normal")
        else:
            self.ffmpeg_var.set(tr("ffmpeg_missing_short"))
            self.ffmpeg_label.configure(foreground=ERROR_RED)
            self.ogg_var.set(False)
            self.ogg_chk.configure(state="disabled")

    def _warn_ffmpeg_missing(self) -> None:
        if self._ffmpeg_warned or self.ffmpeg_path is not None:
            return
        self._ffmpeg_warned = True
        messagebox.showwarning(
            tr("dialog_ffmpeg_missing_title"),
            tr("dialog_ffmpeg_missing_body"),
            parent=self.root,
        )

    def on_pick_ffmpeg(self) -> None:
        path = filedialog.askopenfilename(
            parent=self.root,
            title=tr("pick_ffmpeg_btn"),
            filetypes=[("ffmpeg.exe", "*.exe"), (tr("filetypes_all"), "*.*")],
        )
        if not path:
            return
        try:
            resolved = find_ffmpeg(path)
            flags = (
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                if sys.platform == "win32"
                else 0
            )
            proc = subprocess.run(
                [resolved, "-version"],
                capture_output=True,
                timeout=20,
                creationflags=flags,
            )
            if proc.returncode != 0:
                raise RuntimeError("ffmpeg -version exited with non-zero code")
        except Exception as exc:
            messagebox.showerror(
                tr("dialog_ffmpeg_invalid_title"),
                tr("dialog_ffmpeg_invalid_body", exc=exc),
                parent=self.root,
            )
            return
        self.ffmpeg_explicit = path
        self.detect_ffmpeg()
        self.set_status(tr("status_ffmpeg_enabled", path=self.ffmpeg_path))

    # --------------------------------------------------------------- load chart
    def on_choose_file(self) -> None:
        filetypes = [
            (tr("filetypes_bms"), "*.bms *.bme *.bml"),
            (tr("filetypes_all"), "*.*"),
        ]
        path = filedialog.askopenfilename(
            parent=self.root, title=tr("open_bms_btn"), filetypes=filetypes
        )
        if path:
            self.load_bms(path)

    def load_bms(self, path: str | Path) -> None:
        p = Path(path)
        if not p.is_file():
            messagebox.showerror(
                tr("dialog_file_not_found_title"),
                tr("dialog_file_not_found_body", path=p),
                parent=self.root,
            )
            return
        self.player.stop()
        self.set_busy(True)
        self.set_status(tr("status_parsing", name=p.name))
        self.run_background(
            lambda: inspect_bms(p),
            self._on_chart_loaded,
            lambda exc: self._on_load_failed(p, exc),
        )

    def _on_load_failed(self, path: Path, exc: BaseException) -> None:
        self.set_busy(False, completed=False)
        self.set_status(tr("status_parse_failed"))
        messagebox.showerror(
            tr("status_parse_failed"),
            f"{path.name}\n\n{type(exc).__name__}: {exc}",
            parent=self.root,
        )

    def _on_chart_loaded(self, chart: BmsChart) -> None:
        self.apply_chart(chart)
        self.set_busy(False, completed=False)
        self.set_status(
            tr(
                "status_loaded",
                name=chart.source_path.name,
                wavs=len(chart.wavs),
                events=len(chart.events),
            )
        )
        if radix_needs_notice(chart):
            messagebox.showwarning(
                tr("dialog_radix_title"),
                tr("dialog_radix_body"),
                parent=self.root,
            )

    def apply_chart(self, chart: BmsChart) -> None:
        self.chart = chart
        self.model.set_chart(chart)
        self.model.clear()
        self.model.set_separate_lbd(self.separate_lbd_var.get())
        self.path_var.set(str(chart.source_path))
        self._render_health(chart)
        self.out_var.set(str(suggest_output_dir(chart.source_path)))
        self.dlevel_var.set(default_dlevel_text(chart))
        self.search_var.set("")
        self.refresh_all()
        self.export_btn.configure(state="normal")
        self.open_out_btn.configure(state="disabled")
        self.progress.stop()
        self.progress.configure(mode="determinate", maximum=100, value=0)

    def _render_health(self, chart: Optional[BmsChart]) -> None:
        for w in self._health_widgets:
            w.destroy()
        self._health_widgets.clear()
        self.warn_btn.grid_forget()
        if chart is None:
            lbl = ttk.Label(self.health, text=tr("health_placeholder"))
            lbl.grid(row=0, column=0, columnspan=4, sticky="w")
            self._health_widgets.append(lbl)
            return
        rows = build_health_summary(chart)
        radix_key = tr("health_radix")
        for i, (name, value) in enumerate(rows[:-1]):
            col_block = 0 if i < 4 else 2
            r = i if i < 4 else i - 4
            title = ttk.Label(self.health, text=f"{name}:", foreground="#555555")
            title.grid(row=r, column=col_block, sticky="ne", padx=(0, 4))
            is_radix = name == radix_key and radix_needs_notice(chart)
            kwargs: Dict[str, Any] = {}
            if is_radix:
                kwargs = {"foreground": ERROR_RED, "font": self.bold_font}
            val = ttk.Label(
                self.health, text=value, wraplength=420, justify="left", **kwargs
            )
            val.grid(row=r, column=col_block + 1, sticky="nw", padx=(0, 18))
            self._health_widgets.extend([title, val])
        self.health.columnconfigure(3, weight=1)
        self.warn_btn.configure(
            text=tr("view_warnings_btn", count=len(chart.warnings)), state="normal"
        )
        self.warn_btn.grid(row=3, column=2, columnspan=2, sticky="e")

    def show_chart_warnings(self) -> None:
        if self.chart is not None:
            self.show_text_dialog(
                tr("dialog_warnings_title", count=len(self.chart.warnings)),
                format_warnings(self.chart.warnings),
            )

    def show_text_dialog(
        self, title: str, text: str, *, open_dir: Optional[Path] = None
    ) -> None:
        top = tk.Toplevel(self.root)
        top.title(title)
        top.transient(self.root)
        top.geometry("760x480")
        top.columnconfigure(0, weight=1)
        top.rowconfigure(0, weight=1)
        txt = tk.Text(top, wrap="word")
        sb = ttk.Scrollbar(top, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        txt.insert("1.0", text)
        txt.configure(state="disabled")
        txt.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        btns = ttk.Frame(top, padding=6)
        btns.grid(row=1, column=0, columnspan=2, sticky="e")
        if open_dir is not None:
            ttk.Button(
                btns,
                text=tr("open_output_btn"),
                command=lambda: self.open_folder(open_dir),
            ).pack(side="left", padx=4)
        ttk.Button(btns, text=tr("dialog_close_btn"), command=top.destroy).pack(
            side="left"
        )

    # --------------------------------------------------------- list refreshing
    def active_tab(self) -> TabId:
        return self._active_tab

    def refresh_all(self) -> None:
        self.refresh_left()
        for tab in self.panels:
            self.refresh_tab(tab)
        self._update_tab_button_styles()

    def refresh_tab(self, tab: TabId) -> None:
        self.panels[tab].refresh()
        self._update_tab_button_styles()

    def refresh_left(self) -> None:
        keep = {
            self._left_ids[i]
            for i in self.left_list.curselection()
            if i < len(self._left_ids)
        }
        yview = self.left_list.yview()[0]
        self.left_list.delete(0, tk.END)
        self._left_ids = []
        if self.chart is None:
            self.left_count_var.set("")
            return
        owner = {wid: t for t, ids in self.model.tab_wavs.items() for wid in ids}
        self._left_ids = filter_wav_ids(
            self.chart, self.search_var.get(), self.only_used_var.get()
        )
        rep_to_group, _wid_to_rep = wav_physical_groups(self.chart)
        all_groups = list(rep_to_group.keys())
        assigned_groups = 0
        for rep_id in all_groups:
            if owner.get(rep_id) is not None:
                assigned_groups += 1

        for idx, wid in enumerate(self._left_ids):
            tab = owner.get(wid)
            self.left_list.insert(
                tk.END,
                format_wav_row(
                    self.chart.wavs[wid],
                    tab,
                    chart=self.chart,
                    group_ids=rep_to_group.get(wid),
                ),
            )
            if tab is not None:
                self.left_list.itemconfig(
                    idx,
                    foreground=GRAY_FOREGROUND,
                    selectforeground=GRAY_FOREGROUND,
                )
            if wid in keep:
                self.left_list.selection_set(idx)
        self.left_list.yview_moveto(yview)
        self.left_count_var.set(
            tr(
                "left_count_summary",
                shown=len(self._left_ids),
                total=len(all_groups),
                assigned=assigned_groups,
            )
        )

    def selected_left_ids(self) -> List[str]:
        return [
            self._left_ids[i]
            for i in self.left_list.curselection()
            if i < len(self._left_ids)
        ]

    # ----------------------------------------------------------------- shuttle
    def on_add(self) -> None:
        if self.chart is None:
            return
        ids = self.selected_left_ids()
        if not ids:
            self.set_status(tr("status_select_left_first"))
            return
        tab = self.active_tab()
        added, skipped = self.model.assign(tab, ids, chart=self.chart)
        self.refresh_left()
        self.refresh_tab(tab)
        msg = tr("status_added_to_tab", added=len(added), tab=tab_label(tab))
        if skipped:
            msg += tr("status_added_skipped_suffix", skipped=len(skipped))
        self.set_status(msg)

    def on_remove(self) -> None:
        if self.chart is None:
            return
        tab = self.active_tab()
        ids = self.panels[tab].selected_ids()
        if not ids:
            self.set_status(tr("status_select_right_first"))
            return
        removed = self.model.remove(tab, ids, chart=self.chart)
        self.refresh_left()
        self.refresh_tab(tab)
        self.set_status(
            tr("status_removed_from_tab", removed=len(removed), tab=tab_label(tab))
        )

    def on_clear_all(self) -> None:
        if self.chart is None or not self.model.has_assignments:
            return
        if not messagebox.askyesno(
            tr("dialog_clear_title"), tr("dialog_clear_body"), parent=self.root
        ):
            return
        self.model.clear()
        self.refresh_all()
        self.set_status(tr("status_cleared_all"))

    def on_guess(self) -> None:
        """Manual helper only — never invoked automatically on import."""
        if self.chart is None:
            return
        assigned = self.model.assigned_ids
        # Guess on representative IDs (and expand to sibling physical-file IDs via model.assign)
        rep_ids = filter_wav_ids(self.chart, "", False)
        candidates = [
            (wid, self.chart.wavs[wid].resolved_name)
            for wid in rep_ids
            if wid not in assigned
        ]
        guesses = guess_assignments(candidates)
        if not self.separate_lbd_var.get() and TabId.LBD in guesses:
            # When LBD is hidden, route guessed LBD samples into BD
            lbd_ids = guesses.pop(TabId.LBD)
            guesses.setdefault(TabId.BD, []).extend(lbd_ids)
        total = sum(len(v) for v in guesses.values())
        if total == 0:
            messagebox.showinfo(
                tr("guess_btn"), tr("dialog_guess_none_body"), parent=self.root
            )
            return
        if self.model.has_assignments:
            if not messagebox.askyesno(
                tr("guess_btn"),
                tr(
                    "dialog_guess_confirm_body",
                    assigned=self.model.total_assigned,
                    total=total,
                ),
                parent=self.root,
            ):
                return
        for tab, ids in guesses.items():
            self.model.assign(tab, ids, chart=self.chart)
        self.refresh_all()
        self.set_status(tr("status_guessed", total=total))

    # ----------------------------------------------------------------- preview
    def _play_wav_id(self, wav_id: Optional[str]) -> None:
        if not wav_id or self.chart is None:
            self.set_status(tr("status_select_slice_first"))
            return
        entry = self.chart.wavs.get(wav_id)
        if entry is None or not entry.exists or entry.resolved_path is None:
            self.set_status(tr("status_file_missing_no_preview", wav_id=wav_id))
            return
        self.player.play(entry.resolved_path)

    def _on_left_click_index(self, idx: int) -> None:
        if not self.auto_preview_var.get() or self.chart is None:
            return
        if 0 <= idx < len(self._left_ids):
            self._play_wav_id(self._left_ids[idx])

    def _on_left_click(self, event: Any) -> None:
        if not self._left_ids:
            return
        idx = self.left_list.nearest(event.y)
        self._on_left_click_index(idx)

    def _on_left_arrow_press(self, _event: Any = None) -> None:
        if not self.auto_preview_var.get() or self.chart is None or not self._left_ids:
            return
        try:
            self.root.after_idle(self._on_left_arrow_after_keypress)
        except Exception:
            pass

    def _on_left_arrow_after_keypress(self) -> None:
        if not self.auto_preview_var.get() or self.chart is None or not self._left_ids:
            return
        try:
            active_idx = int(self.left_list.index(tk.ACTIVE))
        except Exception:
            return
        if 0 <= active_idx < len(self._left_ids):
            self._arrow_previewed_idx = active_idx
            self._play_wav_id(self._left_ids[active_idx])

    def _on_left_arrow_release(self, _event: Any = None) -> None:
        if not self.auto_preview_var.get() or self.chart is None or not self._left_ids:
            return
        try:
            active_idx = int(self.left_list.index(tk.ACTIVE))
        except Exception:
            return
        if self._arrow_previewed_idx == active_idx:
            self._arrow_previewed_idx = None
            return
        self._arrow_previewed_idx = None
        if 0 <= active_idx < len(self._left_ids):
            self._play_wav_id(self._left_ids[active_idx])

    def _on_left_arrow_key(self, _event: Any = None) -> None:
        if not self.auto_preview_var.get() or self.chart is None or not self._left_ids:
            return
        try:
            active_idx = int(self.left_list.index(tk.ACTIVE))
        except Exception:
            return
        if 0 <= active_idx < len(self._left_ids):
            self._play_wav_id(self._left_ids[active_idx])

    def preview_from_left(self) -> bool:
        if not self._left_ids:
            self._play_wav_id(None)
            return True
        cur = self.left_list.curselection()
        try:
            active_idx = int(self.left_list.index(tk.ACTIVE))
        except Exception:
            active_idx = -1
        if active_idx in cur and 0 <= active_idx < len(self._left_ids):
            self._play_wav_id(self._left_ids[active_idx])
        else:
            ids = self.selected_left_ids()
            self._play_wav_id(ids[0] if ids else None)
        return True

    def preview_from_tab(self, tab: TabId) -> bool:
        ids = self.panels[tab].selected_ids()
        self._play_wav_id(ids[0] if ids else None)
        return True

    def on_preview_button(self) -> None:
        ids = self.selected_left_ids()
        if not ids:
            ids = (
                self.panels[self.active_tab()].selected_ids() if self.chart else []
            )
        self._play_wav_id(ids[0] if ids else None)

    # --------------------------------------------------------- mapping JSON I/O
    def on_save_mapping(self) -> None:
        path = filedialog.asksaveasfilename(
            parent=self.root,
            title=tr("save_map_btn"),
            defaultextension=".json",
            filetypes=[("JSON", "*.json")],
        )
        if not path:
            return
        try:
            save_mapping_json(self.model, path)
        except OSError as exc:
            messagebox.showerror(
                tr("dialog_save_failed_title"), str(exc), parent=self.root
            )
            return
        self.set_status(tr("status_map_saved", path=path))

    def on_load_mapping(self) -> None:
        if self.chart is None:
            messagebox.showinfo(
                tr("dialog_load_map_title"),
                tr("dialog_load_map_first_body"),
                parent=self.root,
            )
            return
        path = filedialog.askopenfilename(
            parent=self.root,
            title=tr("load_map_btn"),
            filetypes=[("JSON", "*.json")],
        )
        if not path:
            return
        try:
            notes = load_mapping_json(self.model, path, self.chart)
        except Exception as exc:
            messagebox.showerror(
                tr("dialog_load_failed_title"),
                f"{type(exc).__name__}: {exc}",
                parent=self.root,
            )
            return
        if self.model.separate_lbd != self.separate_lbd_var.get():
            self.separate_lbd_var.set(self.model.separate_lbd)
            self._rebuild_tab_grid()
        self.refresh_all()
        self.set_status(tr("status_map_loaded", count=self.model.total_assigned))
        if notes:
            self.show_text_dialog(tr("dialog_map_notes_title"), "\n".join(notes))

    # ------------------------------------------------------------------ export
    def on_pick_output(self) -> None:
        initial = self.out_var.get().strip() or None
        path = filedialog.askdirectory(
            parent=self.root,
            title=tr("dialog_select_out_title"),
            initialdir=initial,
            mustexist=False,
        )
        if not path:
            return
        if self.chart is not None:
            err = check_output_dir(path, self.chart)
            if err:
                messagebox.showerror(
                    tr("dialog_invalid_out_title"), err, parent=self.root
                )
                return
        self.out_var.set(str(Path(path)))

    def on_export(self) -> None:
        if self.chart is None or self._busy:
            return
        chart = self.chart
        out_text = self.out_var.get().strip()
        if not out_text:
            messagebox.showerror(
                tr("dialog_select_out_title"),
                tr("dialog_select_out_body"),
                parent=self.root,
            )
            return
        err = check_output_dir(out_text, chart)
        if err:
            messagebox.showerror(
                tr("dialog_invalid_out_title"), err, parent=self.root
            )
            return
        try:
            dlevel = parse_dlevel(self.dlevel_var.get())
        except ValueError:
            messagebox.showerror(
                tr("dialog_dlevel_err_title"),
                tr("dialog_dlevel_err_body"),
                parent=self.root,
            )
            return

        render_bgm = True
        if self.ffmpeg_path is None:
            if not messagebox.askyesno(
                tr("dialog_ffmpeg_missing_title"),
                tr("dialog_export_no_ffmpeg_body"),
                parent=self.root,
            ):
                return
            render_bgm = False

        out_dir = Path(out_text)
        options = ConversionOptions(
            output_dir=out_dir,
            utf8=self.utf8_var.get(),
            transcode_wav_to_ogg=bool(self.ogg_var.get() and self.ffmpeg_path),
            dlevel=dlevel,
            ffmpeg_path=self.ffmpeg_path,
            render_bgm=render_bgm,
        )
        mapping = self.model.to_mapping_config()
        self.set_busy(True)
        self.open_out_btn.configure(state="disabled")
        self.set_status(tr("status_exporting"))

        def work() -> Any:
            return convert_bms(chart.source_path, options, mapping, parsed_chart=chart)

        self.run_background(work, self._on_export_done, self._on_export_failed)

    def _on_export_done(self, result: core.ConversionResult) -> None:
        self.set_busy(False, completed=True)
        self.last_output_dir = result.output_dir
        self.open_out_btn.configure(state="normal")
        self.set_status(tr("status_export_done", path=result.dtx_path))
        self.show_text_dialog(
            tr("dialog_export_done_title"),
            build_result_summary(result),
            open_dir=result.output_dir,
        )

    def _on_export_failed(self, exc: BaseException) -> None:
        self.set_busy(False, completed=False)
        self.set_status(tr("status_export_failed"))
        messagebox.showerror(
            tr("status_export_failed"),
            f"{type(exc).__name__}: {exc}",
            parent=self.root,
        )

    def open_output_folder(self) -> None:
        target = self.last_output_dir or (
            Path(self.out_var.get()) if self.out_var.get().strip() else None
        )
        if target is not None:
            self.open_folder(target)

    def open_folder(self, folder: Path) -> None:
        try:
            if not folder.is_dir():
                raise FileNotFoundError(str(folder))
            os.startfile(str(folder))  # type: ignore[attr-defined]  # Windows only
        except Exception as exc:
            messagebox.showerror(
                tr("dialog_open_folder_err_title"), str(exc), parent=self.root
            )

    # ------------------------------------------------------------------- close
    def on_close(self) -> None:
        try:
            self.player.shutdown()
            if self._poll_id is not None:
                try:
                    self.root.after_cancel(self._poll_id)
                except tk.TclError:
                    pass
                self._poll_id = None
        finally:
            self.root.destroy()


def run(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    initial = next((a for a in args if not a.startswith("-")), None)
    enable_hidpi()
    root = tk.Tk()
    App(root, initial)
    root.mainloop()
    return 0
