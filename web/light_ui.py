"""Lightweight end-user GUI for Tableau to Power BI migration.

Zero external dependencies: built with Tkinter + subprocess.

Usage:
    python web/light_ui.py
"""

from __future__ import annotations

import os
import queue
import re
import json
import subprocess
import sys
import tempfile
import threading
import time
import csv
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

try:
    import winsound
except ImportError:
    winsound = None


class LightMigrationUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Tableau to Power BI - Light UI")
        self.root.geometry("980x680")
        self.root.minsize(900, 620)

        self.repo_root = Path(__file__).resolve().parents[1]
        self.migrate_script = self.repo_root / "migrate.py"
        self.settings_path = self.repo_root / ".light_ui_settings.json"

        self.mode_var = tk.StringVar(value="batch")
        self.preset_var = tk.StringVar(value="Migrate")
        self.source_var = tk.StringVar(value="")
        self.output_var = tk.StringVar(value=str(self.repo_root))
        self.verbose_var = tk.BooleanVar(value=True)
        self.assess_only_var = tk.BooleanVar(value=False)
        self.global_assess_var = tk.BooleanVar(value=False)
        self.prep_lineage_only_var = tk.BooleanVar(value=False)
        self.notify_var = tk.BooleanVar(value=True)
        self.auto_open_report_var = tk.BooleanVar(value=True)
        self.progress_var = tk.DoubleVar(value=0.0)
        self._progress_total = 0
        self._applying_preset = False
        self._run_started_at: float | None = None
        self._all_logs: list[str] = []
        self._last_output_dir = ""
        self._last_dashboard = ""
        self._last_comparison = ""
        self._last_summary_csv = ""
        self._kpi_only = False
        self._compact_mode = False
        self._task_buttons: dict[str, tk.Button] = {}
        self.section_frames: dict[str, tk.Widget] = {}
        self._status_pulse_job: str | None = None
        self._status_pulse_on = False
        self.ui = {
            "page_bg": "#e9eef8",
            "hero_bg": "#0b1f3a",
            "hero_fg": "#ecf4ff",
            "hero_sub": "#b6d1ff",
            "surface": "#ffffff",
            "surface_alt": "#f7faff",
            "border": "#c9d6ea",
            "muted": "#5c6b82",
            "text": "#13233a",
            "primary": "#0f4c81",
            "primary_hover": "#1a5e97",
            "chip_bg": "#e9f0fb",
            "chip_hover": "#dbe6f7",
            "log_bg": "#0f172a",
            "log_fg": "#dbeafe",
        }

        self._process: subprocess.Popen[str] | None = None
        self._log_queue: queue.Queue[str] = queue.Queue()
        self._running = False
        self._stop_requested = False
        self._active_context: dict[str, object] = {}
        self._session_records: list[dict[str, object]] = []

        self._load_settings()

        self._set_app_icon()
        self._configure_theme()
        self._build_ui()
        self._bind_shortcuts()
        self.root.bind("<Configure>", self._on_window_resize)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll_log_queue()

    def _bind_shortcuts(self) -> None:
        self.root.bind_all("<Control-r>", self._shortcut_run)
        self.root.bind_all("<Escape>", self._shortcut_stop)
        self.root.bind_all("<Control-l>", self._shortcut_clear_logs)

    def _shortcut_run(self, _event: tk.Event) -> str:
        self._start_run()
        return "break"

    def _shortcut_stop(self, _event: tk.Event) -> str:
        if self._running:
            self._stop_run()
        return "break"

    def _shortcut_clear_logs(self, _event: tk.Event) -> str:
        self._clear_logs()
        return "break"

    def _on_close(self) -> None:
        self._save_settings()
        self.root.destroy()

    def _load_settings(self) -> None:
        if not self.settings_path.exists():
            return
        try:
            data = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return

        source = data.get("source")
        output = data.get("output")
        preset = data.get("preset")
        verbose = data.get("verbose")
        notify = data.get("notify")
        auto_open = data.get("auto_open_report")

        if isinstance(source, str):
            self.source_var.set(source)
        if isinstance(output, str) and output.strip():
            self.output_var.set(output)
        else:
            self.output_var.set(str(self.repo_root))
        if preset in {"Assess", "Migrate", "Lineage"}:
            self.preset_var.set(preset)
        if isinstance(verbose, bool):
            self.verbose_var.set(verbose)
        if isinstance(notify, bool):
            self.notify_var.set(notify)
        if isinstance(auto_open, bool):
            self.auto_open_report_var.set(auto_open)

    def _save_settings(self) -> None:
        data = {
            "source": self.source_var.get().strip(),
            "output": self.output_var.get().strip(),
            "preset": self.preset_var.get(),
            "verbose": self.verbose_var.get(),
            "notify": self.notify_var.get(),
            "auto_open_report": self.auto_open_report_var.get(),
        }
        try:
            self.settings_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError:
            # Keep UI responsive even if settings file cannot be written.
            return

    def _configure_theme(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "Shiny.Horizontal.TProgressbar",
            troughcolor="#dbe4f4",
            background=self.ui["primary"],
            bordercolor="#dbe4f4",
            lightcolor="#2f78ba",
            darkcolor=self.ui["primary"],
            thickness=12,
        )

    def _bind_hover(
        self,
        btn: tk.Button,
        normal_bg: str,
        hover_bg: str,
        normal_fg: str = "#1f3f66",
        hover_fg: str | None = None,
        on_leave: callable | None = None,
    ) -> None:
        hover_fg = hover_fg or normal_fg

        def _on_enter(_event: tk.Event) -> None:
            btn.configure(bg=hover_bg, fg=hover_fg)

        def _on_leave(_event: tk.Event) -> None:
            if on_leave is not None:
                on_leave()
            else:
                btn.configure(bg=normal_bg, fg=normal_fg)

        btn.bind("<Enter>", _on_enter)
        btn.bind("<Leave>", _on_leave)

    def _set_app_icon(self) -> None:
        # Build a small in-memory icon so no external asset is required.
        icon = tk.PhotoImage(width=16, height=16)
        for y in range(16):
            for x in range(16):
                color = "#0f4c81" if x < 8 else "#1d9bf0"
                if (x + y) % 5 == 0:
                    color = "#f5b700"
                icon.put(color, (x, y))
        self._app_icon = icon
        self.root.iconphoto(True, self._app_icon)

    def _build_ui(self) -> None:
        self.root.configure(bg=self.ui["page_bg"])

        top = tk.Frame(self.root, padx=12, pady=12, bg=self.ui["page_bg"])
        top.pack(fill=tk.X)

        hero = tk.Frame(top, bg=self.ui["hero_bg"], padx=20, pady=18, bd=0, highlightthickness=0)
        hero.pack(fill=tk.X)
        self.hero_title_label = tk.Label(
            hero,
            text="Tableau to Power BI Migration",
            font=("Segoe UI", 20, "bold"),
            anchor="w",
            bg=self.ui["hero_bg"],
            fg=self.ui["hero_fg"],
        )
        self.hero_title_label.pack(fill=tk.X)
        self.hero_subtitle_label = tk.Label(
            hero,
            text="Pick a batch folder, choose where the result should go, then run the migration.",
            font=("Segoe UI", 10, "bold"),
            fg=self.ui["hero_sub"],
            anchor="w",
            bg=self.ui["hero_bg"],
        )
        self.hero_subtitle_label.pack(fill=tk.X, pady=(4, 8))

        quick_row = tk.Frame(hero, bg=self.ui["hero_bg"])
        quick_row.pack(fill=tk.X)
        self.quick_row = quick_row
        tk.Label(
            quick_row,
            text="1) Select batch folder   2) Select output   3) Click Run migration",
            fg=self.ui["hero_fg"],
            font=("Segoe UI", 11, "bold"),
            anchor="w",
            bg=self.ui["hero_bg"],
        ).pack(side=tk.LEFT)
        tk.Checkbutton(
            quick_row,
            text="Auto-open HTML report",
            variable=self.auto_open_report_var,
            bg=self.ui["hero_bg"],
            fg=self.ui["hero_fg"],
            activebackground=self.ui["hero_bg"],
            activeforeground=self.ui["hero_fg"],
            selectcolor=self.ui["hero_bg"],
        ).pack(side=tk.RIGHT)
        tk.Frame(hero, bg="#2f78ba", height=2).pack(fill=tk.X, pady=(10, 0))
        tk.Frame(hero, bg="#1c3f6b", height=1).pack(fill=tk.X)

        content = tk.Frame(self.root, padx=12, pady=10, bg=self.ui["page_bg"])
        content.pack(fill=tk.BOTH, expand=True)

        work_area = tk.Frame(content, bg=self.ui["page_bg"])
        work_area.pack(fill=tk.BOTH, expand=True)

        setup_card = tk.Frame(work_area, padx=12, pady=10, bd=1, relief="solid", bg=self.ui["surface"], highlightthickness=1, highlightbackground=self.ui["border"])
        setup_card.pack(fill=tk.X)
        self.section_frames["setup"] = setup_card
        tk.Label(setup_card, text="Migration Setup", font=("Segoe UI", 11, "bold"), fg=self.ui["text"], bg=self.ui["surface"], anchor="w").pack(fill=tk.X)
        tk.Frame(setup_card, bg="#d9e6f8", height=1).pack(fill=tk.X, pady=(6, 8))

        mode_row = tk.Frame(setup_card, bg=self.ui["surface"])
        mode_row.pack(fill=tk.X, pady=4)
        tk.Label(mode_row, text="Mode", width=14, anchor="w", bg=self.ui["surface"], fg=self.ui["muted"]).pack(side=tk.LEFT)
        tk.Label(mode_row, text="Batch folder only", fg=self.ui["primary"], bg=self.ui["surface"], font=("Segoe UI", 9, "bold")).pack(side=tk.LEFT)

        task_chip_row = tk.Frame(setup_card, bg=self.ui["surface"])
        task_chip_row.pack(fill=tk.X, pady=(2, 6))
        tk.Label(task_chip_row, text="Task", width=14, anchor="w", fg=self.ui["muted"], bg=self.ui["surface"]).pack(side=tk.LEFT)
        for task in ("Assess", "Migrate", "Lineage"):
            btn = tk.Button(
                task_chip_row,
                text=task,
                command=lambda t=task: self._set_task(t),
                relief="flat",
                bd=0,
                padx=10,
                pady=4,
                bg=self.ui["chip_bg"],
                activebackground=self.ui["chip_hover"],
                font=("Segoe UI", 9, "bold"),
            )
            btn.pack(side=tk.LEFT, padx=(0, 6))
            self._task_buttons[task] = btn
            self._bind_hover(btn, self.ui["chip_bg"], self.ui["chip_hover"], on_leave=self._refresh_task_chip_states)

        self.workflow_hint = tk.Label(
            setup_card,
            text="Batch workflow only. Pick one task: Assess, Migrate, or Lineage.",
            fg=self.ui["muted"],
            bg=self.ui["surface"],
            anchor="w",
            justify="left",
        )
        self.workflow_hint.pack(fill=tk.X, pady=(0, 6))

        src_row = tk.Frame(setup_card, bg=self.ui["surface"])
        src_row.pack(fill=tk.X, pady=4)
        tk.Label(src_row, text="Source", width=14, anchor="w", bg=self.ui["surface"], fg=self.ui["muted"]).pack(side=tk.LEFT)
        tk.Entry(src_row, textvariable=self.source_var, relief="solid", bd=1).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        self.source_browse_btn = tk.Button(src_row, text="Browse", command=self._browse_source, width=10, relief="flat", bd=0, bg=self.ui["chip_bg"], activebackground=self.ui["chip_hover"])
        self.source_browse_btn.pack(side=tk.LEFT)
        self._bind_hover(self.source_browse_btn, self.ui["chip_bg"], self.ui["chip_hover"])

        out_row = tk.Frame(setup_card, bg=self.ui["surface"])
        out_row.pack(fill=tk.X, pady=4)
        tk.Label(out_row, text="Output folder", width=14, anchor="w", bg=self.ui["surface"], fg=self.ui["muted"]).pack(side=tk.LEFT)
        tk.Entry(out_row, textvariable=self.output_var, relief="solid", bd=1).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        self.output_browse_btn = tk.Button(out_row, text="Browse", command=self._browse_output, width=10, relief="flat", bd=0, bg=self.ui["chip_bg"], activebackground=self.ui["chip_hover"])
        self.output_browse_btn.pack(side=tk.LEFT)
        self._bind_hover(self.output_browse_btn, self.ui["chip_bg"], self.ui["chip_hover"])

        opts_row = tk.Frame(setup_card, bg=self.ui["surface"])
        opts_row.pack(fill=tk.X, pady=4)
        tk.Label(opts_row, text="Options", width=14, anchor="w", bg=self.ui["surface"], fg=self.ui["muted"]).pack(side=tk.LEFT)
        tk.Checkbutton(opts_row, text="Verbose output", variable=self.verbose_var, bg=self.ui["surface"], activebackground=self.ui["surface"]).pack(side=tk.LEFT)
        tk.Checkbutton(opts_row, text="Notify when done", variable=self.notify_var, bg=self.ui["surface"], activebackground=self.ui["surface"]).pack(side=tk.LEFT, padx=(10, 0))

        mode_opts_row = tk.Frame(setup_card, bg=self.ui["surface"])
        self.assess_cb = tk.Checkbutton(
            mode_opts_row,
            text="Assessment only (--assess)",
            variable=self.assess_only_var,
            command=self._on_assess_toggle,
            bg=self.ui["surface"],
            activebackground=self.ui["surface"],
        )
        self.assess_cb.pack(side=tk.LEFT)
        self.global_assess_cb = tk.Checkbutton(
            mode_opts_row,
            text="Global assess (--global-assess)",
            variable=self.global_assess_var,
            command=self._on_global_assess_toggle,
            bg=self.ui["surface"],
            activebackground=self.ui["surface"],
        )
        self.global_assess_cb.pack(side=tk.LEFT, padx=(10, 0))
        self.prep_lineage_cb = tk.Checkbutton(
            mode_opts_row,
            text="Prep lineage only (--prep-lineage)",
            variable=self.prep_lineage_only_var,
            command=self._on_prep_lineage_toggle,
            bg=self.ui["surface"],
            activebackground=self.ui["surface"],
        )
        self.prep_lineage_cb.pack(side=tk.LEFT, padx=(10, 0))
        self.mode_opts_row = mode_opts_row

        actions_card = tk.Frame(work_area, padx=12, pady=10, bd=1, relief="solid", bg=self.ui["surface"], highlightthickness=1, highlightbackground=self.ui["border"])
        actions_card.pack(fill=tk.X, pady=(10, 0))
        self.section_frames["run"] = actions_card
        tk.Label(actions_card, text="Run", font=("Segoe UI", 11, "bold"), fg=self.ui["text"], bg=self.ui["surface"], anchor="w").pack(fill=tk.X)
        tk.Frame(actions_card, bg="#d9e6f8", height=1).pack(fill=tk.X, pady=(6, 8))
        actions = tk.Frame(actions_card, bg=self.ui["surface"])
        actions.pack(fill=tk.X)
        self.run_btn = tk.Button(
            actions,
            text="Run migration",
            width=16,
            command=self._start_run,
            bg=self.ui["primary"],
            fg="white",
            activebackground=self.ui["primary_hover"],
            activeforeground="white",
            relief="flat",
            bd=0,
            padx=12,
            pady=7,
            font=("Segoe UI", 10, "bold"),
        )
        self.run_btn.pack(side=tk.LEFT)
        self._bind_hover(self.run_btn, self.ui["primary"], self.ui["primary_hover"], normal_fg="white", hover_fg="white")
        self.stop_btn = tk.Button(
            actions,
            text="Stop",
            width=10,
            command=self._stop_run,
            state=tk.DISABLED,
            bg="#fde8e8",
            fg="#a61b1b",
            activebackground="#f8d1d1",
            relief="flat",
            bd=0,
            padx=8,
            pady=6,
            font=("Segoe UI", 9, "bold"),
        )
        self.stop_btn.pack(side=tk.LEFT, padx=(8, 0))
        self._bind_hover(self.stop_btn, "#fde8e8", "#f8d1d1", normal_fg="#a61b1b", hover_fg="#a61b1b")
        self.status_label = tk.Label(
            actions,
            text="Ready",
            fg="#145a32",
            bg="#e8f5ec",
            padx=10,
            pady=4,
            anchor="w",
            font=("Segoe UI", 9, "bold"),
        )
        self.status_label.pack(side=tk.LEFT, padx=(12, 0))
        results_actions = tk.Frame(actions_card, bg=self.ui["surface"])
        results_actions.pack(fill=tk.X, pady=(10, 0))
        tk.Label(results_actions, text="Results", fg=self.ui["primary"], bg=self.ui["surface"], font=("Segoe UI", 10, "bold")).pack(side=tk.LEFT)
        self.open_summary_btn = tk.Button(results_actions, text="Summary CSV", width=13,
                          command=self._open_summary_csv, state=tk.DISABLED)
        self.open_summary_btn.pack(side=tk.RIGHT, padx=(0, 8))
        self.open_comparison_btn = tk.Button(results_actions, text="Comparison", width=13,
                             command=self._open_comparison, state=tk.DISABLED)
        self.open_comparison_btn.pack(side=tk.RIGHT, padx=(0, 8))
        self.open_dashboard_btn = tk.Button(results_actions, text="HTML Report", width=13,
                            command=self._open_dashboard, state=tk.DISABLED)
        self.open_dashboard_btn.pack(side=tk.RIGHT, padx=(0, 8))
        self.open_output_btn = tk.Button(results_actions, text="Output Folder", width=13,
                         command=self._open_output_folder, state=tk.DISABLED)
        self.open_output_btn.pack(side=tk.RIGHT, padx=(0, 8))

        for btn in (self.open_output_btn, self.open_dashboard_btn, self.open_comparison_btn, self.open_summary_btn):
            btn.configure(relief="flat", bd=0, bg=self.ui["chip_bg"], activebackground=self.ui["chip_hover"], padx=8, pady=4)
            self._bind_hover(btn, self.ui["chip_bg"], self.ui["chip_hover"])
        self.section_frames["results"] = results_actions

        kpi_panel = tk.Frame(self.root, padx=12, pady=8, bg=self.ui["page_bg"])
        kpi_panel.pack(fill=tk.X)

        def _kpi_card(parent: tk.Frame, title: str) -> tk.Label:
            card = tk.Frame(parent, bg=self.ui["surface"], bd=1, relief="solid", padx=10, pady=6, highlightthickness=1, highlightbackground=self.ui["border"])
            tk.Frame(card, bg="#2a6ea9", height=2).pack(fill=tk.X, pady=(0, 6))
            tk.Label(card, text=title, bg=self.ui["surface"], fg=self.ui["muted"], font=("Segoe UI", 9)).pack(anchor="w")
            value_label = tk.Label(card, text="-", bg=self.ui["surface"], fg=self.ui["primary"], font=("Segoe UI", 12, "bold"))
            value_label.pack(anchor="w", pady=(2, 0))
            card.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
            return value_label

        self.kpi_measures_value = _kpi_card(kpi_panel, "Measures")
        self.kpi_visuals_value = _kpi_card(kpi_panel, "Visuals")
        self.kpi_values_value = _kpi_card(kpi_panel, "Visuals with values")
        self.kpi_fidelity_value = _kpi_card(kpi_panel, "Fidelity")

        progress_row = tk.Frame(self.root, padx=12, pady=6, bg=self.ui["page_bg"])
        progress_row.pack(fill=tk.X)
        self.progress = ttk.Progressbar(
            progress_row,
            orient="horizontal",
            mode="determinate",
            variable=self.progress_var,
            maximum=100,
            style="Shiny.Horizontal.TProgressbar",
        )
        self.progress.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.progress_text = tk.Label(progress_row, text="0%", width=8, anchor="e", bg=self.ui["page_bg"], fg=self.ui["text"], font=("Segoe UI", 9, "bold"))
        self.progress_text.pack(side=tk.LEFT, padx=(10, 0))

        status_row = tk.Frame(self.root, padx=12, pady=6, bg=self.ui["page_bg"])
        status_row.pack(fill=tk.X)
        self.stage_label = tk.Label(status_row, text="Stage: idle", anchor="w", fg=self.ui["text"], bg=self.ui["page_bg"])
        self.stage_label.pack(side=tk.LEFT)
        self.elapsed_label = tk.Label(status_row, text="Elapsed: 00:00", anchor="w", fg=self.ui["text"], bg=self.ui["page_bg"])
        self.elapsed_label.pack(side=tk.LEFT, padx=(20, 0))
        self.summary_label = tk.Label(status_row, text="", anchor="w", fg="#274672", bg=self.ui["page_bg"])
        self.summary_label.pack(side=tk.LEFT, padx=(20, 0))

        info_row = tk.Frame(self.root, padx=12, pady=2, bg=self.ui["page_bg"])
        info_row.pack(fill=tk.X)
        self.info_row = info_row
        self.dax_hint_label = tk.Label(
            info_row,
            text="Main KPI shows visuals with values. Explicit DAX visuals are tracked separately in the summary details.",
            anchor="w",
            fg=self.ui["muted"],
            bg=self.ui["page_bg"],
            justify="left",
            wraplength=920,
        )
        self.dax_hint_label.pack(fill=tk.X)

        health_row = tk.Frame(self.root, padx=12, pady=0, bg=self.ui["page_bg"])
        health_row.pack(fill=tk.X)
        self.health_row = health_row
        self.health_label = tk.Label(health_row, text="", anchor="w", fg="#1d4ed8", bg=self.ui["page_bg"])
        self.health_label.pack(fill=tk.X)

        logs_frame = tk.Frame(self.root, padx=12, pady=12, bg=self.ui["page_bg"])
        logs_frame.pack(fill=tk.BOTH, expand=True)
        log_card = tk.Frame(logs_frame, bg=self.ui["log_bg"], bd=1, relief="solid", highlightthickness=1, highlightbackground="#2f3b52")
        log_card.pack(fill=tk.BOTH, expand=True)
        logs_header = tk.Frame(log_card, bg=self.ui["log_bg"])
        logs_header.pack(fill=tk.X)
        tk.Label(logs_header, text="Logs", anchor="w", bg=self.ui["log_bg"], fg="#a7c4ff", font=("Consolas", 10, "bold")).pack(side=tk.LEFT, padx=(8, 0), pady=6)
        self.clear_logs_btn = tk.Button(
            logs_header,
            text="Clear",
            command=self._clear_logs,
            relief="flat",
            bd=0,
            bg="#253149",
            fg="#c8dcff",
            activebackground="#314568",
            activeforeground="#e3efff",
            padx=8,
            pady=3,
        )
        self.clear_logs_btn.pack(side=tk.RIGHT, padx=(0, 8), pady=6)
        self._bind_hover(self.clear_logs_btn, "#253149", "#314568", normal_fg="#c8dcff", hover_fg="#e3efff")
        self.log_box = scrolledtext.ScrolledText(log_card, wrap=tk.WORD, height=24)
        self.log_box.pack(fill=tk.BOTH, expand=True, pady=(4, 0))
        self.log_box.configure(
            state=tk.DISABLED,
            bg=self.ui["log_bg"],
            fg=self.ui["log_fg"],
            insertbackground="#e5eeff",
            selectbackground="#304a73",
            relief="flat",
            borderwidth=0,
            font=("Consolas", 10),
        )
        self.logs_frame = logs_frame
        self.section_frames["logs"] = logs_frame

        self._update_command_preview()
        self.source_var.trace_add("write", lambda *_: self._update_command_preview())
        self.output_var.trace_add("write", lambda *_: self._update_command_preview())
        self.mode_var.trace_add("write", lambda *_: self._update_command_preview())
        self.verbose_var.trace_add("write", lambda *_: self._update_command_preview())
        self.assess_only_var.trace_add("write", lambda *_: self._update_command_preview())
        self.global_assess_var.trace_add("write", lambda *_: self._update_command_preview())
        self.prep_lineage_only_var.trace_add("write", lambda *_: self._update_command_preview())
        self.mode_var.trace_add("write", lambda *_: self._update_option_states())
        self._update_option_states()
        self._apply_preset()

    def _set_task(self, task: str) -> None:
        self.preset_var.set(task)
        self._apply_preset()

    def _flash_section(self, widget: tk.Widget) -> None:
        try:
            old_bg = widget.cget("background")
            widget.configure(background="#fff7cc")
            self.root.after(700, lambda: widget.configure(background=old_bg))
        except tk.TclError:
            return

    def _refresh_task_chip_states(self) -> None:
        selected = self.preset_var.get()
        for task, btn in self._task_buttons.items():
            if task == selected:
                btn.configure(bg="#0f4c81", fg="white", activebackground="#1a5e97", activeforeground="white")
            else:
                btn.configure(bg="#edf2f7", fg="#1f3f66", activebackground="#dbe6f3", activeforeground="#1f3f66")

    def _apply_preset(self) -> None:
        preset = self.preset_var.get()
        self._applying_preset = True
        try:
            if preset == "Migrate":
                self.mode_var.set("batch")
                self.assess_only_var.set(False)
                self.global_assess_var.set(False)
                self.prep_lineage_only_var.set(False)
                self.verbose_var.set(True)
                self.workflow_hint.configure(text="Run a full migration batch and generate Power BI outputs.")
            elif preset == "Lineage":
                self.mode_var.set("batch")
                self.assess_only_var.set(False)
                self.global_assess_var.set(False)
                self.prep_lineage_only_var.set(True)
                self.verbose_var.set(True)
                self.workflow_hint.configure(text="Analyze Tableau Prep flow links across a folder of prep files.")
            elif preset == "Assess":
                self.mode_var.set("batch")
                self.assess_only_var.set(False)
                self.global_assess_var.set(True)
                self.prep_lineage_only_var.set(False)
                self.verbose_var.set(True)
                self.workflow_hint.configure(text="Review a whole folder and generate an overall assessment summary.")
        finally:
            self._applying_preset = False
            self._refresh_task_chip_states()

    def _update_option_states(self) -> None:
        self.assess_cb.configure(state=tk.DISABLED)
        self.global_assess_cb.configure(state=tk.NORMAL)
        self.prep_lineage_cb.configure(state=tk.NORMAL)
        self.assess_only_var.set(False)

    def _on_assess_toggle(self) -> None:
        if self.assess_only_var.get():
            self.global_assess_var.set(False)
            self.prep_lineage_only_var.set(False)

    def _on_global_assess_toggle(self) -> None:
        if self.global_assess_var.get():
            self.prep_lineage_only_var.set(False)

    def _on_prep_lineage_toggle(self) -> None:
        if self.prep_lineage_only_var.get():
            self.global_assess_var.set(False)

    def _browse_source(self) -> None:
        path = filedialog.askdirectory(title="Select folder for batch migration")
        if path:
            self.source_var.set(path)

    def _browse_output(self) -> None:
        path = filedialog.askdirectory(title="Select output folder")
        if path:
            self.output_var.set(path)

    def _toggle_kpi_only(self) -> None:
        self._kpi_only = not self._kpi_only
        if self._kpi_only:
            self.logs_frame.pack_forget()
            self._set_status("KPI-only view enabled", tone="success")
        else:
            self.logs_frame.pack(fill=tk.BOTH, expand=True)
            self._set_status("Ready", tone="success")

    def _toggle_compact_mode(self) -> None:
        self._compact_mode = not self._compact_mode
        if self._compact_mode:
            self.quick_row.pack_forget()
            self.info_row.pack_forget()
            self.health_row.pack_forget()
            self.log_box.configure(height=16)
            self.compact_state_label.configure(text="Compact: ON", fg="#145a32")
            self._set_status("Compact mode enabled", tone="success")
        else:
            self.quick_row.pack(fill=tk.X)
            self.info_row.pack(fill=tk.X, before=self.logs_frame)
            self.health_row.pack(fill=tk.X, before=self.logs_frame)
            self.log_box.configure(height=24)
            self.compact_state_label.configure(text="Compact: OFF", fg="#4b5563")
            self._set_status("Compact mode disabled", tone="info")

    def _on_window_resize(self, _event: tk.Event) -> None:
        # Keep helper text readable across narrow and wide window sizes.
        width = max(self.root.winfo_width(), 900)
        self.dax_hint_label.configure(wraplength=max(620, width - 80))

    def _build_command(self) -> list[str]:
        source = self.source_var.get().strip()
        output = self.output_var.get().strip()

        cmd = [sys.executable, str(self.migrate_script)]
        if self.prep_lineage_only_var.get():
            cmd += ["--prep-lineage", source]
        else:
            if self.global_assess_var.get():
                cmd.append("--global-assess")
            cmd += ["--batch", source]

        cmd += ["--output-dir", output]
        if self.verbose_var.get():
            cmd.append("--verbose")
        return cmd

    def _update_command_preview(self) -> None:
        return

    def _validate_inputs(self) -> bool:
        source = self.source_var.get().strip()
        output = self.output_var.get().strip()

        if not self.migrate_script.exists():
            messagebox.showerror("Missing script", f"Could not find migrate.py at:\n{self.migrate_script}")
            return False
        if not source:
            messagebox.showwarning("Missing source", "Please choose a source folder.")
            return False

        source_path = Path(source)
        if not source_path.is_dir():
            messagebox.showwarning("Invalid source", "Batch mode requires a folder.")
            return False

        if not output:
            messagebox.showwarning("Missing output", "Please choose an output folder.")
            return False

        Path(output).mkdir(parents=True, exist_ok=True)

        # Preflight: verify output directory is writable.
        try:
            with tempfile.NamedTemporaryFile(prefix="ttpbi_", suffix=".tmp", dir=output, delete=True):
                pass
        except OSError as exc:
            messagebox.showwarning(
                "Output not writable",
                f"Cannot write to output folder:\n{output}\n\nDetails: {exc}",
            )
            return False

        return True

    def _append_log(self, text: str) -> None:
        self._all_logs.append(text)
        self._capture_artifact_paths(text)
        self._update_progress_from_log(text)
        self.log_box.configure(state=tk.NORMAL)
        self.log_box.insert(tk.END, text)
        self.log_box.see(tk.END)
        self.log_box.configure(state=tk.DISABLED)

    def _capture_artifact_paths(self, text: str) -> None:
        out_match = re.search(r"Output:\s*(.+)", text)
        if out_match:
            self._last_output_dir = out_match.group(1).strip()

        dash_match = re.search(r"HTML dashboard:\s*(.+\.html)", text)
        if dash_match:
            self._last_dashboard = dash_match.group(1).strip()
            self._last_summary_csv = os.path.splitext(self._last_dashboard)[0] + "_summary.csv"

        html_report_match = re.search(r"HTML report:\s*(.+\.html)", text)
        if html_report_match:
            self._last_dashboard = html_report_match.group(1).strip()
            if not self._last_summary_csv:
                candidate_summary = os.path.join(
                    os.path.dirname(self._last_dashboard),
                    "global_assessment_summary.csv",
                )
                self._last_summary_csv = candidate_summary

        comp_match = re.search(r"Comparison report:\s*(.+\.html)", text)
        if comp_match:
            self._last_comparison = comp_match.group(1).strip()

        assess_summary_match = re.search(r"Assessment summary CSV:\s*(.+\.csv)", text)
        if assess_summary_match:
            self._last_summary_csv = assess_summary_match.group(1).strip()

        pbip_match = re.search(r"\[OK\]\s+Power BI Project created:\s*(.+)", text)
        if pbip_match and not self._last_output_dir:
            self._last_output_dir = pbip_match.group(1).strip()

    def _set_progress(self, value: float, label: str | None = None) -> None:
        clamped = max(0.0, min(100.0, value))
        self.progress_var.set(clamped)
        self.progress_text.configure(text=f"{clamped:.0f}%")
        if label:
            self._set_status(label, tone="info")

    def _set_status(self, text: str, tone: str = "info") -> None:
        palette = {
            "success": ("#145a32", "#e8f5ec"),
            "warn": ("#8a6d1f", "#fff4dc"),
            "error": ("#8a1f1f", "#fde8e8"),
            "info": ("#1f3f66", "#e8eef9"),
        }
        fg, bg = palette.get(tone, palette["info"])
        self.status_label.configure(text=text, fg=fg, bg=bg)

    def _start_status_pulse(self) -> None:
        self._stop_status_pulse()
        self._status_pulse_on = False
        self._status_pulse_job = self.root.after(420, self._tick_status_pulse)

    def _stop_status_pulse(self) -> None:
        if self._status_pulse_job:
            self.root.after_cancel(self._status_pulse_job)
            self._status_pulse_job = None

    def _tick_status_pulse(self) -> None:
        if not self._running:
            self._stop_status_pulse()
            return
        self._status_pulse_on = not self._status_pulse_on
        bg = "#fff4dc" if self._status_pulse_on else "#ffe9bf"
        self.status_label.configure(bg=bg, fg="#8a6d1f")
        self._status_pulse_job = self.root.after(420, self._tick_status_pulse)

    def _update_progress_from_log(self, text: str) -> None:
        batch_match = re.search(r"\[(\d+)/(\d+)\]\s+Migrating", text)
        if batch_match:
            done = int(batch_match.group(1))
            total = int(batch_match.group(2))
            self._progress_total = max(self._progress_total, total)
            self._set_progress((done / total) * 100.0, label=f"Running... ({done}/{total})")
            self.stage_label.configure(text="Stage: batch migration")
            return

        step_match = re.search(r"\[Step\s+(\d+)/(\d+)\]", text)
        if step_match and self.mode_var.get() == "single":
            step = int(step_match.group(1))
            total = int(step_match.group(2))
            self._set_progress((step / total) * 100.0, label=f"Running... (step {step}/{total})")
            if "TABLEAU OBJECTS EXTRACTION" in text:
                self.stage_label.configure(text="Stage: extract")
            elif "POWER BI PROJECT GENERATION" in text:
                self.stage_label.configure(text="Stage: generate")
            return

        if "MIGRATION REPORT:" in text:
            self.stage_label.configure(text="Stage: report")

        if "PBI Desktop Validation:" in text:
            self.stage_label.configure(text="Stage: validate")

        if "BATCH MIGRATION SUMMARY" in text or "IMPORT COMPLETE" in text:
            self._set_progress(100.0)

    def _update_elapsed_label(self) -> None:
        if not self._running or self._run_started_at is None:
            return
        elapsed = int(time.monotonic() - self._run_started_at)
        mins, secs = divmod(elapsed, 60)
        self.elapsed_label.configure(text=f"Elapsed: {mins:02d}:{secs:02d}")
        self.root.after(500, self._update_elapsed_label)

    def _start_run(self) -> None:
        if self._running:
            return
        if not self._validate_inputs():
            return

        context = {
            "source": self.source_var.get().strip(),
            "output": self.output_var.get().strip(),
            "mode": self.mode_var.get(),
            "command": self._build_command(),
            "options": self._collect_current_options(),
            "queue_index": None,
        }
        self._start_execution(context)

    def _start_execution(self, context: dict[str, object]) -> None:
        self._save_settings()
        self._running = True
        self._stop_requested = False
        self._active_context = context
        self._progress_total = 0
        self._run_started_at = time.monotonic()
        self._all_logs = []
        self._last_output_dir = ""
        self._last_dashboard = ""
        self._last_comparison = ""
        self._last_summary_csv = ""
        self.summary_label.configure(text="")
        self.health_label.configure(text="")
        self.stage_label.configure(text="Stage: starting")
        self.elapsed_label.configure(text="Elapsed: 00:00")
        self._set_progress(0.0, label="Running...")
        self.run_btn.configure(state=tk.DISABLED)
        self.stop_btn.configure(state=tk.NORMAL)
        self.open_output_btn.configure(state=tk.DISABLED)
        self.open_dashboard_btn.configure(state=tk.DISABLED)
        self.open_comparison_btn.configure(state=tk.DISABLED)
        self.open_summary_btn.configure(state=tk.DISABLED)
        self._set_status("Running...", tone="warn")
        self._start_status_pulse()

        self._append_log("\n" + "=" * 80 + "\n")
        self._append_log(f"Starting migration: {context.get('source', '')}\n")

        cmd = context["command"]
        thread = threading.Thread(target=self._run_process, args=(cmd,), daemon=True)
        thread.start()
        self.root.after(500, self._update_elapsed_label)

    def _run_process(self, cmd: object) -> None:
        try:
            assert isinstance(cmd, list)
            self._process = subprocess.Popen(
                cmd,
                cwd=str(self.repo_root),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )

            assert self._process.stdout is not None
            for line in self._process.stdout:
                self._log_queue.put(line)

            exit_code = self._process.wait()
            self._log_queue.put(f"\nProcess finished with exit code: {exit_code}\n")
            self._log_queue.put("__RUN_SUCCESS__" if exit_code == 0 else "__RUN_FAILED__")
        except Exception as exc:
            self._log_queue.put(f"\nError launching migration: {exc}\n")
            self._log_queue.put("__RUN_FAILED__")
        finally:
            self._process = None

    def _stop_run(self) -> None:
        self._stop_requested = True
        if self._process and self._process.poll() is None:
            self._process.terminate()
            self._append_log("\nStop requested by user.\n")

    def _finish_run(self, ok: bool) -> None:
        elapsed = 0.0
        if self._run_started_at is not None:
            elapsed = max(0.0, time.monotonic() - self._run_started_at)
        item_status = "success" if ok else ("stopped" if self._stop_requested else "failed")
        self._record_session_row(item_status, elapsed)

        self._running = False
        self._stop_status_pulse()
        self._run_started_at = None
        self.run_btn.configure(state=tk.NORMAL)
        self.stop_btn.configure(state=tk.DISABLED)

        if ok:
            self._set_progress(100.0)
            self._set_status("Completed", tone="success")
            self.stage_label.configure(text="Stage: completed")
            if self._last_output_dir:
                self.open_output_btn.configure(state=tk.NORMAL)
            if self._last_dashboard and os.path.exists(self._last_dashboard):
                self.open_dashboard_btn.configure(state=tk.NORMAL)
            if self._last_comparison and os.path.exists(self._last_comparison):
                self.open_comparison_btn.configure(state=tk.NORMAL)
            if self._last_summary_csv and os.path.exists(self._last_summary_csv):
                self.open_summary_btn.configure(state=tk.NORMAL)
            summary_text = ""
            if self._last_output_dir:
                summary_text = f"Output ready: {self._last_output_dir}"
            self.summary_label.configure(text=summary_text)
            self.health_label.configure(text=self._build_health_summary())
            self._update_kpi_panel(self._read_summary_metrics())
            if self.auto_open_report_var.get() and self._last_dashboard and os.path.exists(self._last_dashboard):
                os.startfile(self._last_dashboard)
            self._notify("Migration completed successfully")
            messagebox.showinfo("Migration complete", "Migration finished successfully.")
        else:
            if self.progress_var.get() < 1:
                self._set_progress(0.0)
            self._set_status("Failed", tone="error")
            self.stage_label.configure(text="Stage: failed")
            hint = self._build_error_hint()
            self.health_label.configure(text="")
            msg = "Migration ended with errors. Check logs."
            if hint:
                msg += f"\n\nSuggested action:\n- {hint}"
            self._notify("Migration failed")
            messagebox.showwarning("Migration failed", msg)

    def _collect_current_options(self) -> dict[str, object]:
        return {
            "verbose": self.verbose_var.get(),
            "assess_only": self.assess_only_var.get(),
            "global_assess": self.global_assess_var.get(),
            "prep_lineage_only": self.prep_lineage_only_var.get(),
            "preset": self.preset_var.get(),
        }

    def _record_session_row(self, status: str, elapsed_seconds: float) -> None:
        source = str(self._active_context.get("source", ""))
        output = str(self._active_context.get("output", ""))
        mode = str(self._active_context.get("mode", ""))
        options = self._active_context.get("options", {})
        if not isinstance(options, dict):
            options = {}
        metrics = self._read_summary_metrics()
        row = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source": source,
            "output": output,
            "mode": mode,
            "status": status,
            "duration_seconds": round(elapsed_seconds, 2),
            "options": options,
            "metrics": metrics,
        }
        self._session_records.append(row)


    def _read_summary_metrics(self) -> dict[str, object]:
        if not self._last_summary_csv or not os.path.exists(self._last_summary_csv):
            return {}
        try:
            with open(self._last_summary_csv, "r", encoding="utf-8", newline="") as fh:
                rows = list(csv.DictReader(fh))
            return rows[0] if rows else {}
        except OSError:
            return {}

    def _update_kpi_panel(self, metrics: dict[str, object]) -> None:
        self.kpi_measures_value.configure(text=str(metrics.get('measures_count', '-')))
        self.kpi_visuals_value.configure(text=str(metrics.get('visuals_count', '-')))
        self.kpi_values_value.configure(text=str(metrics.get('visuals_with_values_count', '-')))
        self.kpi_fidelity_value.configure(text=str(metrics.get('fidelity_score', '-')))

    def _clear_logs(self) -> None:
        self._all_logs = []
        self.log_box.configure(state=tk.NORMAL)
        self.log_box.delete("1.0", tk.END)
        self.log_box.configure(state=tk.DISABLED)


    def _notify(self, text: str) -> None:
        if not self.notify_var.get():
            return
        self.root.title(f"Tableau to Power BI - {text}")
        if winsound is not None:
            try:
                winsound.MessageBeep(winsound.MB_ICONASTERISK)
            except RuntimeError:
                pass

    def _build_error_hint(self) -> str:
        log_text = "\n".join(self._all_logs)
        checks = [
            ("handleClearSelection", "Regenerate with latest slicer hotfix and reopen the new .pbip output."),
            ("same name already exists", "Name collision detected; ensure latest generator is used and regenerate output from scratch."),
            ("Number of Records", "Use the patched build that removes Number of Records measure/column collisions and regenerate."),
            ("Access is denied", "Choose a different output folder or close apps locking files (OneDrive sync can lock files)."),
            ("No datasources found", "Source workbook has no extractable datasources; verify the Tableau file and rerun."),
        ]
        for token, hint in checks:
            if token in log_text:
                return hint
        return "Review the first ERROR/Traceback section in logs and rerun after applying that fix."

    def _open_output_folder(self) -> None:
        if self._last_output_dir and os.path.exists(self._last_output_dir):
            os.startfile(self._last_output_dir)
        else:
            messagebox.showwarning("Output not found", "No output folder is available yet.")

    def _open_dashboard(self) -> None:
        if self._last_dashboard and os.path.exists(self._last_dashboard):
            os.startfile(self._last_dashboard)
        else:
            messagebox.showwarning("Dashboard not found", "No HTML dashboard is available yet.")

    def _open_comparison(self) -> None:
        if self._last_comparison and os.path.exists(self._last_comparison):
            os.startfile(self._last_comparison)
        else:
            messagebox.showwarning("Comparison report not found", "No comparison report is available yet.")

    def _open_summary_csv(self) -> None:
        if self._last_summary_csv and os.path.exists(self._last_summary_csv):
            os.startfile(self._last_summary_csv)
        else:
            messagebox.showwarning("Summary CSV not found", "No summary CSV is available yet.")

    def _build_health_summary(self) -> str:
        if self._last_summary_csv and os.path.exists(self._last_summary_csv):
            try:
                with open(self._last_summary_csv, "r", encoding="utf-8", newline="") as fh:
                    rows = list(csv.DictReader(fh))
                if rows:
                    row = rows[0]
                    visuals = row.get("visuals_count", "-")
                    visuals_with_values = row.get("visuals_with_values_count", "-")
                    dax_visuals = row.get("visuals_with_dax_measures_count", "-")
                    measures = row.get("measures_count", "-")
                    return (
                        f"Health: measures={measures}, visuals={visuals}, "
                        f"visuals with values={visuals_with_values}, explicit DAX visuals={dax_visuals}"
                    )
            except OSError:
                pass
        return ""

    def _poll_log_queue(self) -> None:
        try:
            while True:
                msg = self._log_queue.get_nowait()
                if msg == "__RUN_SUCCESS__":
                    self._finish_run(ok=True)
                elif msg == "__RUN_FAILED__":
                    self._finish_run(ok=False)
                else:
                    self._append_log(msg)
        except queue.Empty:
            pass
        self.root.after(120, self._poll_log_queue)


def main() -> int:
    if os.environ.get("DISPLAY", "") == "" and os.name != "nt":
        print("No GUI display found. Run on a desktop environment.")
        return 1

    root = tk.Tk()
    app = LightMigrationUI(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
