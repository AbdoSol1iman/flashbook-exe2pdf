#!/usr/bin/env python3
"""
FlashBook Converter - a minimal desktop front-end for flashbook-exe2pdf.

Pick a Flash flipbook "eBook .exe", click Start, get a real PDF.
The heavy lifting is done by flashbook_exe2pdf.convert(); this file is
pure UI (stdlib tkinter only).

Run from source:   pip install pypdf && python3 flashbook_gui.py
"""

import os
import queue
import subprocess
import sys
import threading
import webbrowser

import flashbook_exe2pdf as core

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:
    sys.exit("tkinter is required (it ships with most Python installs).\n"
             "On Linux:  sudo apt install python3-tk   (or the dnf equivalent)")

# --------------------------------------------------------------------------- #
# theme
# --------------------------------------------------------------------------- #
BG, BG2 = "#16181d", "#1f222a"
FG, FG_DIM = "#e8eaf0", "#8a90a0"
ACCENT = "#4f8cff"
OK, ERR = "#5dd67b", "#ff6b6b"
FONT = ("Segoe UI", 10)
FONT_S = ("Segoe UI", 9)
FONT_MONO = ("Consolas", 8)

# --------------------------------------------------------------------------- #
# open a file / folder with the OS default app
# --------------------------------------------------------------------------- #
def open_with_default(path: str):
    try:
        if sys.platform == "win32":
            os.startfile(path)          # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        webbrowser.open("file://" + path)


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.q: queue.Queue = queue.Queue()
        self.worker = None
        self.result = None
        root.title("FlashBook Converter")
        root.configure(bg=BG)
        root.minsize(560, 420)
        self._build()

        # poll the log queue -> text widget (thread-safe)
        root.after(100, self._drain)

    # ------------------------------------------------------------------ UI #
    def _build(self):
        pad = {"padx": 14, "pady": 6}

        head = tk.Label(self.root, text="FlashBook Converter", bg=BG, fg=FG,
                        font=("Segoe UI", 16, "bold"))
        head.pack(anchor="w", pady=(14, 0), padx=14)
        tk.Label(self.root,
                 text='Flash flipbook "eBook .exe"  →  real, searchable PDF',
                 bg=BG, fg=FG_DIM, font=FONT_S).pack(anchor="w", padx=14,
                                                     pady=(0, 10))

        # --- input row
        row1 = tk.Frame(self.root, bg=BG)
        row1.pack(fill="x", **pad)
        tk.Label(row1, text="Book (.exe)", bg=BG, fg=FG_DIM,
                 font=FONT_S, width=11, anchor="w").pack(side="left")
        self.exe_var = tk.StringVar()
        self.exe_entry = tk.Entry(row1, textvariable=self.exe_var, bg=BG2,
                                  fg=FG, insertbackground=FG, relief="flat",
                                  font=FONT, highlightthickness=1,
                                  highlightbackground=BG2,
                                  highlightcolor=ACCENT)
        self.exe_entry.pack(side="left", fill="x", expand=True, ipady=5)
        tk.Button(row1, text="Browse…", command=self._pick_exe, bg=BG2,
                  fg=FG, activebackground=ACCENT, activeforeground="white",
                  relief="flat", font=FONT_S, padx=12, cursor="hand2"
                  ).pack(side="left", padx=(8, 0), ipady=4)

        # --- output row
        row2 = tk.Frame(self.root, bg=BG)
        row2.pack(fill="x", padx=14, pady=(0, 6))
        tk.Label(row2, text="Save as", bg=BG, fg=FG_DIM,
                 font=FONT_S, width=11, anchor="w").pack(side="left")
        self.out_var = tk.StringVar()
        self.out_entry = tk.Entry(row2, textvariable=self.out_var, bg=BG2,
                                  fg=FG, insertbackground=FG, relief="flat",
                                  font=FONT, highlightthickness=1,
                                  highlightbackground=BG2,
                                  highlightcolor=ACCENT)
        self.out_entry.pack(side="left", fill="x", expand=True, ipady=5)
        tk.Button(row2, text="Browse…", command=self._pick_out, bg=BG2,
                  fg=FG, activebackground=ACCENT, activeforeground="white",
                  relief="flat", font=FONT_S, padx=12, cursor="hand2"
                  ).pack(side="left", padx=(8, 0), ipady=4)

        # --- status + progress
        self.status = tk.Label(self.root, text="Pick a book to begin.",
                               bg=BG, fg=FG_DIM, font=FONT, anchor="w")
        self.status.pack(fill="x", padx=14, pady=(8, 0))
        self.progress = ttk.Progressbar(self.root, mode="indeterminate",
                                        length=10000)
        self.progress.pack(fill="x", padx=14, pady=(2, 6))

        # --- log
        self.log_text = tk.Text(self.root, bg=BG2, fg=FG_DIM, relief="flat",
                                font=FONT_MONO, height=12, wrap="word",
                                state="disabled", insertbackground=FG,
                                padx=10, pady=8)
        self.log_text.pack(fill="both", expand=True, padx=14, pady=(0, 10))

        # --- buttons
        btns = tk.Frame(self.root, bg=BG)
        btns.pack(fill="x", padx=14, pady=(0, 14))
        self.start_btn = tk.Button(btns, text="Start", command=self._start,
                                   bg=ACCENT, fg="white",
                                   activebackground="#6b9dff",
                                   relief="flat", font=("Segoe UI", 11, "bold"),
                                   padx=26, pady=7, cursor="hand2")
        self.start_btn.pack(side="right")
        self.open_btn = tk.Button(btns, text="Open PDF", command=self._open,
                                  bg=BG2, fg=FG, activebackground=ACCENT,
                                  activeforeground="white", relief="flat",
                                  font=FONT_S, padx=16, pady=6,
                                  cursor="hand2", state="disabled")
        self.open_btn.pack(side="right", padx=10)

    # ------------------------------------------------------------ pickers #
    def _pick_exe(self):
        p = filedialog.askopenfilename(
            title="Choose the eBook .exe",
            filetypes=[("eBook executables", "*.exe"), ("All files", "*")])
        if not p:
            return
        self.exe_var.set(p)
        self.out_var.set(os.path.splitext(p)[0] + ".pdf")
        self.status.config(text="Ready.", fg=OK)
        self._log(f"selected: {p}\n")

    def _pick_out(self):
        p = filedialog.asksaveasfilename(
            title="Save PDF as", defaultextension=".pdf",
            filetypes=[("PDF", "*.pdf")],
            initialfile=self.out_var.get() or "book.pdf")
        if p:
            self.out_var.set(p)

    # -------------------------------------------------------------- start #
    def _start(self):
        if self.worker and self.worker.is_alive():
            return
        exe = self.exe_var.get().strip()
        if not exe or not os.path.isfile(exe):
            messagebox.showerror("FlashBook Converter",
                                 "Please choose a valid .exe file first.")
            return
        out = self.out_var.get().strip() or os.path.splitext(exe)[0] + ".pdf"

        self.result = None
        self.open_btn.config(state="disabled")
        self.start_btn.config(text="Converting…", state="disabled")
        self.status.config(text="Working — first run downloads the renderer "
                                "(~22 MB).", fg=FG_DIM)
        self.progress.start(12)
        self._log(f"converting: {exe}\n")

        q = self.q

        def rep_cb(msg, kind):
            if kind == "done":
                q.put(("ok", msg))
            elif kind == "warn":
                q.put(("warn", msg))
            elif kind == "step":
                q.put(("step", msg))
            else:
                q.put(("line", "  " + msg))

        def task():
            try:
                result = core.convert(exe, out=out, rep=core.Reporter(rep_cb))
                q.put(("RESULT", result))
            except Exception as e:
                q.put(("RESULT", {"error": str(e)}))

        self.worker = threading.Thread(target=task, daemon=True)
        self.worker.start()

    # --------------------------------------------------------------- poll #
    def _drain(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "RESULT":
                    self._finish(payload)
                elif kind == "step":
                    self._log("\n" + payload)
                elif kind == "ok":
                    self._log(payload, OK)
                elif kind == "warn":
                    self._log(payload, "#e0b03a")
                elif kind == "line":
                    self._log(payload)
                else:
                    self._log(payload)
        except queue.Empty:
            pass
        self.root.after(100, self._drain)

    def _log(self, line: str, color=None):
        self.log_text.config(state="normal")
        tag = None
        if color:
            tag = f"c{color}"
            self.log_text.tag_config(tag, foreground=color)
        self.log_text.insert("end", line + "\n", tag or ())
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    # ------------------------------------------------------------- finish #
    def _finish(self, result):
        self.progress.stop()
        self.start_btn.config(text="Start", state="normal")
        if "error" in result:
            self.status.config(text="Failed.", fg=ERR)
            self._log("ERROR: " + result["error"], ERR)
            messagebox.showerror("FlashBook Converter",
                                 "Conversion failed:\n\n" + result["error"])
            return
        self.result = result
        self.status.config(
            text=f"Done — {result['pages']} pages, "
                 f"{os.path.getsize(result['out']) / 1e6:.1f} MB", fg=OK)
        self.open_btn.config(state="normal")
        open_with_default(result["out"])

    def _open(self):
        if self.result and self.result.get("out"):
            open_with_default(self.result["out"])


def main():
    root = tk.Tk()
    if sys.platform != "darwin":        # macOS Aqua already themes itself
        try:
            style = ttk.Style()
            style.theme_use("clam")
            style.configure("Horizontal.TProgressbar", background=ACCENT,
                            troughcolor=BG2, bordercolor=BG, lightcolor=ACCENT,
                            darkcolor=ACCENT)
        except Exception:
            pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
