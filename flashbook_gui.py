#!/usr/bin/env python3
"""
FlashBook Converter - Modern Desktop GUI for flashbook-exe2pdf.

Pick a Flash flipbook "eBook .exe", inspect its internal structure,
click Convert, and get a searchable, bookmarked, vector PDF.

Zero external dependencies required (stdlib tkinter + ttk).
Gracefully supports drag-and-drop if tkinterdnd2 is installed.
"""

import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser

# Core conversion engine
import flashbook_exe2pdf as core

# Tkinter & optional drag-and-drop (disabled on Linux due to XWayland xcb bugs)
try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:
    sys.exit(
        "tkinter is required (it ships with most Python installs).\n"
        "On Linux:  sudo apt install python3-tk   (or sudo dnf install python3-tkinter)"
    )

HAS_DND = False
if sys.platform != "linux":
    try:
        from tkinterdnd2 import DND_FILES, TkinterDnD
        HAS_DND = True
    except Exception:
        HAS_DND = False

# --------------------------------------------------------------------------- #
# Theme & Palette (Modern Slate / Dark Mode)
# --------------------------------------------------------------------------- #
BG_ROOT = "#0d1117"        # Deep canvas
BG_CARD = "#161b22"        # Elevated card surface
BG_INPUT = "#0f141c"       # Input fields & text areas
BG_HOVER = "#21262d"       # Card / button hover
BORDER = "#30363d"         # Subtle card border
BORDER_FOCUS = "#388bfd"   # Active focus outline

FG_PRIMARY = "#f0f6fc"     # Crisp readable text
FG_MUTED = "#8b949e"       # Secondary text
FG_DIM = "#484f58"         # Subtle hints / dividers

ACCENT = "#2563eb"         # Primary brand blue
ACCENT_HOVER = "#1d4ed8"
ACCENT_ACTIVE = "#1e40af"

OK = "#10b981"             # Emerald green
OK_BG = "#064e3b"
WARN = "#f59e0b"           # Amber
WARN_BG = "#451a03"
ERR = "#ef4444"            # Rose / red
ERR_BG = "#4c0519"
INFO = "#38bdf8"           # Sky blue


def detect_fonts():
    """Detect the cleanest available fonts on the current OS."""
    available = set(tk.font.families()) if hasattr(tk, "font") else set()
    
    # UI fonts
    ui_candidates = ["Segoe UI", "SF Pro Display", "Inter", "Ubuntu", "DejaVu Sans", "Liberation Sans", "Helvetica"]
    chosen_ui = next((f for f in ui_candidates if f in available), "TkDefaultFont")
    
    # Monospace fonts
    mono_candidates = ["Cascadia Code", "JetBrains Mono", "Fira Code", "Consolas", "DejaVu Sans Mono", "Courier New"]
    chosen_mono = next((f for f in mono_candidates if f in available), "TkFixedFont")
    
    return {
        "title": (chosen_ui, 14, "bold"),
        "section": (chosen_ui, 10, "bold"),
        "body": (chosen_ui, 9),
        "body_bold": (chosen_ui, 9, "bold"),
        "small": (chosen_ui, 8),
        "mono": (chosen_mono, 8),
        "button": (chosen_ui, 9, "bold"),
        "btn_lg": (chosen_ui, 11, "bold"),
    }


# --------------------------------------------------------------------------- #
# Helper: open a file / folder with the OS default app
# --------------------------------------------------------------------------- #
def open_with_default(path: str):
    """Open a file or directory using the OS default application."""
    try:
        if sys.platform == "win32":
            os.startfile(path)          # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        webbrowser.open("file://" + os.path.abspath(path))


def reveal_in_folder(path: str):
    """Open the directory containing the path and highlight/select the file."""
    try:
        path = os.path.abspath(path)
        folder = os.path.dirname(path) if os.path.isfile(path) else path
        if sys.platform == "win32":
            subprocess.Popen(["explorer", f"/select,{path}"])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
        else:
            open_with_default(folder)
    except Exception:
        open_with_default(os.path.dirname(path))


# --------------------------------------------------------------------------- #
# Custom Modern Widgets
# --------------------------------------------------------------------------- #
class ModernButton(tk.Button):
    """Clean flat button with responsive hover and active state transitions."""

    def __init__(self, master, text="", command=None, bg=BG_HOVER, fg=FG_PRIMARY,
                 hover_bg=None, active_bg=None, font=None, cursor="hand2",
                 padx=14, pady=6, **kwargs):
        super().__init__(
            master, text=text, command=command, bg=bg, fg=fg,
            activebackground=active_bg or hover_bg or bg,
            activeforeground=fg, relief="flat", bd=0,
            highlightthickness=0, font=font, cursor=cursor,
            padx=padx, pady=pady, **kwargs
        )
        self.default_bg = bg
        self.hover_bg = hover_bg or self._tint(bg, 1.15)
        self.active_bg = active_bg or self._tint(bg, 0.85)

        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)

    def _tint(self, hex_color, factor):
        if not hex_color.startswith("#") or len(hex_color) != 7:
            return hex_color
        try:
            r = int(hex_color[1:3], 16)
            g = int(hex_color[3:5], 16)
            b = int(hex_color[5:7], 16)
            r = min(255, max(0, int(r * factor)))
            g = min(255, max(0, int(g * factor)))
            b = min(255, max(0, int(b * factor)))
            return f"#{r:02x}{g:02x}{b:02x}"
        except Exception:
            return hex_color

    def _on_enter(self, _):
        if str(self["state"]) != "disabled":
            self.configure(bg=self.hover_bg)

    def _on_leave(self, _):
        if str(self["state"]) != "disabled":
            self.configure(bg=self.default_bg)

    def set_theme(self, bg, fg=FG_PRIMARY, hover_bg=None):
        self.default_bg = bg
        self.hover_bg = hover_bg or self._tint(bg, 1.15)
        self.configure(bg=bg, fg=fg)


class Card(tk.Frame):
    """Rounded-feel card container with border and background styling."""

    def __init__(self, master, bg=BG_CARD, border_color=BORDER, **kwargs):
        super().__init__(master, bg=border_color, padx=1, pady=1, **kwargs)
        self.inner = tk.Frame(self, bg=bg)
        self.inner.pack(fill="both", expand=True)


# --------------------------------------------------------------------------- #
# Main Application
# --------------------------------------------------------------------------- #
class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.fonts = detect_fonts()
        self.q: queue.Queue = queue.Queue()
        self.worker = None
        self.cancel_requested = False
        self.result = None
        self.start_time = 0
        self.inspection_cache = {}

        # Window settings
        root.title("FlashBook Converter — eBook .exe to PDF")
        root.configure(bg=BG_ROOT)
        root.minsize(680, 620)
        
        # Center window on screen
        self._center_window(760, 700)

        # Build UI layout
        self._setup_styles()
        self._build_ui()
        self._check_environment()

        # Keyboard shortcuts
        root.bind("<Control-o>", lambda e: self._pick_exe())
        root.bind("<Control-s>", lambda e: self._pick_out())
        root.bind("<Escape>", lambda e: self._cancel())

        # Thread-safe event polling
        root.after(60, self._drain)

    def _center_window(self, width, height):
        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = max(0, (sw - width) // 2)
        y = max(0, (sh - height) // 2)
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def _setup_styles(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(
            "Flashbook.Horizontal.TProgressbar",
            troughcolor=BG_INPUT,
            background=ACCENT,
            bordercolor=BORDER,
            lightcolor=ACCENT,
            darkcolor=ACCENT,
        )

    # ------------------------------------------------------------------ UI #
    def _build_ui(self):
        # Outer container with padding
        main = tk.Frame(self.root, bg=BG_ROOT)
        main.pack(fill="both", expand=True, padx=20, pady=16)

        # 1. Header & System Status
        self._build_header(main)

        # 2. Source Card (Book .exe Selection & Live Inspector)
        self._build_source_card(main)

        # 3. Output Card (Destination & Quick Actions)
        self._build_output_card(main)

        # 4. Advanced Settings (Collapsible)
        self._build_advanced_card(main)

        # 5. Progress & Status
        self._build_progress_section(main)

        # 6. Activity Log
        self._build_log_section(main)

        # 7. Action Bar & Result Banner
        self._build_action_bar(main)

    def _build_header(self, parent):
        header_frame = tk.Frame(parent, bg=BG_ROOT)
        header_frame.pack(fill="x", pady=(0, 14))

        # Title & subtitle
        left = tk.Frame(header_frame, bg=BG_ROOT)
        left.pack(side="left", fill="y")

        title_row = tk.Frame(left, bg=BG_ROOT)
        title_row.pack(anchor="w")

        tk.Label(title_row, text="📖", bg=BG_ROOT, fg=ACCENT,
                 font=("Segoe UI Emoji", 16)).pack(side="left", padx=(0, 8))
        tk.Label(title_row, text="FlashBook Converter", bg=BG_ROOT, fg=FG_PRIMARY,
                 font=self.fonts["title"]).pack(side="left")

        tk.Label(
            left,
            text="Extract & reconstruct Flash flipbook eBooks (.exe) into searchable, vector PDFs",
            bg=BG_ROOT, fg=FG_MUTED, font=self.fonts["body"]
        ).pack(anchor="w", pady=(2, 0))

        # System health badges on right
        self.badge_frame = tk.Frame(header_frame, bg=BG_ROOT)
        self.badge_frame.pack(side="right", anchor="e")

        self.java_badge = tk.Label(
            self.badge_frame, text="☕ Checking Java…", bg=BG_CARD, fg=FG_MUTED,
            font=self.fonts["small"], padx=8, pady=3, relief="flat"
        )
        self.java_badge.pack(side="right", padx=(6, 0))

        if HAS_DND:
            dnd_badge = tk.Label(
                self.badge_frame, text="✨ Drag & Drop Ready", bg=BG_CARD, fg=INFO,
                font=self.fonts["small"], padx=8, pady=3, relief="flat"
            )
            dnd_badge.pack(side="right")

    def _build_source_card(self, parent):
        card = Card(parent)
        card.pack(fill="x", pady=(0, 10))
        p = card.inner

        # Card header
        header = tk.Frame(p, bg=BG_CARD)
        header.pack(fill="x", padx=14, pady=(12, 6))

        tk.Label(header, text="SOURCE EBOOK EXECUTABLE", bg=BG_CARD, fg=FG_MUTED,
                 font=self.fonts["section"]).pack(side="left")
        
        self.dnd_hint = tk.Label(header, text="Drop .exe here or Browse", bg=BG_CARD,
                                 fg=FG_DIM, font=self.fonts["small"])
        self.dnd_hint.pack(side="right")

        # Input row
        row = tk.Frame(p, bg=BG_CARD)
        row.pack(fill="x", padx=14, pady=(0, 8))

        self.exe_var = tk.StringVar()
        self.exe_var.trace_add("write", self._on_exe_changed)

        input_frame = tk.Frame(row, bg=BORDER, padx=1, pady=1)
        input_frame.pack(side="left", fill="x", expand=True)

        self.exe_entry = tk.Entry(
            input_frame, textvariable=self.exe_var, bg=BG_INPUT, fg=FG_PRIMARY,
            insertbackground=FG_PRIMARY, relief="flat", font=self.fonts["body"],
            bd=0
        )
        self.exe_entry.pack(fill="x", ipady=7, padx=8)

        # Paste clean handler (strips quotes)
        self.exe_entry.bind("<Control-v>", self._clean_paste)

        ModernButton(
            row, text="Browse…", command=self._pick_exe, bg=ACCENT, fg="white",
            hover_bg=ACCENT_HOVER, font=self.fonts["button"], padx=16, pady=6
        ).pack(side="left", padx=(8, 0))

        ModernButton(
            row, text="Clear", command=self._clear_exe, bg=BG_HOVER, fg=FG_MUTED,
            font=self.fonts["body"], padx=10, pady=6
        ).pack(side="left", padx=(6, 0))

        # Setup Drag and drop if supported
        if HAS_DND:
            try:
                card.drop_target_register(DND_FILES)
                card.dnd_bind("<<Drop>>", self._on_drop)
                self.exe_entry.drop_target_register(DND_FILES)
                self.exe_entry.dnd_bind("<<Drop>>", self._on_drop)
            except Exception:
                pass

        # Live Pre-Flight Inspection Bar (Collapsible preview)
        self.inspect_frame = tk.Frame(p, bg=BG_HOVER, padx=10, pady=8)
        # Packed conditionally when an exe is examined
        self.inspect_icon = tk.Label(self.inspect_frame, text="🔍", bg=BG_HOVER,
                                     font=("Segoe UI Emoji", 10))
        self.inspect_icon.pack(side="left", padx=(0, 6))

        self.inspect_label = tk.Label(
            self.inspect_frame, text="Select an eBook .exe to inspect structure.",
            bg=BG_HOVER, fg=FG_MUTED, font=self.fonts["body"], anchor="w"
        )
        self.inspect_label.pack(side="left", fill="x", expand=True)

        self.inspect_badge = tk.Label(
            self.inspect_frame, text="", bg=BG_CARD, fg=FG_MUTED,
            font=self.fonts["small"], padx=6, pady=2
        )
        self.inspect_badge.pack(side="right")

    def _build_output_card(self, parent):
        card = Card(parent)
        card.pack(fill="x", pady=(0, 10))
        p = card.inner

        # Card header
        header = tk.Frame(p, bg=BG_CARD)
        header.pack(fill="x", padx=14, pady=(12, 6))

        tk.Label(header, text="DESTINATION PDF", bg=BG_CARD, fg=FG_MUTED,
                 font=self.fonts["section"]).pack(side="left")

        # Input row
        row = tk.Frame(p, bg=BG_CARD)
        row.pack(fill="x", padx=14, pady=(0, 12))

        self.out_var = tk.StringVar()
        input_frame = tk.Frame(row, bg=BORDER, padx=1, pady=1)
        input_frame.pack(side="left", fill="x", expand=True)

        self.out_entry = tk.Entry(
            input_frame, textvariable=self.out_var, bg=BG_INPUT, fg=FG_PRIMARY,
            insertbackground=FG_PRIMARY, relief="flat", font=self.fonts["body"],
            bd=0
        )
        self.out_entry.pack(fill="x", ipady=7, padx=8)

        ModernButton(
            row, text="Browse…", command=self._pick_out, bg=BG_HOVER, fg=FG_PRIMARY,
            font=self.fonts["body"], padx=14, pady=6
        ).pack(side="left", padx=(8, 0))

    def _build_advanced_card(self, parent):
        self.adv_card = Card(parent)
        self.adv_card.pack(fill="x", pady=(0, 10))
        p = self.adv_card.inner

        # Toggle Bar
        toggle_bar = tk.Frame(p, bg=BG_CARD, cursor="hand2")
        toggle_bar.pack(fill="x", padx=14, pady=8)
        toggle_bar.bind("<Button-1>", lambda e: self._toggle_advanced())

        self.adv_toggle_icon = tk.Label(toggle_bar, text="▸", bg=BG_CARD, fg=ACCENT,
                                       font=self.fonts["body_bold"])
        self.adv_toggle_icon.pack(side="left", padx=(0, 6))

        tk.Label(toggle_bar, text="Advanced Settings", bg=BG_CARD, fg=FG_PRIMARY,
                 font=self.fonts["body_bold"]).pack(side="left")
        tk.Label(toggle_bar, text="(scratch directory, extract only, ffdec.jar)",
                 bg=BG_CARD, fg=FG_MUTED, font=self.fonts["small"]).pack(side="left", padx=8)

        # Options Container (hidden by default)
        self.adv_content = tk.Frame(p, bg=BG_CARD, padx=14, pady=4)
        self.adv_expanded = False

        # 1. Extract only checkbox
        self.extract_only_var = tk.BooleanVar(value=False)
        cb1 = tk.Checkbutton(
            self.adv_content, text="Extract only (carve SWF blobs & dump index without rendering PDF)",
            variable=self.extract_only_var, bg=BG_CARD, fg=FG_PRIMARY, selectcolor=BG_INPUT,
            activebackground=BG_CARD, activeforeground=FG_PRIMARY, font=self.fonts["body"],
            relief="flat", bd=0, highlightthickness=0
        )
        cb1.pack(anchor="w", pady=(2, 6))

        # 2. Auto-open PDF
        self.auto_open_var = tk.BooleanVar(value=True)
        cb2 = tk.Checkbutton(
            self.adv_content, text="Open PDF automatically when conversion finishes",
            variable=self.auto_open_var, bg=BG_CARD, fg=FG_PRIMARY, selectcolor=BG_INPUT,
            activebackground=BG_CARD, activeforeground=FG_PRIMARY, font=self.fonts["body"],
            relief="flat", bd=0, highlightthickness=0
        )
        cb2.pack(anchor="w", pady=(0, 6))

        # 3. Custom Workdir
        workdir_row = tk.Frame(self.adv_content, bg=BG_CARD)
        workdir_row.pack(fill="x", pady=(2, 6))
        tk.Label(workdir_row, text="Keep Scratch Dir:", bg=BG_CARD, fg=FG_MUTED,
                 font=self.fonts["body"], width=16, anchor="w").pack(side="left")
        self.workdir_var = tk.StringVar()
        w_in = tk.Frame(workdir_row, bg=BORDER, padx=1, pady=1)
        w_in.pack(side="left", fill="x", expand=True)
        tk.Entry(w_in, textvariable=self.workdir_var, bg=BG_INPUT, fg=FG_PRIMARY,
                 insertbackground=FG_PRIMARY, relief="flat", font=self.fonts["body"], bd=0
                 ).pack(fill="x", ipady=4, padx=6)
        ModernButton(workdir_row, text="Choose…", command=self._pick_workdir, bg=BG_HOVER,
                     fg=FG_PRIMARY, font=self.fonts["small"], padx=10, pady=3
                     ).pack(side="left", padx=(6, 0))

        # 4. Custom ffdec.jar
        ffdec_row = tk.Frame(self.adv_content, bg=BG_CARD)
        ffdec_row.pack(fill="x", pady=(2, 6))
        tk.Label(ffdec_row, text="Custom ffdec.jar:", bg=BG_CARD, fg=FG_MUTED,
                 font=self.fonts["body"], width=16, anchor="w").pack(side="left")
        self.ffdec_var = tk.StringVar()
        f_in = tk.Frame(ffdec_row, bg=BORDER, padx=1, pady=1)
        f_in.pack(side="left", fill="x", expand=True)
        tk.Entry(f_in, textvariable=self.ffdec_var, bg=BG_INPUT, fg=FG_PRIMARY,
                 insertbackground=FG_PRIMARY, relief="flat", font=self.fonts["body"], bd=0
                 ).pack(fill="x", ipady=4, padx=6)
        ModernButton(ffdec_row, text="Choose…", command=self._pick_ffdec, bg=BG_HOVER,
                     fg=FG_PRIMARY, font=self.fonts["small"], padx=10, pady=3
                     ).pack(side="left", padx=(6, 0))

    def _toggle_advanced(self):
        if self.adv_expanded:
            self.adv_content.pack_forget()
            self.adv_toggle_icon.config(text="▸")
            self.adv_expanded = False
        else:
            self.adv_content.pack(fill="x", padx=14, pady=(0, 10))
            self.adv_toggle_icon.config(text="▾")
            self.adv_expanded = True

    def _build_progress_section(self, parent):
        p_frame = tk.Frame(parent, bg=BG_ROOT)
        p_frame.pack(fill="x", pady=(0, 8))

        # Status text + Percentage
        row = tk.Frame(p_frame, bg=BG_ROOT)
        row.pack(fill="x", pady=(0, 4))

        self.status_title = tk.Label(
            row, text="Ready.", bg=BG_ROOT, fg=FG_PRIMARY,
            font=self.fonts["body_bold"], anchor="w"
        )
        self.status_title.pack(side="left")

        self.percent_label = tk.Label(
            row, text="", bg=BG_ROOT, fg=ACCENT,
            font=self.fonts["body_bold"], anchor="e"
        )
        self.percent_label.pack(side="right")

        # Progress bar
        self.progress = ttk.Progressbar(
            p_frame, style="Flashbook.Horizontal.TProgressbar", mode="determinate",
            maximum=100, value=0
        )
        self.progress.pack(fill="x", ipady=2)

    def _build_log_section(self, parent):
        card = Card(parent)
        card.pack(fill="both", expand=True, pady=(0, 10))
        p = card.inner

        # Log toolbar
        bar = tk.Frame(p, bg=BG_CARD)
        bar.pack(fill="x", padx=12, pady=(8, 4))

        tk.Label(bar, text="ACTIVITY LOG", bg=BG_CARD, fg=FG_MUTED,
                 font=self.fonts["section"]).pack(side="left")

        self.log_lines_badge = tk.Label(bar, text="0 events", bg=BG_CARD, fg=FG_DIM,
                                       font=self.fonts["small"])
        self.log_lines_badge.pack(side="left", padx=8)

        # Right tools
        self.autoscroll_var = tk.BooleanVar(value=True)
        tk.Checkbutton(
            bar, text="Auto-scroll", variable=self.autoscroll_var, bg=BG_CARD,
            fg=FG_MUTED, selectcolor=BG_INPUT, activebackground=BG_CARD,
            activeforeground=FG_PRIMARY, font=self.fonts["small"], relief="flat",
            bd=0, highlightthickness=0
        ).pack(side="right")

        ModernButton(
            bar, text="Clear", command=self._clear_log, bg=BG_HOVER, fg=FG_MUTED,
            font=self.fonts["small"], padx=8, pady=2
        ).pack(side="right", padx=6)

        ModernButton(
            bar, text="Copy Log", command=self._copy_log, bg=BG_HOVER, fg=FG_MUTED,
            font=self.fonts["small"], padx=8, pady=2
        ).pack(side="right", padx=(0, 6))

        # Log Text Box with Scrollbar
        log_box_frame = tk.Frame(p, bg=BG_INPUT)
        log_box_frame.pack(fill="both", expand=True, padx=12, pady=(0, 10))

        self.log_text = tk.Text(
            log_box_frame, bg=BG_INPUT, fg=FG_PRIMARY, relief="flat",
            font=self.fonts["mono"], wrap="word", state="disabled",
            insertbackground=FG_PRIMARY, padx=10, pady=8, bd=0
        )
        scrollbar = ttk.Scrollbar(log_box_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)

        scrollbar.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)

        # Syntax color tags
        self.log_text.tag_config("time", foreground=FG_DIM)
        self.log_text.tag_config("step", foreground=INFO, font=self.fonts["body_bold"])
        self.log_text.tag_config("ok", foreground=OK, font=self.fonts["body_bold"])
        self.log_text.tag_config("warn", foreground=WARN)
        self.log_text.tag_config("err", foreground=ERR, font=self.fonts["body_bold"])
        self.log_text.tag_config("dim", foreground=FG_MUTED)

        self.log_count = 0

    def _build_action_bar(self, parent):
        # Result summary banner (hidden until finish)
        self.summary_banner = tk.Frame(parent, bg=OK_BG, padx=12, pady=8)
        # Conditionally packed upon success

        self.summary_icon = tk.Label(self.summary_banner, text="🎉", bg=OK_BG,
                                     font=("Segoe UI Emoji", 12))
        self.summary_icon.pack(side="left", padx=(0, 8))

        self.summary_text = tk.Label(
            self.summary_banner, text="", bg=OK_BG, fg=FG_PRIMARY,
            font=self.fonts["body_bold"]
        )
        self.summary_text.pack(side="left", fill="x", expand=True)

        # Action Buttons row
        action_row = tk.Frame(parent, bg=BG_ROOT)
        action_row.pack(fill="x", pady=(8, 0))

        # Left hints
        tk.Label(
            action_row, text="Shortcuts: Ctrl+O (Browse) • Esc (Cancel)",
            bg=BG_ROOT, fg=FG_DIM, font=self.fonts["small"]
        ).pack(side="left", anchor="c")

        # Right control buttons
        self.cancel_btn = ModernButton(
            action_row, text="Cancel", command=self._cancel, bg=BG_HOVER, fg=ERR,
            hover_bg="#3b1d1d", font=self.fonts["button"], padx=14, pady=8,
            state="disabled"
        )
        self.cancel_btn.pack(side="right", padx=(8, 0))

        self.open_folder_btn = ModernButton(
            action_row, text="Show in Folder", command=self._open_folder,
            bg=BG_HOVER, fg=FG_PRIMARY, font=self.fonts["button"], padx=14, pady=8,
            state="disabled"
        )
        self.open_folder_btn.pack(side="right", padx=(8, 0))

        self.open_pdf_btn = ModernButton(
            action_row, text="Open PDF", command=self._open_pdf,
            bg=BG_HOVER, fg=FG_PRIMARY, font=self.fonts["button"], padx=16, pady=8,
            state="disabled"
        )
        self.open_pdf_btn.pack(side="right", padx=(8, 0))

        self.start_btn = ModernButton(
            action_row, text="Convert to PDF", command=self._start,
            bg=ACCENT, fg="white", hover_bg=ACCENT_HOVER,
            active_bg=ACCENT_ACTIVE, font=self.fonts["btn_lg"], padx=26, pady=8
        )
        self.start_btn.pack(side="right", padx=(8, 0))

    # -------------------------------------------------------- Environment #
    def _check_environment(self):
        """Check for Java runtime and pypdf."""
        has_java = bool(shutil.which("java"))
        if has_java:
            try:
                proc = subprocess.run(["java", "-version"], capture_output=True, text=True, timeout=3)
                out = proc.stderr or proc.stdout
                m = re.search(r'version "([^"]+)"', out)
                ver = m.group(1) if m else "Ready"
                self.java_badge.config(text=f"☕ Java {ver}", fg=OK)
            except Exception:
                self.java_badge.config(text="☕ Java Ready", fg=OK)
        else:
            self.java_badge.config(text="⚠️ Java Not Found", fg=WARN)
            self._log_event("WARNING: Java is not in your system PATH. Rendering pages requires Java.", "warn")

    # ---------------------------------------------------- File Selection #
    def _clean_paste(self, _):
        """Handle path pasting and strip quotes gracefully."""
        try:
            text = self.root.clipboard_get().strip().strip('"').strip("'")
            if text and os.path.exists(text):
                self.exe_var.set(text)
                return "break"
        except Exception:
            pass

    def _on_drop(self, event):
        """Handle drag-and-drop file drop."""
        data = event.data.strip()
        # TkinterDnD passes file paths in curly braces if they contain spaces
        if data.startswith("{") and data.endswith("}"):
            data = data[1:-1]
        data = data.strip('"').strip("'")
        if os.path.isfile(data):
            self.exe_var.set(data)

    def _pick_exe(self):
        path = filedialog.askopenfilename(
            title="Choose the eBook .exe",
            filetypes=[("eBook Executables", "*.exe"), ("All Files", "*.*")]
        )
        if path:
            self.exe_var.set(path)

    def _clear_exe(self):
        self.exe_var.set("")
        self.out_var.set("")
        self.inspect_frame.pack_forget()
        self.status_title.config(text="Ready.", fg=FG_PRIMARY)
        self.percent_label.config(text="")
        self.progress["value"] = 0

    def _pick_out(self):
        initial = self.out_var.get() or "book.pdf"
        path = filedialog.asksaveasfilename(
            title="Save PDF As", defaultextension=".pdf",
            filetypes=[("PDF Documents", "*.pdf")],
            initialfile=os.path.basename(initial),
            initialdir=os.path.dirname(initial) if os.path.dirname(initial) else None
        )
        if path:
            self.out_var.set(path)

    def _pick_workdir(self):
        d = filedialog.askdirectory(title="Choose Scratch Directory")
        if d:
            self.workdir_var.set(d)

    def _pick_ffdec(self):
        p = filedialog.askopenfilename(
            title="Select ffdec.jar",
            filetypes=[("Java Archive", "*.jar"), ("All Files", "*.*")]
        )
        if p:
            self.ffdec_var.set(p)

    # ------------------------------------------------- Live Pre-Flight Check #
    def _on_exe_changed(self, *_):
        path = self.exe_var.get().strip().strip('"').strip("'")
        if not path or not os.path.isfile(path):
            self.inspect_frame.pack_forget()
            return

        # Auto-suggest output path
        default_out = os.path.splitext(path)[0] + ".pdf"
        if not self.out_var.get() or self.out_var.get().endswith(".pdf"):
            self.out_var.set(default_out)

        # Run fast inspection in thread
        threading.Thread(target=self._inspect_async, args=(path,), daemon=True).start()

    def _inspect_async(self, path):
        if path in self.inspection_cache:
            info = self.inspection_cache[path]
        else:
            info = core.inspect_exe(path)
            self.inspection_cache[path] = info
        self.q.put(("INSPECT", (path, info)))

    def _apply_inspection(self, path, info):
        # Check that path still matches
        if self.exe_var.get().strip().strip('"').strip("'") != path:
            return

        self.inspect_frame.pack(fill="x", padx=14, pady=(0, 10))
        size_mb = info["size"] / 1e6

        if info["valid"] and info["is_flipbook"]:
            self.inspect_icon.config(text="✨")
            title = info.get("title") or os.path.splitext(os.path.basename(path))[0]
            pages_desc = f"{info['indexed_pages']} pages indexed" if info["indexed_pages"] else "Flipbook container"
            self.inspect_label.config(
                text=f"Title: {title}  •  {size_mb:.1f} MB  •  {info['manifest_count']} assets  •  {pages_desc}",
                fg=FG_PRIMARY
            )
            self.inspect_badge.config(text="✔ Flip PDF eBook", bg=OK_BG, fg=OK)
            self.status_title.config(text="eBook verified. Ready to convert.", fg=OK)

            # Auto refine output name if title detected
            if info.get("title") and info["title"] != "Converted eBook":
                folder = os.path.dirname(self.out_var.get()) or os.path.dirname(path)
                safe_title = re.sub(r'[\\/*?:"<>|]', "", info["title"]).strip()
                if safe_title:
                    self.out_var.set(os.path.join(folder, f"{safe_title}.pdf"))

        elif info["valid"]:
            self.inspect_icon.config(text="⚠️")
            self.inspect_label.config(
                text=f"PE overlay found ({size_mb:.1f} MB), but standard flipbook manifest was not detected.",
                fg=WARN
            )
            self.inspect_badge.config(text="Fallback Mode", bg=WARN_BG, fg=WARN)
            self.status_title.config(text="Ready (generic container).", fg=WARN)
        else:
            self.inspect_icon.config(text="❌")
            err = info.get("error") or "Not a compatible eBook .exe"
            self.inspect_label.config(text=f"Warning: {err}", fg=ERR)
            self.inspect_badge.config(text="Invalid", bg=ERR_BG, fg=ERR)
            self.status_title.config(text="Unsupported file format.", fg=ERR)

    # ----------------------------------------------------- Conversion Flow #
    def _start(self):
        if self.worker and self.worker.is_alive():
            return

        exe = self.exe_var.get().strip().strip('"').strip("'")
        if not exe or not os.path.isfile(exe):
            messagebox.showerror("FlashBook Converter", "Please select a valid .exe file first.")
            return

        out = self.out_var.get().strip() or os.path.splitext(exe)[0] + ".pdf"
        workdir = self.workdir_var.get().strip() or None
        ffdec = self.ffdec_var.get().strip() or None
        extract_only = self.extract_only_var.get()

        # UI State update
        self.cancel_requested = False
        self.result = None
        self.start_time = time.time()
        self.start_btn.config(text="Converting…", state="disabled")
        self.cancel_btn.config(state="normal")
        self.open_pdf_btn.config(state="disabled")
        self.open_folder_btn.config(state="disabled")
        self.summary_banner.pack_forget()

        self.status_title.config(text="Starting conversion…", fg=FG_PRIMARY)
        self.percent_label.config(text="0%")
        self.progress["mode"] = "indeterminate"
        self.progress.start(15)

        self._log_event(f"Starting conversion: {os.path.basename(exe)}", "step")
        self._log_event(f"Input: {exe}\nOutput: {out}", "dim")

        q = self.q

        def rep_cb(msg, kind):
            q.put(("LOG", (msg, kind)))

        def progress_cb(current, total):
            pct = int((current / total) * 100) if total else 0
            q.put(("PROGRESS", (current, total, pct)))

        def cancel_check():
            return self.cancel_requested

        def task():
            try:
                rep = core.Reporter(rep_cb)
                res = core.convert(
                    exe=exe, out=out, workdir=workdir, extract_only=extract_only,
                    ffdec=ffdec, rep=rep, progress=progress_cb, cancel_check=cancel_check
                )
                q.put(("RESULT", res))
            except Exception as e:
                q.put(("RESULT", {"error": str(e)}))

        self.worker = threading.Thread(target=task, daemon=True)
        self.worker.start()

    def _cancel(self):
        if self.worker and self.worker.is_alive():
            self.cancel_requested = True
            self.status_title.config(text="Cancelling conversion…", fg=WARN)
            self._log_event("Cancellation requested by user...", "warn")
            self.cancel_btn.config(state="disabled")

    # --------------------------------------------------------- Event Queue #
    def _drain(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "RESULT":
                    self._on_finish(payload)
                elif kind == "INSPECT":
                    path, info = payload
                    self._apply_inspection(path, info)
                elif kind == "PROGRESS":
                    cur, total, pct = payload
                    self.progress["mode"] = "determinate"
                    self.progress["value"] = pct
                    self.percent_label.config(text=f"{pct}%")
                    self.status_title.config(text=f"Rendering pages: {cur} of {total} ({pct}%)")
                elif kind == "LOG":
                    msg, ltype = payload
                    self._log_event(msg, ltype)
        except queue.Empty:
            pass
        self.root.after(60, self._drain)

    def _log_event(self, msg: str, kind="plain"):
        self.log_text.config(state="normal")
        t_str = time.strftime("[%H:%M:%S] ")
        self.log_text.insert("end", t_str, "time")

        if kind == "step":
            self.log_text.insert("end", f"{msg}\n", "step")
            self.status_title.config(text=msg, fg=INFO)
        elif kind == "done":
            self.log_text.insert("end", f"{msg}\n", "ok")
        elif kind == "warn":
            self.log_text.insert("end", f"{msg}\n", "warn")
        elif kind == "err":
            self.log_text.insert("end", f"{msg}\n", "err")
        else:
            self.log_text.insert("end", f"{msg}\n")

        if self.autoscroll_var.get():
            self.log_text.see("end")

        self.log_text.config(state="disabled")
        self.log_count += 1
        self.log_lines_badge.config(text=f"{self.log_count} events")

    def _clear_log(self):
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")
        self.log_count = 0
        self.log_lines_badge.config(text="0 events")

    def _copy_log(self):
        try:
            txt = self.log_text.get("1.0", "end").strip()
            self.root.clipboard_clear()
            self.root.clipboard_append(txt)
            self._log_event("Log copied to clipboard.", "dim")
        except Exception:
            pass

    # -------------------------------------------------------------- Finish #
    def _on_finish(self, result):
        self.progress.stop()
        self.start_btn.config(text="Convert to PDF", state="normal")
        self.cancel_btn.config(state="disabled")
        elapsed = time.time() - self.start_time

        if "error" in result:
            err_msg = result["error"]
            if self.cancel_requested:
                self.status_title.config(text="Cancelled.", fg=WARN)
                self.percent_label.config(text="")
                self._log_event("Conversion was cancelled.", "warn")
            else:
                self.status_title.config(text="Failed.", fg=ERR)
                self.percent_label.config(text="")
                self._log_event(f"ERROR: {err_msg}", "err")
                messagebox.showerror("FlashBook Converter", f"Conversion failed:\n\n{err_msg}")
            return

        self.result = result
        self.open_pdf_btn.config(state="normal")
        self.open_folder_btn.config(state="normal")
        self.progress["mode"] = "determinate"
        self.progress["value"] = 100
        self.percent_label.config(text="100%")

        # Summary calculation
        out_path = result.get("out")
        pages = result.get("pages", 0)
        size_mb = os.path.getsize(out_path) / 1e6 if out_path and os.path.isfile(out_path) else 0

        if result.get("blobs") and not out_path:
            # Extract only mode
            summary = f"Extracted {result['blobs']} SWF blobs in {elapsed:.1f}s."
            self.status_title.config(text="Extraction Complete.", fg=OK)
        else:
            summary = f"Generated {pages} vector pages ({size_mb:.1f} MB) in {elapsed:.1f}s!"
            self.status_title.config(text="Done! PDF Created successfully.", fg=OK)

        self._log_event(f"[SUCCESS] {summary}", "done")

        # Present celebratory summary banner
        self.summary_text.config(text=summary)
        self.summary_banner.pack(fill="x", pady=(4, 8), before=self.start_btn.master)

        # Auto open actions
        if self.auto_open_var.get() and out_path and os.path.isfile(out_path):
            open_with_default(out_path)

    def _open_pdf(self):
        if self.result and self.result.get("out"):
            open_with_default(self.result["out"])

    def _open_folder(self):
        if self.result and self.result.get("out"):
            reveal_in_folder(self.result["out"])
        elif self.out_var.get():
            reveal_in_folder(self.out_var.get())


# --------------------------------------------------------------------------- #
# Entry Point
# --------------------------------------------------------------------------- #
def main():
    if HAS_DND:
        root = TkinterDnD.Tk()
    else:
        root = tk.Tk()

    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
