"""bms2dtx desktop GUI (tkinter / ttk). v0.3.0: Toggle = OFF only."""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import tkinter.font as tkfont

import core
from core import (
    AUTO_LP_CHIP_EMPTY_ZZ,
    AUTO_LP_CHIP_NONE,
    AUTO_LP_THRESHOLD_MS,
    AUTO_LP_THRESHOLD_NOTE,
    AUTO_LP_VALID_DENOMINATORS,
    DEFAULT_WAV_LABELS,
    IMAGE_MODE_KEEP,
    IMAGE_MODE_NONE,
    IMAGE_MODE_RESIZE,
    TAB_SPEC_MAP,
    TAB_SPECS,
    WAV_LABEL_KEYS,
    BmsChart,
    ConversionOptions,
    FfmpegNotFoundError,
    TabId,
    convert_bms,
    find_ffmpeg,
    inspect_bms,
    sanitize_wav_label,
)

from . import APP_NAME, APP_VERSION
from .auto_guess import guess_assignments
from .crash_log import attach_tk_crash_handler, get_crash_log_dir, install_crash_handlers
from .helpers import (
    AUTO_BIND_SENTINEL,
    GRAY_FOREGROUND,
    AssignmentModel,
    auto_lp_combobox_values,
    bind_combobox_values,
    build_health_summary,
    build_result_summary,
    check_output_dir,
    default_dlevel_text,
    filter_wav_ids,
    format_tab_row,
    format_warnings,
    format_wav_tree_values,
    get_no_bind_label,
    get_tab_note,
    load_mapping_json,
    parse_dlevel,
    radix_needs_notice,
    save_mapping_json,
    suggest_load_mapping_location,
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
    load_app_settings,
    save_app_settings,
    set_language,
    tr,
)
from .preview_player import PreviewPlayer

ERROR_RED = "#c62828"
OK_GREEN = "#2e7d32"
WARN_AMBER = "#b26a00"
ACTIVE_TAB_BG = "#1565c0"
ACTIVE_TAB_FG = "#ffffff"
INACTIVE_TAB_BG = "#f0f0f0"
INACTIVE_TAB_FG = "#111111"
ASSIGNED_TAB_FG = "#0d47a1"
TAB_GRID_COLUMNS = 5
MAX_UNDO_HISTORY = 100
_IMAGE_MODES_ORDER = (IMAGE_MODE_KEEP, IMAGE_MODE_RESIZE, IMAGE_MODE_NONE)


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


class _Tooltip:
    """Lightweight dynamic-text hover tooltip for ttk/tk widgets."""

    def __init__(
        self,
        widget: tk.Widget,
        text_getter: Callable[[], str],
        *,
        delay_ms: int = 380,
    ) -> None:
        self._widget = widget
        self._text_getter = text_getter
        self._delay_ms = max(0, int(delay_ms))
        self._tip_win: Optional[tk.Toplevel] = None
        self._after_id: Optional[str] = None
        widget.bind("<Enter>", self._on_enter, add="+")
        widget.bind("<Leave>", self._on_leave, add="+")
        widget.bind("<ButtonPress>", self._on_leave, add="+")
        widget.bind("<Destroy>", self._on_leave, add="+")

    def _on_enter(self, _event: Any = None) -> None:
        self._cancel()
        try:
            self._after_id = self._widget.after(self._delay_ms, self._show)
        except Exception:
            pass

    def _on_leave(self, _event: Any = None) -> None:
        self._cancel()
        self._hide()

    def _cancel(self) -> None:
        if self._after_id is not None:
            try:
                self._widget.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None

    def _show(self) -> None:
        self._after_id = None
        text = self._text_getter()
        if not text:
            return
        try:
            x = self._widget.winfo_rootx() + 16
            y = self._widget.winfo_rooty() + self._widget.winfo_height() + 4
            win = tk.Toplevel(self._widget)
            win.wm_overrideredirect(True)
            win.wm_geometry(f"+{x}+{y}")
            lbl = tk.Label(
                win,
                text=text,
                background="#ffffe0",
                foreground="#222222",
                relief="solid",
                borderwidth=1,
                padx=6,
                pady=3,
                wraplength=320,
                justify="left",
            )
            lbl.pack()
            self._tip_win = win
        except Exception:
            self._hide()

    def _hide(self) -> None:
        if self._tip_win is not None:
            try:
                self._tip_win.destroy()
            except Exception:
                pass
            self._tip_win = None


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
        self._arrow_previewed_idx: Optional[int] = None

        # LP-only Auto-LP dual-row controls
        self._suppress_auto_lp_sync = False
        self.auto_lp_mode_note_rb: Optional[ttk.Radiobutton] = None
        self.auto_lp_mode_ms_rb: Optional[ttk.Radiobutton] = None
        self.auto_lp_label: Optional[ttk.Label] = None
        self.auto_lp_combo: Optional[ttk.Combobox] = None
        self.auto_lp_chip_values: List[str] = [AUTO_LP_CHIP_NONE]
        self.auto_lp_chip_var = tk.StringVar()
        self.auto_lp_vol_label: Optional[ttk.Label] = None
        self.auto_lp_vol_spin: Optional[tk.Spinbox] = None
        self.auto_lp_vol_var = tk.StringVar(value="50")
        self.auto_lp_thresh_mode_var = tk.StringVar(value=AUTO_LP_THRESHOLD_NOTE)
        self.auto_lp_num_var = tk.StringVar(value="1")
        self.auto_lp_den_var = tk.StringVar(value="4")
        self.auto_lp_ms_var = tk.StringVar(value="250")
        self.auto_lp_num_entry: Optional[ttk.Entry] = None
        self.auto_lp_den_combo: Optional[ttk.Combobox] = None
        self.auto_lp_ms_entry: Optional[ttk.Entry] = None
        self._lp_thresh_note_frame: Optional[ttk.Frame] = None
        self._lp_thresh_ms_frame: Optional[ttk.Frame] = None
        self._lp_tip_label: Optional[_Tooltip] = None
        self._lp_tip_combo: Optional[_Tooltip] = None

        row = 0
        note = get_tab_note(tab)
        if note is not None:
            self.note_label = ttk.Label(
                self.frame, text=note, foreground="#1565c0", wraplength=380, justify="left"
            )
            self.note_label.grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 4))
            row += 1
        if tab == TabId.LP:
            # Dual-row compact header for LP Tab:
            # Row 1: compact Default bind | Threshold mode radios (Note Value vs ms)
            lp_row1 = ttk.Frame(self.frame)
            lp_row1.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(0, 3))
            lp_row1.columnconfigure(1, weight=1)
            self.bind_label = ttk.Label(lp_row1, text=tr("lp_default_bind_short"))
            self.bind_label.grid(row=0, column=0, sticky="w", padx=(0, 4))
            self.combo = ttk.Combobox(
                lp_row1,
                textvariable=self.combo_var,
                state="readonly",
                values=[get_no_bind_label()],
                width=18,
            )
            self.combo.grid(row=0, column=1, sticky="ew", padx=(0, 8))
            self.combo.bind("<<ComboboxSelected>>", self._on_bind_selected)

            self.auto_lp_mode_note_rb = ttk.Radiobutton(
                lp_row1,
                text=tr("auto_lp_mode_note"),
                value=AUTO_LP_THRESHOLD_NOTE,
                variable=self.auto_lp_thresh_mode_var,
                command=self._on_auto_lp_changed,
            )
            self.auto_lp_mode_note_rb.grid(row=0, column=2, sticky="e")
            self.auto_lp_mode_ms_rb = ttk.Radiobutton(
                lp_row1,
                text=tr("auto_lp_mode_ms"),
                value=AUTO_LP_THRESHOLD_MS,
                variable=self.auto_lp_thresh_mode_var,
                command=self._on_auto_lp_changed,
            )
            self.auto_lp_mode_ms_rb.grid(row=0, column=3, sticky="e", padx=(6, 0))
            row += 1

            # Row 2: Auto-LP chip selector | Vol (0-100) | <= threshold
            lp_row2 = ttk.Frame(self.frame)
            lp_row2.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(0, 6))
            lp_row2.columnconfigure(1, weight=1)

            self.auto_lp_label = ttk.Label(lp_row2, text=tr("auto_lp_label"))
            self.auto_lp_label.grid(row=0, column=0, sticky="w", padx=(0, 4))
            self.auto_lp_combo = ttk.Combobox(
                lp_row2,
                textvariable=self.auto_lp_chip_var,
                state="readonly",
                width=14,
            )
            self.auto_lp_combo.grid(row=0, column=1, sticky="ew", padx=(0, 6))
            self.auto_lp_combo.bind("<<ComboboxSelected>>", self._on_auto_lp_changed)
            self._lp_tip_label = _Tooltip(
                self.auto_lp_label, lambda: tr("auto_lp_tooltip"), delay_ms=1000
            )
            self._lp_tip_combo = _Tooltip(
                self.auto_lp_combo, lambda: tr("auto_lp_tooltip"), delay_ms=1000
            )

            self.auto_lp_vol_label = ttk.Label(lp_row2, text=tr("auto_lp_vol_label"))
            self.auto_lp_vol_label.grid(row=0, column=2, sticky="e", padx=(0, 2))
            self.auto_lp_vol_spin = tk.Spinbox(
                lp_row2,
                from_=0,
                to=100,
                width=4,
                textvariable=self.auto_lp_vol_var,
                command=self._on_auto_lp_changed,
            )
            self.auto_lp_vol_spin.grid(row=0, column=3, sticky="e", padx=(0, 6))
            self.auto_lp_vol_spin.bind("<FocusOut>", self._on_auto_lp_changed)
            self.auto_lp_vol_spin.bind("<Return>", self._on_auto_lp_changed)

            self._lp_thresh_note_frame = ttk.Frame(lp_row2)
            ttk.Label(self._lp_thresh_note_frame, text="<=").pack(side="left", padx=(0, 2))
            self.auto_lp_num_entry = ttk.Entry(
                self._lp_thresh_note_frame, textvariable=self.auto_lp_num_var, width=3
            )
            self.auto_lp_num_entry.pack(side="left")
            self.auto_lp_num_entry.bind("<FocusOut>", self._on_auto_lp_changed)
            self.auto_lp_num_entry.bind("<Return>", self._on_auto_lp_changed)
            ttk.Label(self._lp_thresh_note_frame, text="/").pack(side="left", padx=2)
            self.auto_lp_den_combo = ttk.Combobox(
                self._lp_thresh_note_frame,
                textvariable=self.auto_lp_den_var,
                state="readonly",
                width=3,
                values=[str(d) for d in AUTO_LP_VALID_DENOMINATORS],
            )
            self.auto_lp_den_combo.pack(side="left")
            self.auto_lp_den_combo.bind("<<ComboboxSelected>>", self._on_auto_lp_changed)

            self._lp_thresh_ms_frame = ttk.Frame(lp_row2)
            ttk.Label(self._lp_thresh_ms_frame, text="<=").pack(side="left", padx=(0, 2))
            self.auto_lp_ms_entry = ttk.Entry(
                self._lp_thresh_ms_frame, textvariable=self.auto_lp_ms_var, width=5
            )
            self.auto_lp_ms_entry.pack(side="left")
            self.auto_lp_ms_entry.bind("<FocusOut>", self._on_auto_lp_changed)
            self.auto_lp_ms_entry.bind("<Return>", self._on_auto_lp_changed)
            ttk.Label(self._lp_thresh_ms_frame, text="ms").pack(side="left", padx=(2, 0))

            self._lp_thresh_note_frame.grid(row=0, column=4, sticky="e")
            row += 1
        elif tab_has_bind_combobox(tab):
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

        self.listbox.bind("<FocusIn>", lambda _e: self.app._set_focus_side("right"))
        self.listbox.bind("<Button-1>", self._on_right_click)
        self.listbox.bind("<KeyPress-Up>", self._on_right_arrow_press)
        self.listbox.bind("<KeyPress-Down>", self._on_right_arrow_press)
        self.listbox.bind("<KeyRelease-Up>", self._on_right_arrow_release)
        self.listbox.bind("<KeyRelease-Down>", self._on_right_arrow_release)
        self.listbox.bind(
            "<Double-Button-1>",
            lambda _e: (
                None if self.app.auto_preview_var.get() else self.app.preview_from_tab(self.tab),
                "break",
            )[1],
        )
        self.listbox.bind("<space>", lambda _e: (self.app.preview_from_tab(self.tab), "break")[1])

    def _on_right_click(self, event: Any) -> Optional[str]:
        self.app._set_focus_side("right")
        if not self.ids:
            self.listbox.selection_clear(0, tk.END)
            return "break"
        idx = self.listbox.nearest(event.y)
        bbox = self.listbox.bbox(idx) if idx >= 0 else None
        if bbox is None or event.y < bbox[1] or event.y > bbox[1] + bbox[3]:
            self.listbox.selection_clear(0, tk.END)
            return "break"
        if self.app.auto_preview_var.get() and self.app.chart is not None and 0 <= idx < len(self.ids):
            self.app._play_wav_id(self.ids[idx])
        return None

    def _on_right_arrow_press(self, _event: Any = None) -> None:
        self.app._set_focus_side("right")
        if not self.app.auto_preview_var.get() or self.app.chart is None or not self.ids:
            return
        try:
            self.app.root.after_idle(self._on_right_arrow_after_keypress)
        except Exception:
            pass

    def _on_right_arrow_after_keypress(self) -> None:
        if not self.app.auto_preview_var.get() or self.app.chart is None or not self.ids:
            return
        try:
            active_idx = int(self.listbox.index(tk.ACTIVE))
        except Exception:
            return
        if 0 <= active_idx < len(self.ids):
            self._arrow_previewed_idx = active_idx
            self.app._play_wav_id(self.ids[active_idx])

    def _on_right_arrow_release(self, _event: Any = None) -> None:
        if not self.app.auto_preview_var.get() or self.app.chart is None or not self.ids:
            return
        try:
            active_idx = int(self.listbox.index(tk.ACTIVE))
        except Exception:
            return
        if self._arrow_previewed_idx == active_idx:
            self._arrow_previewed_idx = None
            return
        self._arrow_previewed_idx = None
        if 0 <= active_idx < len(self.ids):
            self.app._play_wav_id(self.ids[active_idx])

    def _on_bind_selected(self, _event: Any = None) -> None:
        idx = self.combo.current() if self.combo is not None else 0
        token = self.bind_ids[idx] if 0 <= idx < len(self.bind_ids) else None
        before = self.app.model.snapshot()
        try:
            self.app.model.set_bind(self.tab, token)
        except ValueError:
            self.app.model.default_binds.pop(self.tab, None)
        self.app._commit_undo(before)

        eff_wid, is_auto = self.app.model.effective_bind(self.tab, self.app.chart)
        if is_auto and eff_wid:
            wav_label = f"{tr('status_bind_auto_label')} -> {eff_wid}"
        elif eff_wid:
            wav_label = eff_wid
        else:
            wav_label = get_no_bind_label()
        self.app.set_status(
            tr("status_bind_updated", tab=tab_label(self.tab), wav=wav_label)
        )

    def _on_auto_lp_changed(self, _event: Any = None) -> None:
        if self.tab != TabId.LP or self._suppress_auto_lp_sync:
            return
        self.sync_auto_lp_to_model(push_undo=True)
        self._refresh_auto_lp_controls()

    def sync_auto_lp_to_model(self, *, push_undo: bool = True) -> None:
        """Read LP Tab Auto-LP widgets into ``model.auto_lp`` cleanly."""
        if self.tab != TabId.LP or self.auto_lp_combo is None:
            return
        before = self.app.model.snapshot() if push_undo else None
        alp = self.app.model.auto_lp

        cidx = self.auto_lp_combo.current()
        if 0 <= cidx < len(self.auto_lp_chip_values):
            alp.chip_mode = self.auto_lp_chip_values[cidx]

        tmode = self.auto_lp_thresh_mode_var.get()
        alp.threshold_mode = (
            AUTO_LP_THRESHOLD_MS if tmode == AUTO_LP_THRESHOLD_MS else AUTO_LP_THRESHOLD_NOTE
        )

        try:
            vol = max(0, min(100, int(self.auto_lp_vol_var.get().strip())))
        except ValueError:
            vol = alp.volume
        alp.volume = vol

        try:
            num = max(1, int(self.auto_lp_num_var.get().strip()))
        except ValueError:
            num = alp.note_num
        alp.note_num = num

        try:
            den = int(self.auto_lp_den_var.get().strip())
        except ValueError:
            den = alp.note_den
        if den in AUTO_LP_VALID_DENOMINATORS:
            alp.note_den = den

        try:
            ms_val = max(0.0, float(self.auto_lp_ms_var.get().strip()))
        except ValueError:
            ms_val = alp.threshold_ms
        alp.threshold_ms = ms_val

        if push_undo and before is not None:
            self.app._commit_undo(before)

    def _refresh_auto_lp_controls(self) -> None:
        if self.tab != TabId.LP or self.auto_lp_combo is None:
            return
        self._suppress_auto_lp_sync = True
        try:
            model = self.app.model
            chart = self.app.chart
            alp = model.auto_lp

            options = auto_lp_combobox_values(model, chart)
            self.auto_lp_chip_values = [val for _lbl, val in options]
            self.auto_lp_combo.configure(values=[lbl for lbl, _val in options])

            target_idx = 0
            if alp.chip_mode in self.auto_lp_chip_values:
                target_idx = self.auto_lp_chip_values.index(alp.chip_mode)
            else:
                alp.chip_mode = AUTO_LP_CHIP_NONE
            self.auto_lp_combo.current(target_idx)

            self.auto_lp_thresh_mode_var.set(alp.threshold_mode)
            self.auto_lp_vol_var.set(str(alp.volume))
            self.auto_lp_num_var.set(str(alp.note_num))
            self.auto_lp_den_var.set(str(alp.note_den))
            self.auto_lp_ms_var.set(f"{alp.threshold_ms:g}")

            # Disable volume box when chip_mode is None or ZZ: Silent
            if self.auto_lp_vol_spin is not None:
                vol_enabled = alp.chip_mode not in (AUTO_LP_CHIP_NONE, AUTO_LP_CHIP_EMPTY_ZZ)
                self.auto_lp_vol_spin.configure(state="normal" if vol_enabled else "disabled")

            if self._lp_thresh_note_frame is not None and self._lp_thresh_ms_frame is not None:
                if alp.threshold_mode == AUTO_LP_THRESHOLD_MS:
                    self._lp_thresh_note_frame.grid_remove()
                    self._lp_thresh_ms_frame.grid(row=0, column=4, sticky="e")
                else:
                    self._lp_thresh_ms_frame.grid_remove()
                    self._lp_thresh_note_frame.grid(row=0, column=4, sticky="e")
        finally:
            self._suppress_auto_lp_sync = False

    def update_language_texts(self) -> None:
        if self.note_label is not None:
            note = get_tab_note(self.tab)
            if note is not None:
                self.note_label.configure(text=note)
        if self.bind_label is not None:
            self.bind_label.configure(
                text=tr("lp_default_bind_short") if self.tab == TabId.LP else tr("default_bind_label")
            )
        if self.tab == TabId.LP:
            if self.auto_lp_label is not None:
                self.auto_lp_label.configure(text=tr("auto_lp_label"))
            if self.auto_lp_vol_label is not None:
                self.auto_lp_vol_label.configure(text=tr("auto_lp_vol_label"))
            if self.auto_lp_mode_note_rb is not None:
                self.auto_lp_mode_note_rb.configure(text=tr("auto_lp_mode_note"))
            if self.auto_lp_mode_ms_rb is not None:
                self.auto_lp_mode_ms_rb.configure(text=tr("auto_lp_mode_ms"))
            self._refresh_auto_lp_controls()

    def refresh(self) -> None:
        model = self.app.model
        chart = self.app.chart
        keep_sel = {self.ids[i] for i in self.listbox.curselection() if i < len(self.ids)}
        self.ids = model.display_ids_for_tab(self.tab, chart)
        self.listbox.delete(0, tk.END)
        for idx, wid in enumerate(self.ids):
            entry = chart.wavs.get(wid) if chart else None
            self.listbox.insert(tk.END, format_tab_row(entry, wid, chart=chart))
            if wid in keep_sel:
                self.listbox.selection_set(idx)
        if self.combo is not None:
            values = bind_combobox_values(model, chart, self.tab)
            self.bind_ids = [token for _text, token in values]
            self.combo.configure(values=[text for text, _token in values])

            _eff_wid, is_auto = model.effective_bind(self.tab, chart)
            manual = model.default_binds.get(self.tab)
            none_idx = self.bind_ids.index(None) if None in self.bind_ids else 0
            bound_idx = none_idx

            if manual is not None:
                if manual in self.bind_ids:
                    bound_idx = self.bind_ids.index(manual)
                elif chart is not None:
                    group = set(wav_group_for(chart, manual))
                    for candidate_idx, cid in enumerate(self.bind_ids):
                        if cid not in (None, AUTO_BIND_SENTINEL) and cid in group:
                            bound_idx = candidate_idx
                            break
                    else:
                        model.default_binds.pop(self.tab, None)
                        bound_idx = (
                            self.bind_ids.index(AUTO_BIND_SENTINEL)
                            if (model.default_bind_mode == "auto" and AUTO_BIND_SENTINEL in self.bind_ids)
                            else none_idx
                        )
                else:
                    model.default_binds.pop(self.tab, None)
            elif is_auto and AUTO_BIND_SENTINEL in self.bind_ids:
                bound_idx = self.bind_ids.index(AUTO_BIND_SENTINEL)
            else:
                bound_idx = none_idx

            self.combo.current(bound_idx)
        if self.tab == TabId.LP:
            self._refresh_auto_lp_controls()

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
        self._persist_settings = (self.config_path is not None) or self.startup_dialogs

        loaded_cfg = load_app_settings(self.config_path) if self._persist_settings else {
            "language": get_language(),
            "ffmpeg_path": None,
            "default_bind_mode": "auto",
            "swap_lr": False,
            "allow_left_remove": False,
            "wav_labels": dict(DEFAULT_WAV_LABELS),
        }
        if self._persist_settings:
            set_language(str(loaded_cfg.get("language", "zh_CN")))

        self.chart: Optional[BmsChart] = None
        self.model = AssignmentModel(default_bind_mode=str(loaded_cfg.get("default_bind_mode", "auto")))
        self.wav_labels: Dict[str, str] = dict(loaded_cfg.get("wav_labels", DEFAULT_WAV_LABELS))
        self.allow_left_remove: bool = bool(loaded_cfg.get("allow_left_remove", False))
        self.ffmpeg_explicit: Optional[str] = loaded_cfg.get("ffmpeg_path")
        self.ffmpeg_path: Optional[str] = None
        self.last_output_dir: Optional[Path] = None
        self._busy = False
        self._left_ids: List[str] = []
        self._left_id_set: set[str] = set()
        self._active_tab: TabId = TabId.LC
        self._last_focus_side: str = "left"
        self._arrow_previewed_iid: Optional[str] = None
        self._queue: "queue.Queue[tuple]" = queue.Queue()
        self._ffmpeg_warned = False

        self._undo_stack: List[Dict[str, Any]] = []
        self._redo_stack: List[Dict[str, Any]] = []
        self._clean_snapshot: Dict[str, Any] = self.model.snapshot()
        self.dirty: bool = False

        self._icon_image: Optional[tk.PhotoImage] = None
        self._tkdnd_ready: bool = False
        self._last_guess_dialog: Optional[tk.Toplevel] = None

        attach_tk_crash_handler(root, show_dialog=self.startup_dialogs)
        self._update_window_title()
        self._apply_window_icon()
        self._setup_fonts_and_geometry()

        self.swap_lr_var = tk.BooleanVar(value=False)
        self.model.swap_lr = False
        self.player = PreviewPlayer(
            lambda: self.ffmpeg_path,
            on_status=lambda msg: self._queue.put(("preview_status", msg)),
            swap_lr_getter=lambda: bool(self.swap_lr_var.get()),
        )

        self._build_ui()
        self._bind_hotkeys()
        self.detect_ffmpeg()
        self._setup_drag_drop()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._poll_id: Optional[str] = root.after(80, self._poll_queue)

        if initial_path:
            root.after(150, lambda: self.load_bms(initial_path))

    # ------------------------------------------------------------------ setup
    def _update_window_title(self) -> None:
        if self.chart is not None:
            self.root.title(f"{APP_NAME} {APP_VERSION} [{self.chart.source_path}]")
        else:
            self.root.title(f"{APP_NAME} {APP_VERSION}")

    def _apply_window_icon(self) -> None:
        search_dirs: List[Path] = [
            Path(sys.executable).resolve().parent,
            Path(__file__).resolve().parent.parent,
        ]
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            search_dirs.append(Path(meipass))

        ico_candidates: List[Path] = []
        png_candidates: List[Path] = []
        for base in search_dirs:
            ico_candidates.extend([base / "assets" / "icon.ico", base / "icon.ico"])
            png_candidates.extend([base / "assets" / "icon.png", base / "icon.png"])

        for ico in ico_candidates:
            if ico.is_file():
                try:
                    self.root.iconbitmap(default=str(ico))
                    return
                except Exception:
                    pass
        for png in png_candidates:
            if png.is_file():
                try:
                    img = tk.PhotoImage(file=str(png))
                    self.root.iconphoto(True, img)
                    self._icon_image = img
                    return
                except Exception:
                    pass

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
        self._dpi_scale = scale
        width = min(int(1180 * scale), self.root.winfo_screenwidth() - 40)
        height = min(int(780 * scale), self.root.winfo_screenheight() - 80)
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(min(int(960 * scale), width), min(int(640 * scale), height))

    def _setup_drag_drop(self) -> None:
        """Register native file drag-and-drop via tkinterdnd2 (tkdnd C extension)."""
        self._tkdnd_ready = False
        try:
            from tkinterdnd2 import DND_FILES, TkinterDnD

            if not isinstance(self.root, TkinterDnD.DnDWrapper):
                self.root.__class__ = type(
                    "TkWithDnD", (self.root.__class__, TkinterDnD.DnDWrapper), {}
                )
            self.root.TkdndVersion = TkinterDnD._require(self.root)
            self.root.drop_target_register(DND_FILES)
            self.root.dnd_bind("<<Drop>>", self._on_tkdnd_drop)
            self._tkdnd_ready = True
        except Exception:
            self._tkdnd_ready = False

    def _split_dnd_paths(self, raw_data: Any) -> List[str]:
        """Parse tkdnd drop event data into individual file paths (handling spaces & CJK)."""
        if not raw_data:
            return []
        text = str(raw_data).strip()
        if not text:
            return []
        try:
            parts = self.root.tk.splitlist(text)
            return [str(p).strip() for p in parts if str(p).strip()]
        except Exception:
            return [text]

    def _on_tkdnd_drop(self, event: Any) -> Any:
        paths = self._split_dnd_paths(getattr(event, "data", ""))
        if paths:
            self.root.after(0, lambda dropped=paths: self._handle_dropped_files(dropped))
        return getattr(event, "action", None)

    def _handle_dropped_files(self, paths: Sequence[str]) -> None:
        if not paths or self._busy:
            return
        candidate = Path(paths[0])
        if candidate.is_dir() or candidate.suffix.lower() not in (".bms", ".bme", ".bml", ".pms"):
            messagebox.showwarning(
                tr("dialog_drop_invalid_title"),
                tr("dialog_drop_invalid_body", name=candidate.name or str(candidate)),
                parent=self.root,
            )
            return
        self.load_bms(candidate)

    def _bind_hotkeys(self) -> None:
        for seq in ("<Control-z>", "<Control-Z>"):
            self.root.bind_all(seq, self._on_ctrl_z)
        for seq in ("<Control-y>", "<Control-Y>", "<Control-Shift-z>", "<Control-Shift-Z>"):
            self.root.bind_all(seq, self._on_ctrl_y)

    @staticmethod
    def _is_text_input_focused(widget: Optional[tk.Misc]) -> bool:
        return isinstance(widget, (tk.Entry, ttk.Entry, ttk.Combobox, tk.Text, tk.Spinbox))

    def _on_ctrl_z(self, _event: Any = None) -> Optional[str]:
        try:
            focus = self.root.focus_get()
        except Exception:
            focus = None
        if self._is_text_input_focused(focus):
            return None
        if self.on_undo():
            return "break"
        return None

    def _on_ctrl_y(self, _event: Any = None) -> Optional[str]:
        try:
            focus = self.root.focus_get()
        except Exception:
            focus = None
        if self._is_text_input_focused(focus):
            return None
        if self.on_redo():
            return "break"
        return None

    def _build_ui(self) -> None:
        root = self.root
        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)

        # --- slim top bar: open button | path/hint label | [missing ffmpeg badge] | config button ---
        top = ttk.Frame(root, padding=(8, 8, 8, 2))
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(1, weight=1)
        self.open_btn = ttk.Button(top, text=tr("open_bms_btn"), command=self.on_choose_file)
        self.open_btn.grid(row=0, column=0, sticky="w", ipadx=12, ipady=4)

        self.path_var = tk.StringVar(value=tr("no_chart_selected"))
        self.path_label = ttk.Label(top, textvariable=self.path_var)
        self.path_label.grid(row=0, column=1, sticky="ew", padx=10)

        self.ffmpeg_warn_btn = tk.Button(
            top,
            text=tr("ffmpeg_warn_badge"),
            fg=ERROR_RED,
            bg="#fff3cd",
            activeforeground=ERROR_RED,
            activebackground="#ffe69c",
            relief="groove",
            borderwidth=1,
            padx=8,
            pady=3,
            cursor="hand2",
            command=self.open_settings_dialog,
        )
        self.ffmpeg_warn_btn.grid(row=0, column=2, sticky="e", padx=(0, 6))
        self.ffmpeg_warn_btn.grid_remove()

        self.config_btn = ttk.Button(
            top, text=tr("config_btn"), command=self.open_settings_dialog
        )
        self.config_btn.grid(row=0, column=3, sticky="e", ipadx=8, ipady=3)

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

        # 5-column ttk.Treeview: ID | Filename | Count | Target | L/R
        self.left_tree = ttk.Treeview(
            self.left_frame,
            columns=("id", "filename", "count", "target", "lr"),
            show="headings",
            selectmode="extended",
        )
        s = self._dpi_scale
        self.left_tree.column("id", width=int(56 * s), minwidth=44, stretch=False, anchor="w")
        self.left_tree.column("filename", width=int(200 * s), minwidth=100, stretch=True, anchor="w")
        self.left_tree.column("count", width=int(52 * s), minwidth=42, stretch=False, anchor="e")
        self.left_tree.column("target", width=int(74 * s), minwidth=54, stretch=False, anchor="w")
        self.left_tree.column("lr", width=int(42 * s), minwidth=36, stretch=False, anchor="center")
        self._apply_tree_headings()

        self.left_tree.tag_configure("assigned", foreground=GRAY_FOREGROUND)
        self.left_tree.tag_configure("missing", foreground=ERROR_RED)

        scroll = ttk.Scrollbar(self.left_frame, orient="vertical", command=self.left_tree.yview)
        xscroll = ttk.Scrollbar(self.left_frame, orient="horizontal", command=self.left_tree.xview)
        self.left_tree.configure(yscrollcommand=scroll.set, xscrollcommand=xscroll.set)
        self.left_tree.grid(row=1, column=0, sticky="nsew")
        scroll.grid(row=1, column=1, sticky="ns")
        xscroll.grid(row=2, column=0, sticky="ew")

        self.left_tree.bind("<FocusIn>", lambda _e: self._set_focus_side("left"))
        self.left_tree.bind("<Button-1>", self._on_left_click)
        self.left_tree.bind("<KeyPress-Up>", self._on_left_arrow_press)
        self.left_tree.bind("<KeyPress-Down>", self._on_left_arrow_press)
        self.left_tree.bind("<KeyRelease-Up>", self._on_left_arrow_release)
        self.left_tree.bind("<KeyRelease-Down>", self._on_left_arrow_release)
        self.left_tree.bind(
            "<Double-Button-1>",
            lambda _e: (None if self.auto_preview_var.get() else self.preview_from_left(), "break")[1],
        )
        self.left_tree.bind("<space>", lambda _e: (self.preview_from_left(), "break")[1])

        # Bottom-left 2-row controls
        pv_container = ttk.Frame(self.left_frame)
        pv_container.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(6, 0))

        pv_row1 = ttk.Frame(pv_container)
        pv_row1.pack(fill="x", anchor="w")
        self.play_btn = ttk.Button(pv_row1, text=tr("play_btn"), command=self.on_preview_button)
        self.play_btn.pack(side="left")
        self.stop_btn = ttk.Button(pv_row1, text=tr("stop_btn"), command=self.player.stop)
        self.stop_btn.pack(side="left", padx=(4, 0))
        self.auto_preview_var = tk.BooleanVar(value=True)
        self.auto_preview_chk = ttk.Checkbutton(
            pv_row1, text=tr("auto_preview_chk"), variable=self.auto_preview_var
        )
        self.auto_preview_chk.pack(side="left", padx=(8, 0))
        self._auto_preview_tip = _Tooltip(
            self.auto_preview_chk, lambda: tr("auto_preview_tooltip")
        )
        self.swap_lr_chk = ttk.Checkbutton(
            pv_row1,
            text=tr("swap_lr_chk"),
            variable=self.swap_lr_var,
            command=self._on_swap_lr_toggled,
        )
        self.swap_lr_chk.pack(side="left", padx=(8, 0))

        pv_row2 = ttk.Frame(pv_container)
        pv_row2.pack(fill="x", anchor="w", pady=(3, 0))
        self.left_count_var = tk.StringVar(value="")
        self.left_count_label = ttk.Label(pv_row2, textvariable=self.left_count_var, foreground="#444444")
        self.left_count_label.pack(side="left")

    def _apply_tree_headings(self) -> None:
        self.left_tree.heading("id", text=tr("col_id"), anchor="w")
        self.left_tree.heading("filename", text=tr("col_filename"), anchor="w")
        self.left_tree.heading("count", text=tr("col_count"), anchor="e")
        self.left_tree.heading("target", text=tr("col_target"), anchor="w")
        self.left_tree.heading("lr", text=tr("col_lr"), anchor="center")

    def _build_center(self, parent: ttk.Frame) -> None:
        center = ttk.Frame(parent, padding=(8, 0))
        center.grid(row=0, column=1, sticky="ns")
        center.rowconfigure(0, weight=1)
        center.rowconfigure(7, weight=1)
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
        self.swap_slice_lr_btn = ttk.Button(
            center, text=tr("swap_slice_lr_btn"), command=self.on_toggle_slice_lr
        )
        self.swap_slice_lr_btn.grid(row=3, column=0, sticky="ew", pady=4)
        self._swap_slice_lr_tip = _Tooltip(
            self.swap_slice_lr_btn, lambda: tr("swap_slice_lr_tooltip")
        )
        ttk.Separator(center, orient="horizontal").grid(row=4, column=0, sticky="ew", pady=8)
        self.guess_btn = ttk.Button(center, text=tr("guess_btn"), command=self.on_guess)
        self.guess_btn.grid(row=5, column=0, sticky="ew", pady=2)
        self.clear_btn = ttk.Button(center, text=tr("clear_all_btn"), command=self.on_clear_all)
        self.clear_btn.grid(row=6, column=0, sticky="ew", pady=2)

    def _build_right(self, parent: ttk.Frame) -> None:
        self.right_frame = ttk.LabelFrame(parent, text=tr("right_frame_title"), padding=6)
        self.right_frame.grid(row=0, column=2, sticky="nsew")
        self.right_frame.columnconfigure(0, weight=1)
        self.right_frame.rowconfigure(2, weight=1)

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
        self.export_btn.grid(row=0, column=3, rowspan=3, padx=(12, 0), ipadx=14, ipady=8)

        opts1 = ttk.Frame(self.export_frame)
        opts1.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        self.utf8_var = tk.BooleanVar(value=False)
        self.utf8_chk = ttk.Checkbutton(opts1, text=tr("utf8_chk"), variable=self.utf8_var)
        self.utf8_chk.pack(side="left")
        self.ogg_var = tk.BooleanVar(value=False)
        self.ogg_chk = ttk.Checkbutton(opts1, text=tr("transcode_ogg_chk"), variable=self.ogg_var)
        self.ogg_chk.pack(side="left", padx=12)
        self.dlevel_label = ttk.Label(opts1, text=tr("dlevel_label"))
        self.dlevel_label.pack(side="left", padx=(12, 2))
        self.dlevel_var = tk.StringVar(value="50")
        ttk.Entry(opts1, textvariable=self.dlevel_var, width=6).pack(side="left")

        opts2 = ttk.Frame(self.export_frame)
        opts2.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        self.image_mode_label = ttk.Label(opts2, text=tr("image_mode_label"))
        self.image_mode_label.pack(side="left")
        self._image_mode_key = IMAGE_MODE_KEEP
        self.image_mode_var = tk.StringVar()
        self.image_mode_combo = ttk.Combobox(
            opts2,
            textvariable=self.image_mode_var,
            state="readonly",
            width=36,
        )
        self.image_mode_combo.pack(side="left", padx=(4, 14))
        self.image_mode_combo.bind("<<ComboboxSelected>>", self._on_image_mode_selected)
        self._refresh_image_mode_combo()

        self.export_bga_var = tk.BooleanVar(value=False)
        self.export_bga_chk = ttk.Checkbutton(
            opts2, text=tr("export_bga_chk"), variable=self.export_bga_var
        )
        self.export_bga_chk.pack(side="left")

        self.progress = ttk.Progressbar(
            self.export_frame, mode="determinate", maximum=100, value=0
        )
        self.progress.grid(row=3, column=0, columnspan=4, sticky="ew", pady=(6, 0))
        self.open_out_btn = ttk.Button(
            self.export_frame,
            text=tr("open_output_btn"),
            command=self.open_output_folder,
            state="disabled",
        )
        self.open_out_btn.grid(row=0, column=4, rowspan=3, padx=(8, 0))

    def _image_mode_labels(self) -> List[str]:
        return [
            tr("image_mode_keep"),
            tr("image_mode_resize"),
            tr("image_mode_none"),
        ]

    def _refresh_image_mode_combo(self) -> None:
        labels = self._image_mode_labels()
        self.image_mode_combo.configure(values=labels)
        try:
            idx = _IMAGE_MODES_ORDER.index(self._image_mode_key)
        except ValueError:
            idx = 0
        self.image_mode_combo.current(idx)

    def _on_image_mode_selected(self, _event: Any = None) -> None:
        idx = self.image_mode_combo.current()
        if 0 <= idx < len(_IMAGE_MODES_ORDER):
            self._image_mode_key = _IMAGE_MODES_ORDER[idx]

    def selected_image_mode(self) -> str:
        return self._image_mode_key

    def _on_swap_lr_toggled(self) -> None:
        self.model.swap_lr = bool(self.swap_lr_var.get())
        self._update_dirty_flag()
        self.refresh_left()

    # ---------------------------------------------------- settings & i18n
    def change_language(self, lang_code: str) -> None:
        norm = set_language(lang_code)
        if self._persist_settings:
            save_app_settings({"language": norm}, self.config_path)
        self.apply_language()

    def apply_language(self) -> None:
        """Dynamically refresh all UI labels and texts in the current language."""
        self.open_btn.configure(text=tr("open_bms_btn"))
        if self.chart is None:
            self.path_var.set(tr("no_chart_selected"))
        self.config_btn.configure(text=tr("config_btn"))
        self.ffmpeg_warn_btn.configure(text=tr("ffmpeg_warn_badge"))
        self._update_ffmpeg_ui()

        self.health.configure(text=tr("health_frame_title"))
        self._render_health(self.chart)

        self.left_frame.configure(text=tr("left_frame_title"))
        self.search_label.configure(text=tr("search_label"))
        self.clear_search_btn.configure(text=tr("clear_search_btn"))
        self.only_used_chk.configure(text=tr("only_used_chk"))
        self._apply_tree_headings()
        self.play_btn.configure(text=tr("play_btn"))
        self.stop_btn.configure(text=tr("stop_btn"))
        self.auto_preview_chk.configure(text=tr("auto_preview_chk"))
        self.swap_lr_chk.configure(text=tr("swap_lr_chk"))

        self.add_btn.configure(text=tr("add_btn"))
        self.remove_btn.configure(text=tr("remove_btn"))
        self.swap_slice_lr_btn.configure(text=tr("swap_slice_lr_btn"))
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
        self.image_mode_label.configure(text=tr("image_mode_label"))
        self._refresh_image_mode_combo()
        self.export_bga_chk.configure(text=tr("export_bga_chk"))

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

    def _validate_ffmpeg_candidate(self, path_or_cmd: Optional[str]) -> str:
        resolved = find_ffmpeg(path_or_cmd)
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
        return resolved

    def open_settings_dialog(self) -> None:
        """Open unified Settings modal dialog (language, ffmpeg, auto-bind, #WAV labels)."""
        top = tk.Toplevel(self.root)
        top.title(tr("settings_title"))
        top.transient(self.root)
        top.grab_set()
        top.resizable(False, False)

        container = ttk.Frame(top, padding=12)
        container.pack(fill="both", expand=True)

        # 1. Language
        lang_sec = ttk.LabelFrame(container, text=tr("settings_general_section"), padding=8)
        lang_sec.pack(fill="x", pady=(0, 8))
        lang_var = tk.StringVar(value=LANGUAGE_LABELS.get(get_language(), "简体中文"))
        ttk.Label(lang_sec, text=tr("lang_label")).pack(side="left", padx=(0, 6))
        lang_combo = ttk.Combobox(
            lang_sec,
            textvariable=lang_var,
            state="readonly",
            width=16,
            values=[label for _code, label in SUPPORTED_LANGUAGES],
        )
        lang_combo.pack(side="left")

        # 2. ffmpeg status & locator + winget guide
        ff_sec = ttk.LabelFrame(container, text=tr("settings_ffmpeg_section"), padding=8)
        ff_sec.pack(fill="x", pady=(0, 8))
        draft_explicit: List[Optional[str]] = [self.ffmpeg_explicit]
        draft_detected: List[Optional[str]] = [self.ffmpeg_path]

        ff_status_var = tk.StringVar()
        ff_status_lbl = ttk.Label(ff_sec, textvariable=ff_status_var, wraplength=540, justify="left")
        ff_status_lbl.pack(anchor="w")

        def _refresh_ff_dialog() -> None:
            if draft_detected[0]:
                ff_status_var.set(tr("ffmpeg_ok", path=draft_detected[0]))
                ff_status_lbl.configure(foreground=OK_GREEN)
            else:
                ff_status_var.set(tr("ffmpeg_missing_short"))
                ff_status_lbl.configure(foreground="#555555")

        _refresh_ff_dialog()

        ff_btns = ttk.Frame(ff_sec)
        ff_btns.pack(fill="x", pady=(6, 4))

        def _dlg_pick_ffmpeg() -> None:
            path = filedialog.askopenfilename(
                parent=top,
                title=tr("pick_ffmpeg_btn"),
                filetypes=[("ffmpeg.exe", "*.exe"), (tr("filetypes_all"), "*.*")],
            )
            if not path:
                return
            try:
                resolved = self._validate_ffmpeg_candidate(path)
            except Exception as exc:
                messagebox.showerror(
                    tr("dialog_ffmpeg_invalid_title"),
                    tr("dialog_ffmpeg_invalid_body", exc=exc),
                    parent=top,
                )
                return
            draft_explicit[0] = path
            draft_detected[0] = resolved
            _refresh_ff_dialog()

        def _dlg_redetect_ffmpeg() -> None:
            draft_explicit[0] = None
            try:
                draft_detected[0] = find_ffmpeg(None)
            except FfmpegNotFoundError:
                draft_detected[0] = None
            _refresh_ff_dialog()

        ttk.Button(ff_btns, text=tr("pick_ffmpeg_btn"), command=_dlg_pick_ffmpeg).pack(side="left")
        ttk.Button(
            ff_btns, text=tr("settings_ffmpeg_redetect_btn"), command=_dlg_redetect_ffmpeg
        ).pack(side="left", padx=6)

        ttk.Label(ff_sec, text=tr("settings_ffmpeg_hint"), foreground="#555555").pack(
            anchor="w", pady=(4, 2)
        )
        cmd_row = ttk.Frame(ff_sec)
        cmd_row.pack(fill="x")
        cmd_var = tk.StringVar(value=tr("settings_winget_cmd"))
        cmd_entry = ttk.Entry(cmd_row, textvariable=cmd_var, state="readonly")
        cmd_entry.pack(side="left", fill="x", expand=True)

        def _copy_winget() -> None:
            try:
                top.clipboard_clear()
                top.clipboard_append(cmd_var.get())
                self.set_status(tr("settings_copied_cmd"))
            except Exception:
                pass

        ttk.Button(cmd_row, text=tr("settings_copy_cmd_btn"), command=_copy_winget).pack(
            side="left", padx=(6, 0)
        )

        # 3. Default bound chip strategy
        bind_sec = ttk.LabelFrame(container, text=tr("settings_bind_section"), padding=8)
        bind_sec.pack(fill="x", pady=(0, 8))
        bind_mode_var = tk.StringVar(value=self.model.default_bind_mode)
        ttk.Radiobutton(
            bind_sec, text=tr("settings_bind_auto"), value="auto", variable=bind_mode_var
        ).pack(anchor="w")
        ttk.Radiobutton(
            bind_sec, text=tr("settings_bind_manual"), value="manual", variable=bind_mode_var
        ).pack(anchor="w", pady=(2, 0))

        # 4. #WAVxx inline labels editor
        lbl_sec = ttk.LabelFrame(container, text=tr("settings_labels_section"), padding=8)
        lbl_sec.pack(fill="x", pady=(0, 10))
        label_vars: Dict[str, tk.StringVar] = {}
        cols_per_row = 4
        for idx, key in enumerate(WAV_LABEL_KEYS):
            r, c = divmod(idx, cols_per_row)
            display_name = "BGM" if key == "BGM" else TAB_SPEC_MAP[TabId(key)].label
            ttk.Label(lbl_sec, text=f"{display_name}:", width=9, anchor="e").grid(
                row=r, column=c * 2, sticky="e", padx=(2, 2), pady=2
            )
            var = tk.StringVar(value=self.wav_labels.get(key, DEFAULT_WAV_LABELS[key]))
            label_vars[key] = var
            ttk.Entry(lbl_sec, textvariable=var, width=9).grid(
                row=r, column=c * 2 + 1, sticky="w", padx=(0, 8), pady=2
            )

        def _reset_labels() -> None:
            for key, default_val in DEFAULT_WAV_LABELS.items():
                if key in label_vars:
                    label_vars[key].set(default_val)

        ttk.Button(lbl_sec, text=tr("settings_reset_labels_btn"), command=_reset_labels).grid(
            row=(len(WAV_LABEL_KEYS) + cols_per_row - 1) // cols_per_row,
            column=0,
            columnspan=cols_per_row * 2,
            sticky="e",
            pady=(6, 0),
        )

        allow_left_remove_var = tk.BooleanVar(value=self.allow_left_remove)
        ttk.Checkbutton(
            container,
            text=tr("settings_allow_left_remove_chk"),
            variable=allow_left_remove_var,
        ).pack(anchor="w", pady=(0, 8))

        # Bottom Save / Cancel
        btns = ttk.Frame(container)
        btns.pack(fill="x")

        def _save_and_close() -> None:
            chosen_label = lang_var.get()
            new_lang = LABEL_TO_LANGUAGE.get(chosen_label, get_language())
            new_bind_mode = bind_mode_var.get()
            new_labels = {
                k: sanitize_wav_label(label_vars[k].get())
                for k in WAV_LABEL_KEYS
            }

            self.ffmpeg_explicit = draft_explicit[0]
            self.detect_ffmpeg()
            self.wav_labels = new_labels
            self.allow_left_remove = bool(allow_left_remove_var.get())

            prev_mode = self.model.default_bind_mode
            self.model.set_default_bind_mode(new_bind_mode)

            if self._persist_settings:
                save_app_settings(
                    {
                        "language": new_lang,
                        "ffmpeg_path": self.ffmpeg_explicit,
                        "default_bind_mode": self.model.default_bind_mode,
                        "allow_left_remove": self.allow_left_remove,
                        "wav_labels": new_labels,
                    },
                    self.config_path,
                )

            if new_lang != get_language():
                self.change_language(new_lang)
            elif prev_mode != self.model.default_bind_mode:
                self.refresh_all()

            if self.ffmpeg_path:
                self.set_status(tr("status_ffmpeg_enabled", path=self.ffmpeg_path))
            top.destroy()

        ttk.Button(
            btns, text=tr("settings_open_logs_btn"), command=self.open_logs_folder
        ).pack(side="left")
        ttk.Button(btns, text=tr("settings_cancel_btn"), command=top.destroy).pack(side="right")
        ttk.Button(btns, text=tr("settings_save_btn"), command=_save_and_close).pack(
            side="right", padx=(0, 8)
        )

    def open_logs_folder(self) -> None:
        log_dir = get_crash_log_dir()
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        self.open_folder(log_dir)
        self.set_status(tr("status_logs_opened", path=log_dir))

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
        before = self.model.snapshot()
        enabled = bool(self.separate_lbd_var.get())
        self.model.set_separate_lbd(enabled)
        if not enabled and self._active_tab == TabId.LBD:
            self._active_tab = TabId.LP
        self._commit_undo(before)
        self._rebuild_tab_grid()
        self.select_tab(self._active_tab)
        self.refresh_left()
        self.refresh_tab(TabId.LBD)

    # ------------------------------------------------------------- undo/redo
    def _update_dirty_flag(self) -> None:
        if self.chart is None:
            self.dirty = False
        else:
            self.dirty = self.model.snapshot() != self._clean_snapshot

    def _commit_undo(self, before_snapshot: Dict[str, Any]) -> None:
        after_snapshot = self.model.snapshot()
        if after_snapshot == before_snapshot:
            return
        self._undo_stack.append(before_snapshot)
        if len(self._undo_stack) > MAX_UNDO_HISTORY:
            del self._undo_stack[0]
        self._redo_stack.clear()
        self._update_dirty_flag()

    def on_undo(self) -> bool:
        if self._busy or not self._undo_stack:
            return False
        self._redo_stack.append(self.model.snapshot())
        snap = self._undo_stack.pop()
        self.model.restore(snap)
        if self.separate_lbd_var.get() != self.model.separate_lbd:
            self.separate_lbd_var.set(self.model.separate_lbd)
            if not self.model.separate_lbd and self._active_tab == TabId.LBD:
                self._active_tab = TabId.LP
            self._rebuild_tab_grid()
            self.select_tab(self._active_tab)
        if self.swap_lr_var.get() != self.model.swap_lr:
            self.swap_lr_var.set(self.model.swap_lr)
        self.refresh_all()
        self._update_dirty_flag()
        self.set_status(tr("status_undone"))
        return True

    def on_redo(self) -> bool:
        if self._busy or not self._redo_stack:
            return False
        self._undo_stack.append(self.model.snapshot())
        snap = self._redo_stack.pop()
        self.model.restore(snap)
        if self.separate_lbd_var.get() != self.model.separate_lbd:
            self.separate_lbd_var.set(self.model.separate_lbd)
            if not self.model.separate_lbd and self._active_tab == TabId.LBD:
                self._active_tab = TabId.LP
            self._rebuild_tab_grid()
            self.select_tab(self._active_tab)
        if self.swap_lr_var.get() != self.model.swap_lr:
            self.swap_lr_var.set(self.model.swap_lr)
        self.refresh_all()
        self._update_dirty_flag()
        self.set_status(tr("status_redone"))
        return True

    # ----------------------------------------------------------------- helpers
    def _set_focus_side(self, side: str) -> None:
        self._last_focus_side = side

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
        self.ffmpeg_warn_btn.grid_remove()
        self.ogg_chk.configure(state="normal")

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
            self._validate_ffmpeg_candidate(path)
        except Exception as exc:
            messagebox.showerror(
                tr("dialog_ffmpeg_invalid_title"),
                tr("dialog_ffmpeg_invalid_body", exc=exc),
                parent=self.root,
            )
            return
        self.ffmpeg_explicit = path
        if self._persist_settings:
            save_app_settings({"ffmpeg_path": path}, self.config_path)
        self.detect_ffmpeg()
        self.set_status(tr("status_ffmpeg_enabled", path=self.ffmpeg_path))

    # --------------------------------------------------------------- load chart
    def on_choose_file(self) -> None:
        filetypes = [
            (tr("filetypes_bms"), "*.bms *.bme *.bml *.pms"),
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
        self.swap_lr_var.set(False)
        self.model.swap_lr = False
        self.model.swapped_wav_ids.clear()
        self._undo_stack.clear()
        self._redo_stack.clear()
        self._clean_snapshot = self.model.snapshot()
        self.dirty = False

        self._update_window_title()
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
        keep_sel = set(self.left_tree.selection())
        keep_focus = self.left_tree.focus()
        yview = self.left_tree.yview()[0]
        children = self.left_tree.get_children("")
        if children:
            self.left_tree.delete(*children)
        self._left_ids = []
        self._left_id_set = set()
        if self.chart is None:
            self.left_count_var.set("")
            return
        owner = {wid: t for t, ids in self.model.tab_wavs.items() for wid in ids}
        self._left_ids = filter_wav_ids(
            self.chart, self.search_var.get(), self.only_used_var.get()
        )
        self._left_id_set = set(self._left_ids)
        rep_to_group, _wid_to_rep = wav_physical_groups(self.chart)
        all_groups = list(rep_to_group.keys())
        assigned_groups = 0
        for rep_id in all_groups:
            if owner.get(rep_id) is not None:
                assigned_groups += 1

        to_select: List[str] = []
        global_swap = bool(self.swap_lr_var.get())
        for wid in self._left_ids:
            tab = owner.get(wid)
            entry = self.chart.wavs[wid]
            values = format_wav_tree_values(
                entry,
                tab,
                chart=self.chart,
                group_ids=rep_to_group.get(wid),
                effective_swap_lr=self.model.is_effective_swap_lr(
                    wid, global_swap_lr=global_swap
                ),
            )
            if tab is not None:
                tags: tuple[str, ...] = ("assigned",)
            elif not entry.exists:
                tags = ("missing",)
            else:
                tags = ()
            self.left_tree.insert("", tk.END, iid=wid, values=values, tags=tags)
            if wid in keep_sel:
                to_select.append(wid)
        if to_select:
            self.left_tree.selection_set(tuple(to_select))
        if keep_focus and keep_focus in self._left_id_set:
            self.left_tree.focus(keep_focus)
        self.left_tree.yview_moveto(yview)
        self.left_count_var.set(
            tr(
                "left_count_summary",
                shown=len(self._left_ids),
                total=len(all_groups),
                assigned=assigned_groups,
            )
        )

    def selected_left_ids(self) -> List[str]:
        return [iid for iid in self.left_tree.selection() if iid in self._left_id_set]

    # ----------------------------------------------------------------- shuttle
    def on_add(self) -> None:
        if self.chart is None:
            return
        ids = self.selected_left_ids()
        if not ids:
            self.set_status(tr("status_select_left_first"))
            return
        tab = self.active_tab()
        before = self.model.snapshot()
        added, skipped = self.model.assign(tab, ids, chart=self.chart)
        self._commit_undo(before)
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
        right_ids = self.panels[tab].selected_ids()
        left_ids = self.selected_left_ids() if self.allow_left_remove else []

        if self.allow_left_remove:
            use_right = (self._last_focus_side == "right" and bool(right_ids)) or (
                not left_ids and bool(right_ids)
            )
            if use_right:
                before = self.model.snapshot()
                removed = self.model.remove(tab, right_ids, chart=self.chart)
                self._commit_undo(before)
                self.refresh_left()
                self.refresh_tab(tab)
                self.set_status(
                    tr("status_removed_from_tab", removed=len(removed), tab=tab_label(tab))
                )
                return
            if left_ids:
                before = self.model.snapshot()
                removed = self.model.unassign_wav_ids(left_ids, chart=self.chart)
                if removed:
                    self._commit_undo(before)
                    self.refresh_all()
                self.set_status(
                    tr("status_unassigned_from_left", removed=len(removed))
                )
                return

        if not right_ids:
            self.set_status(tr("status_select_right_first"))
            return
        before = self.model.snapshot()
        removed = self.model.remove(tab, right_ids, chart=self.chart)
        self._commit_undo(before)
        self.refresh_left()
        self.refresh_tab(tab)
        self.set_status(
            tr("status_removed_from_tab", removed=len(removed), tab=tab_label(tab))
        )

    def on_toggle_slice_lr(self) -> None:
        """Toggle per-slice L/R swap override for selected slice(s) in left or right list."""
        if self.chart is None:
            return
        left_ids = self.selected_left_ids()
        right_ids = self.panels[self.active_tab()].selected_ids()
        if self._last_focus_side == "right" and right_ids:
            target_ids = right_ids
        elif left_ids:
            target_ids = left_ids
        elif right_ids:
            target_ids = right_ids
        else:
            self.set_status(tr("status_select_slice_first"))
            return
        before = self.model.snapshot()
        toggled = self.model.toggle_slice_swap_lr(target_ids, chart=self.chart)
        if not toggled:
            return
        self._commit_undo(before)
        self.refresh_left()
        self.set_status(tr("status_slice_lr_toggled", count=len(target_ids)))
        if len(target_ids) == 1 and self.auto_preview_var.get():
            self._play_wav_id(target_ids[0])

    def on_clear_all(self) -> None:
        if self.chart is None or (not self.model.has_assignments and not self.model.swapped_wav_ids):
            return
        if not messagebox.askyesno(
            tr("dialog_clear_title"), tr("dialog_clear_body"), parent=self.root
        ):
            return
        before = self.model.snapshot()
        self.model.clear()
        self._commit_undo(before)
        self.refresh_all()
        self.set_status(tr("status_cleared_all"))

    def on_guess(self) -> None:
        """Manual helper only — never invoked automatically on import."""
        if self.chart is None:
            return
        assigned = self.model.assigned_ids
        rep_ids = filter_wav_ids(self.chart, "", False)
        candidates = [
            (wid, self.chart.wavs[wid].resolved_name)
            for wid in rep_ids
            if wid not in assigned
        ]
        audio_paths = {
            wid: self.chart.wavs[wid].resolved_path
            for wid, _name in candidates
            if wid in self.chart.wavs
        }
        global_swap = bool(self.swap_lr_var.get())
        effective_swaps = {
            wid: self.model.is_effective_swap_lr(wid, global_swap_lr=global_swap)
            for wid, _name in candidates
        }
        guesses = guess_assignments(
            candidates,
            audio_paths=audio_paths,
            effective_swaps=effective_swaps,
        )
        if not self.separate_lbd_var.get() and TabId.LBD in guesses:
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
        before = self.model.snapshot()
        for tab, ids in guesses.items():
            self.model.assign(tab, ids, chart=self.chart)
        self._commit_undo(before)
        self.refresh_all()
        self.set_status(tr("status_guessed", total=total))
        self.show_guess_complete_dialog(total)

    def show_guess_complete_dialog(self, total: int) -> tk.Toplevel:
        """Show completion modal after Auto-Guess with a prominent red review warning."""
        dlg = tk.Toplevel(self.root)
        dlg.title(tr("dialog_guess_done_title"))
        dlg.transient(self.root)
        dlg.resizable(False, False)
        self._last_guess_dialog = dlg

        body = ttk.Frame(dlg, padding=16)
        body.pack(fill="both", expand=True)

        ttk.Label(
            body,
            text=tr("dialog_guess_done_summary", total=total),
            font=self.normal_font,
            wraplength=440,
            justify="left",
        ).pack(anchor="w", pady=(0, 10))

        tk.Label(
            body,
            text=tr("dialog_guess_done_warning"),
            fg="#C62828",
            font=self.bold_font,
            wraplength=440,
            justify="left",
        ).pack(anchor="w", pady=(0, 14))

        btn_frame = ttk.Frame(body)
        btn_frame.pack(fill="x")
        ok_btn = ttk.Button(
            btn_frame,
            text=tr("dialog_close_btn"),
            command=dlg.destroy,
        )
        ok_btn.pack(side="right")

        dlg.bind("<Return>", lambda _e: dlg.destroy())
        dlg.bind("<Escape>", lambda _e: dlg.destroy())

        try:
            dlg.update_idletasks()
            rx = self.root.winfo_rootx()
            ry = self.root.winfo_rooty()
            rw = self.root.winfo_width()
            rh = self.root.winfo_height()
            dw = dlg.winfo_reqwidth()
            dh = dlg.winfo_reqheight()
            if rw > 1 and rh > 1:
                x = max(0, rx + (rw - dw) // 2)
                y = max(0, ry + (rh - dh) // 2)
                dlg.geometry(f"+{x}+{y}")
        except Exception:
            pass

        if self.startup_dialogs:
            try:
                dlg.grab_set()
                ok_btn.focus_set()
                self.root.wait_window(dlg)
            except Exception:
                pass
        return dlg

    # ----------------------------------------------------------------- preview
    def _play_wav_id(self, wav_id: Optional[str]) -> None:
        if not wav_id or self.chart is None:
            self.set_status(tr("status_select_slice_first"))
            return
        entry = self.chart.wavs.get(wav_id)
        if entry is None or not entry.exists or entry.resolved_path is None:
            self.set_status(tr("status_file_missing_no_preview", wav_id=wav_id))
            return
        if self.model.is_slice_swap_override(wav_id):
            eff_swap = self.model.is_effective_swap_lr(
                wav_id, global_swap_lr=bool(self.swap_lr_var.get())
            )
            self.player.play(entry.resolved_path, swap_lr=eff_swap)
        else:
            self.player.play(entry.resolved_path)

    def _on_left_click_index(self, idx: int) -> None:
        if not self.auto_preview_var.get() or self.chart is None:
            return
        if 0 <= idx < len(self._left_ids):
            self._play_wav_id(self._left_ids[idx])

    def _on_left_click(self, event: Any) -> Optional[str]:
        self._set_focus_side("left")
        region = self.left_tree.identify_region(event.x, event.y)
        row_iid = self.left_tree.identify_row(event.y)
        if not row_iid and region not in ("heading", "separator"):
            sel = self.left_tree.selection()
            if sel:
                self.left_tree.selection_remove(*sel)
            return "break"
        if (
            self._left_ids
            and row_iid
            and row_iid in self._left_id_set
            and self.auto_preview_var.get()
            and self.chart is not None
        ):
            self._play_wav_id(row_iid)
        return None

    def _on_left_arrow_press(self, _event: Any = None) -> None:
        self._set_focus_side("left")
        if not self.auto_preview_var.get() or self.chart is None or not self._left_ids:
            return
        try:
            self.root.after_idle(self._on_left_arrow_after_keypress)
        except Exception:
            pass

    def _on_left_arrow_after_keypress(self) -> None:
        if not self.auto_preview_var.get() or self.chart is None or not self._left_ids:
            return
        focus_iid = self.left_tree.focus()
        if focus_iid and focus_iid in self._left_id_set:
            self._arrow_previewed_iid = focus_iid
            self._play_wav_id(focus_iid)

    def _on_left_arrow_release(self, _event: Any = None) -> None:
        if not self.auto_preview_var.get() or self.chart is None or not self._left_ids:
            return
        focus_iid = self.left_tree.focus()
        if self._arrow_previewed_iid == focus_iid:
            self._arrow_previewed_iid = None
            return
        self._arrow_previewed_iid = None
        if focus_iid and focus_iid in self._left_id_set:
            self._play_wav_id(focus_iid)

    def _on_left_arrow_key(self, _event: Any = None) -> None:
        if not self.auto_preview_var.get() or self.chart is None or not self._left_ids:
            return
        focus_iid = self.left_tree.focus()
        if focus_iid and focus_iid in self._left_id_set:
            self._play_wav_id(focus_iid)

    def preview_from_left(self) -> bool:
        self._set_focus_side("left")
        if not self._left_ids:
            self._play_wav_id(None)
            return True
        sel = self.selected_left_ids()
        focus_iid = self.left_tree.focus()
        if focus_iid and focus_iid in sel:
            self._play_wav_id(focus_iid)
        else:
            self._play_wav_id(sel[0] if sel else None)
        return True

    def preview_from_tab(self, tab: TabId) -> bool:
        self._set_focus_side("right")
        ids = self.panels[tab].selected_ids()
        if not ids:
            panel = self.panels[tab]
            try:
                active_idx = int(panel.listbox.index(tk.ACTIVE))
            except Exception:
                active_idx = -1
            if 0 <= active_idx < len(panel.ids):
                ids = [panel.ids[active_idx]]
        self._play_wav_id(ids[0] if ids else None)
        return True

    def on_preview_button(self) -> None:
        left_ids = self.selected_left_ids()
        right_ids = (
            self.panels[self.active_tab()].selected_ids() if self.chart else []
        )
        if self._last_focus_side == "right" and right_ids:
            self._play_wav_id(right_ids[0])
            return
        if left_ids:
            self._play_wav_id(left_ids[0])
            return
        self._play_wav_id(right_ids[0] if right_ids else None)

    # --------------------------------------------------------- mapping JSON I/O
    def on_save_mapping(self) -> None:
        lp_panel = self.panels.get(TabId.LP)
        if lp_panel is not None:
            lp_panel.sync_auto_lp_to_model(push_undo=False)
        self.model.swap_lr = bool(self.swap_lr_var.get())
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
        initial_dir, initial_file = suggest_load_mapping_location(
            self.chart, self.last_output_dir, self.out_var.get()
        )
        dlg_kw: Dict[str, Any] = {
            "parent": self.root,
            "title": tr("load_map_btn"),
            "filetypes": [("JSON", "*.json")],
        }
        if initial_dir is not None:
            dlg_kw["initialdir"] = str(initial_dir)
        if initial_file is not None:
            dlg_kw["initialfile"] = initial_file
        path = filedialog.askopenfilename(**dlg_kw)
        if not path:
            return
        before = self.model.snapshot()
        try:
            notes = load_mapping_json(self.model, path, self.chart)
        except Exception as exc:
            messagebox.showerror(
                tr("dialog_load_failed_title"),
                f"{type(exc).__name__}: {exc}",
                parent=self.root,
            )
            return
        self._commit_undo(before)
        if self.model.separate_lbd != self.separate_lbd_var.get():
            self.separate_lbd_var.set(self.model.separate_lbd)
            self._rebuild_tab_grid()
        if self.model.swap_lr != self.swap_lr_var.get():
            self.swap_lr_var.set(self.model.swap_lr)
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
        if self.selected_image_mode() == IMAGE_MODE_RESIZE and self.ffmpeg_path is None:
            if not messagebox.askyesno(
                tr("dialog_ffmpeg_missing_title"),
                tr("dialog_export_resize_no_ffmpeg_body"),
                parent=self.root,
            ):
                return

        lp_panel = self.panels.get(TabId.LP)
        if lp_panel is not None:
            lp_panel.sync_auto_lp_to_model(push_undo=False)
        self.model.swap_lr = bool(self.swap_lr_var.get())

        out_dir = Path(out_text)
        options = ConversionOptions(
            output_dir=out_dir,
            utf8=self.utf8_var.get(),
            transcode_wav_to_ogg=bool(self.ogg_var.get()),
            dlevel=dlevel,
            ffmpeg_path=self.ffmpeg_path,
            render_bgm=render_bgm,
            swap_lr=bool(self.swap_lr_var.get()),
            wav_labels=dict(self.wav_labels),
            image_mode=self.selected_image_mode(),
            export_bga=bool(self.export_bga_var.get()),
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
        self._clean_snapshot = self.model.snapshot()
        self.dirty = False
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
        if self.dirty and self.startup_dialogs:
            if not messagebox.askyesno(
                tr("dialog_dirty_close_title"),
                tr("dialog_dirty_close_body"),
                parent=self.root,
            ):
                return
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
    install_crash_handlers()
    args = list(sys.argv[1:] if argv is None else argv)
    initial = next((a for a in args if not a.startswith("-")), None)
    enable_hidpi()
    try:
        from tkinterdnd2 import TkinterDnD

        root = TkinterDnD.Tk()
    except Exception:
        root = tk.Tk()
    App(root, initial)
    root.mainloop()
    return 0
