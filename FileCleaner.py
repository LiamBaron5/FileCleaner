#!/usr/bin/env python3
"""
FileCleaner - Step 1: scan and visualize what is taking up space on your Mac.

This version is strictly READ-ONLY. It never deletes, moves, renames, or
modifies anything. The only actions it can take are "Reveal in Finder" and
"Copy path".

Every file and folder is also classified by how safe it would be to touch:
  PROTECTED - part of macOS itself; never delete or move.
  CAUTION   - app data, settings, installed apps, or developer tools;
              removing these can break apps or lose settings.
  (blank)   - ordinary user files.

Usage:
  python3 FileCleaner.py                 # open the GUI, scanning your home folder
  python3 FileCleaner.py ~/Downloads     # open the GUI on a specific folder
  python3 FileCleaner.py ~/Downloads --report   # print a text summary instead

Tip: macOS hides some folders (Mail, Messages, Safari, etc.) from apps that
don't have "Full Disk Access". To see them, grant it to the app you run this
from (Terminal / VS Code) in System Settings > Privacy & Security.
"""

import argparse
import heapq
import itertools
import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

HOME = str(Path.home())

# --------------------------------------------------------------------------
# Safety classification
# --------------------------------------------------------------------------

PROTECTED = "protected"
CAUTION = "caution"

# Checked in order; the first matching prefix wins, so list specific paths
# before the broader paths that contain them.
SAFETY_RULES = [
    ("/usr/local", CAUTION, "Installed tools (e.g. Homebrew)"),
    ("/opt", CAUTION, "Installed tools (e.g. Homebrew)"),
    ("/System", PROTECTED, "macOS system"),
    ("/usr", PROTECTED, "macOS system"),
    ("/bin", PROTECTED, "macOS system"),
    ("/sbin", PROTECTED, "macOS system"),
    ("/private", PROTECTED, "macOS system (etc/var/tmp)"),
    ("/etc", PROTECTED, "macOS system"),
    ("/var", PROTECTED, "macOS system"),
    ("/tmp", PROTECTED, "macOS system"),
    ("/dev", PROTECTED, "macOS system"),
    ("/cores", PROTECTED, "macOS system"),
    ("/Library", PROTECTED, "System-wide app support"),
    ("/Applications", CAUTION, "Installed app - uninstall properly"),
    (os.path.join(HOME, "Library"), CAUTION, "App data & settings"),
    (os.path.join(HOME, "Applications"), CAUTION, "Installed app - uninstall properly"),
]


def classify(path):
    """Return (level, reason) describing how safe a path is to touch."""
    if path == "/":
        return PROTECTED, "Startup disk root"
    for prefix, level, reason in SAFETY_RULES:
        if path == prefix or path.startswith(prefix + "/"):
            return level, reason
    if ".app/" in path or path.endswith(".app"):
        return CAUTION, "Application bundle"
    if path.startswith(HOME + "/"):
        rel = path[len(HOME) + 1:]
        if any(part.startswith(".") for part in rel.split("/")):
            return CAUTION, "Hidden config/data"
    return "", ""


# --------------------------------------------------------------------------
# File categories (for the "File types" view and treemap colors)
# --------------------------------------------------------------------------

CATEGORIES = {
    "Images": "jpg jpeg png gif heic heif tif tiff bmp webp raw cr2 nef arw dng svg psd ai",
    "Video": "mov mp4 m4v avi mkv wmv flv webm mpg mpeg 3gp",
    "Audio": "mp3 m4a aac wav aiff aif flac ogg wma alac mid midi",
    "Documents": "pdf doc docx xls xlsx ppt pptx pages numbers key txt rtf md csv odt epub",
    "Archives & Installers": "zip dmg pkg tar gz tgz bz2 xz rar 7z iso xip",
    "Code & Data": "py js ts java c cpp h swift go rs rb php html css json xml yml yaml sql db sqlite ipynb",
}
EXT_TO_CATEGORY = {ext: cat for cat, exts in CATEGORIES.items() for ext in exts.split()}

CATEGORY_COLORS = {
    "Folder": "#5b8def",
    "Images": "#2bb673",
    "Video": "#9b59b6",
    "Audio": "#e67e9f",
    "Documents": "#3fb8c9",
    "Archives & Installers": "#f39c4a",
    "Code & Data": "#8a9a5b",
    "Other": "#9aa5b1",
}
SAFETY_COLORS = {PROTECTED: "#d9534f", CAUTION: "#e8b33a"}


def extension_of(name):
    ext = os.path.splitext(name)[1].lower().lstrip(".")
    return ext or "(none)"


def category_of(name):
    return EXT_TO_CATEGORY.get(extension_of(name), "Other")


def human(n):
    """Format a byte count the way Finder does (base 1000)."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000


def fmt_date(ts):
    return time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else ""


# --------------------------------------------------------------------------
# Scanner
# --------------------------------------------------------------------------

# Never descend into these (unless the user scans them directly): they are
# other drives, network mounts, or duplicate views of the same disk.
SKIP_DIRS = {"/System/Volumes", "/Volumes", "/dev", "/net", "/home"}


class Node:
    __slots__ = ("name", "parent", "is_dir", "size", "count", "mtime", "children", "note")

    def __init__(self, name, parent, is_dir, mtime=0.0, size=0):
        self.name = name
        self.parent = parent
        self.is_dir = is_dir
        self.size = size  # bytes actually used on disk
        self.count = 0 if is_dir else 1  # number of files inside
        self.mtime = mtime
        self.children = [] if is_dir else None
        self.note = ""  # e.g. "Permission denied"

    def path(self):
        parts = []
        node = self
        while node is not None:
            parts.append(node.name)
            node = node.parent
        return os.path.join(*reversed(parts))


class Scanner:
    """Walks a folder tree and builds a Node tree with sizes. Read-only."""

    LARGEST_KEEP = 1000

    def __init__(self, root_path):
        self.root_path = os.path.abspath(os.path.expanduser(root_path))
        self.cancel_event = threading.Event()
        self.files_seen = 0
        self.bytes_seen = 0
        self.current = ""
        self.unreadable = []  # folders we were not allowed to read
        self.ext_stats = {}  # ext -> [bytes, count]
        self.largest = []  # min-heap of (size, tiebreak, node)
        self.root = None
        self.error = None
        self.done = False
        self.cancelled = False

    def cancel(self):
        self.cancel_event.set()

    def run(self):
        try:
            self.root = self._scan()
        except Exception as e:  # report instead of crashing the thread
            self.error = e
        finally:
            self.done = True

    def _scan(self):
        st = os.stat(self.root_path)
        if not stat.S_ISDIR(st.st_mode):
            raise NotADirectoryError(f"Not a folder: {self.root_path}")

        root = Node(self.root_path, None, True, st.st_mtime)
        seen_dirs = {(st.st_dev, st.st_ino)}
        seen_hardlinks = set()
        tiebreak = itertools.count()
        dirs_in_order = []
        stack = [root]

        while stack:
            if self.cancel_event.is_set():
                self.cancelled = True
                break
            node = stack.pop()
            dirs_in_order.append(node)
            path = node.path()
            self.current = path
            try:
                with os.scandir(path) as entries:
                    for entry in entries:
                        try:
                            est = entry.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        if stat.S_ISDIR(est.st_mode):
                            child = Node(entry.name, node, True, est.st_mtime)
                            node.children.append(child)
                            key = (est.st_dev, est.st_ino)
                            child_path = os.path.join(path, entry.name)
                            if child_path in SKIP_DIRS:
                                child.note = "Skipped (other volume)"
                            elif key in seen_dirs:
                                child.note = "Skipped (already counted)"
                            else:
                                seen_dirs.add(key)
                                stack.append(child)
                            continue

                        # Files and symlinks. st_blocks is real disk usage, so
                        # iCloud files that aren't downloaded count as ~0.
                        size = est.st_blocks * 512
                        if est.st_nlink > 1 and not stat.S_ISLNK(est.st_mode):
                            key = (est.st_dev, est.st_ino)
                            if key in seen_hardlinks:
                                size = 0
                            else:
                                seen_hardlinks.add(key)
                        child = Node(entry.name, node, False, est.st_mtime, size)
                        node.children.append(child)

                        self.files_seen += 1
                        self.bytes_seen += size
                        ext = extension_of(entry.name)
                        stats = self.ext_stats.setdefault(ext, [0, 0])
                        stats[0] += size
                        stats[1] += 1
                        item = (size, next(tiebreak), child)
                        if len(self.largest) < self.LARGEST_KEEP:
                            heapq.heappush(self.largest, item)
                        elif size > self.largest[0][0]:
                            heapq.heapreplace(self.largest, item)
            except PermissionError:
                node.note = "Permission denied"
                self.unreadable.append(path)
            except OSError as e:
                node.note = e.strerror or "Unreadable"
                self.unreadable.append(path)

        # Children always come after their parent in dirs_in_order, so walking
        # it backwards totals up every folder before its parent needs it.
        for d in reversed(dirs_in_order):
            d.size = sum(c.size for c in d.children)
            d.count = sum(c.count for c in d.children)
            d.children.sort(key=lambda c: c.size, reverse=True)
        return root

    def largest_files(self):
        return [node for _, _, node in sorted(self.largest, reverse=True)]


# --------------------------------------------------------------------------
# Treemap layout (squarified algorithm)
# --------------------------------------------------------------------------

def _worst_ratio(row, short_side):
    s = sum(row)
    return max(short_side * short_side * max(row) / (s * s),
               (s * s) / (short_side * short_side * min(row)))


def squarify(sizes, x, y, w, h):
    """Lay out sizes (sorted largest first) as rectangles filling x,y,w,h."""
    total = sum(sizes)
    if total <= 0 or w <= 0 or h <= 0:
        return []
    scale = w * h / total
    areas = [s * scale for s in sizes]
    rects = []
    i = 0
    while i < len(areas):
        short_side = min(w, h)
        row = [areas[i]]
        i += 1
        while i < len(areas) and _worst_ratio(row + [areas[i]], short_side) <= _worst_ratio(row, short_side):
            row.append(areas[i])
            i += 1
        row_sum = sum(row)
        if w >= h:  # stack this row as a column on the left
            col_w = row_sum / h
            yy = y
            for a in row:
                rects.append((x, yy, col_w, a / col_w))
                yy += a / col_w
            x += col_w
            w -= col_w
        else:  # stack this row along the top
            row_h = row_sum / w
            xx = x
            for a in row:
                rects.append((xx, y, a / row_h, row_h))
                xx += a / row_h
            y += row_h
            h -= row_h
    return rects


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------

def run_gui(start_path):
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    MAX_TREE_CHILDREN = 300
    MAX_MAP_ITEMS = 150

    class App:
        def __init__(self, win):
            self.win = win
            self.scanner = None
            self.root_node = None
            self.tree_nodes = {}  # tree iid -> Node
            self.map_node = None
            self.map_items = {}  # canvas tag -> Node (or None for "smaller items")
            self.list_nodes = {}  # largest-files iid -> Node
            self.ignore_next_select = False

            win.title("FileCleaner - Disk Explorer (read-only)")
            win.geometry("1300x800")
            win.protocol("WM_DELETE_WINDOW", self.on_close)
            self._build_ui(start_path)

        # ---------- layout ----------

        def _build_ui(self, start_path):
            top = ttk.Frame(self.win, padding=8)
            top.pack(fill="x")
            ttk.Label(top, text="Folder:").pack(side="left")
            self.path_var = tk.StringVar(value=start_path)
            entry = ttk.Entry(top, textvariable=self.path_var)
            entry.pack(side="left", fill="x", expand=True, padx=6)
            entry.bind("<Return>", lambda e: self.start_scan())
            ttk.Button(top, text="Choose...", command=self.choose_folder).pack(side="left")
            self.scan_btn = ttk.Button(top, text="Scan", command=self.start_scan)
            self.scan_btn.pack(side="left", padx=(6, 0))
            self.stop_btn = ttk.Button(top, text="Stop", command=self.stop_scan, state="disabled")
            self.stop_btn.pack(side="left", padx=(6, 0))
            self.spinner = ttk.Progressbar(top, mode="indeterminate", length=100)
            self.spinner.pack(side="left", padx=(10, 0))

            self.status_var = tk.StringVar(value="Choose a folder and press Scan. Nothing will be modified.")
            ttk.Label(self.win, textvariable=self.status_var, anchor="w", padding=(8, 4)).pack(side="bottom", fill="x")

            self.notebook = ttk.Notebook(self.win)
            self.notebook.pack(fill="both", expand=True, padx=8, pady=(0, 4))
            self._build_explorer_tab()
            self._build_largest_tab()
            self._build_types_tab()

            self.menu = tk.Menu(self.win, tearoff=0)
            self.menu.add_command(label="Reveal in Finder", command=lambda: self._menu_action("reveal"))
            self.menu.add_command(label="Copy path", command=lambda: self._menu_action("copy"))
            self.menu.add_command(label="Show in Explorer", command=lambda: self._menu_action("show"))
            self.menu_node = None

        def _scrolled_tree(self, parent, columns, show="tree headings"):
            frame = ttk.Frame(parent)
            tree = ttk.Treeview(frame, columns=columns, show=show, selectmode="browse")
            vsb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
            tree.configure(yscrollcommand=vsb.set)
            tree.pack(side="left", fill="both", expand=True)
            vsb.pack(side="right", fill="y")
            tree.tag_configure(PROTECTED, foreground=SAFETY_COLORS[PROTECTED])
            tree.tag_configure(CAUTION, foreground="#b8860b")
            tree.tag_configure("muted", foreground="gray")
            return frame, tree

        def _build_explorer_tab(self):
            paned = ttk.PanedWindow(self.notebook, orient="horizontal")
            self.notebook.add(paned, text="Explorer")

            frame, tree = self._scrolled_tree(paned, ("size", "share", "items", "modified", "safety"))
            self.tree = tree
            for col, label, width, anchor in (
                ("#0", "Name", 280, "w"),
                ("size", "Size", 90, "e"),
                ("share", "% of parent", 150, "w"),
                ("items", "Files", 90, "e"),
                ("modified", "Modified", 90, "center"),
                ("safety", "Safety", 200, "w"),
            ):
                tree.heading(col, text=label)
                tree.column(col, width=width, anchor=anchor, stretch=(col in ("#0", "safety")))
            tree.bind("<<TreeviewOpen>>", self.on_tree_open)
            tree.bind("<<TreeviewSelect>>", self.on_tree_select)
            self._bind_context_menu(tree, lambda iid: self.tree_nodes.get(iid))
            paned.add(frame, weight=3)

            right = ttk.Frame(paned)
            bar = ttk.Frame(right)
            bar.pack(fill="x", pady=(0, 4))
            ttk.Button(bar, text="Up", width=4, command=self.map_up).pack(side="left")
            self.map_title = tk.StringVar()
            ttk.Label(bar, textvariable=self.map_title, anchor="w").pack(side="left", padx=6, fill="x", expand=True)
            self.canvas = tk.Canvas(right, background="#20242a", highlightthickness=0)
            self.canvas.pack(fill="both", expand=True)
            self.canvas.bind("<Configure>", lambda e: self.draw_map())
            self.canvas.bind("<Motion>", self.on_map_hover)
            self.canvas.bind("<Double-Button-1>", self.on_map_double_click)
            self.canvas.bind("<Button-1>", self.on_map_click)
            self._bind_context_menu(self.canvas, lambda _: self._map_node_under_cursor(), canvas=True)
            self.hover_var = tk.StringVar(value="Hover a block for details. Double-click a folder to zoom in.")
            ttk.Label(right, textvariable=self.hover_var, anchor="w").pack(fill="x", pady=(4, 0))

            legend = ttk.Frame(right)
            legend.pack(fill="x", pady=(2, 0))
            for name, color in list(CATEGORY_COLORS.items()) + [("Protected", SAFETY_COLORS[PROTECTED]),
                                                                ("Caution", SAFETY_COLORS[CAUTION])]:
                swatch = tk.Canvas(legend, width=12, height=12, highlightthickness=0, background=color)
                swatch.pack(side="left", padx=(6, 2))
                ttk.Label(legend, text=name).pack(side="left")
            paned.add(right, weight=2)

        def _build_largest_tab(self):
            frame, tree = self._scrolled_tree(self.notebook, ("size", "modified", "safety", "path"), show="headings")
            self.largest_tree = tree
            for col, label, width, anchor in (
                ("size", "Size", 90, "e"),
                ("modified", "Modified", 90, "center"),
                ("safety", "Safety", 200, "w"),
                ("path", "Path", 700, "w"),
            ):
                tree.heading(col, text=label)
                tree.column(col, width=width, anchor=anchor, stretch=(col == "path"))
            tree.bind("<Double-Button-1>", lambda e: self._show_in_explorer(self.list_nodes.get(tree.focus())))
            self._bind_context_menu(tree, lambda iid: self.list_nodes.get(iid))
            self.notebook.add(frame, text="Largest files")

        def _build_types_tab(self):
            frame, tree = self._scrolled_tree(self.notebook, ("size", "share", "count"))
            self.types_tree = tree
            for col, label, width, anchor in (
                ("#0", "Category / extension", 260, "w"),
                ("size", "Size", 100, "e"),
                ("share", "% of scanned", 180, "w"),
                ("count", "Files", 100, "e"),
            ):
                tree.heading(col, text=label)
                tree.column(col, width=width, anchor=anchor, stretch=(col == "#0"))
            self.notebook.add(frame, text="File types")

        # ---------- context menu ----------

        def _bind_context_menu(self, widget, get_node, canvas=False):
            def popup(event):
                if not canvas:
                    iid = widget.identify_row(event.y)
                    if not iid:
                        return
                    widget.selection_set(iid)
                    widget.focus(iid)
                    node = get_node(iid)
                else:
                    node = get_node(None)
                if node is None:
                    return
                self.menu_node = node
                self.menu.tk_popup(event.x_root, event.y_root)

            widget.bind("<Button-2>", popup)  # right-click on macOS
            widget.bind("<Button-3>", popup)
            widget.bind("<Control-Button-1>", popup)

        def _menu_action(self, action):
            node = self.menu_node
            if node is None:
                return
            path = node.path()
            if action == "reveal":
                subprocess.run(["open", "-R", path])
            elif action == "copy":
                self.win.clipboard_clear()
                self.win.clipboard_append(path)
                self.status_var.set(f"Copied: {path}")
            elif action == "show":
                self._show_in_explorer(node)

        # ---------- scanning ----------

        def choose_folder(self):
            path = filedialog.askdirectory(initialdir=self.path_var.get() or HOME)
            if path:
                self.path_var.set(path)

        def start_scan(self):
            if self.scanner and not self.scanner.done:
                return
            path = os.path.expanduser(self.path_var.get().strip() or HOME)
            if not os.path.isdir(path):
                messagebox.showerror("FileCleaner", f"Not a folder:\n{path}")
                return
            self.scanner = Scanner(path)
            threading.Thread(target=self.scanner.run, daemon=True).start()
            self.scan_btn.configure(state="disabled")
            self.stop_btn.configure(state="normal")
            self.spinner.start(12)
            self._poll_scan()

        def stop_scan(self):
            if self.scanner:
                self.scanner.cancel()

        def _poll_scan(self):
            s = self.scanner
            if not s.done:
                current = s.current
                if len(current) > 80:
                    current = "..." + current[-77:]
                self.status_var.set(f"Scanning... {s.files_seen:,} files, {human(s.bytes_seen)}   {current}")
                self.win.after(150, self._poll_scan)
                return
            self.spinner.stop()
            self.scan_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")
            if s.error:
                messagebox.showerror("FileCleaner", f"Scan failed:\n{s.error}")
                self.status_var.set("Scan failed.")
                return
            self._show_results(s)

        def _show_results(self, s):
            self.root_node = s.root
            self._fill_tree()
            self._fill_largest(s)
            self._fill_types(s)
            self.set_map_node(s.root)
            msg = f"{'Stopped early - partial results. ' if s.cancelled else ''}" \
                  f"Scanned {s.files_seen:,} files using {human(s.root.size)}."
            if s.unreadable:
                msg += (f"  {len(s.unreadable):,} folders couldn't be read (red 'Permission denied'). "
                        "Grant Full Disk Access to see them.")
            self.status_var.set(msg)

        # ---------- explorer tree ----------

        def _fill_tree(self):
            self.tree.delete(*self.tree.get_children())
            self.tree_nodes.clear()
            root_iid = self._insert_tree_row("", self.root_node, self.root_node.size)
            self._load_children(root_iid)
            self.tree.item(root_iid, open=True)
            self.tree.selection_set(root_iid)

        def _insert_tree_row(self, parent_iid, node, parent_size):
            iid = f"n{id(node)}"
            self.tree_nodes[iid] = node
            level, reason = classify(node.path())
            safety = f"{level.upper()}: {reason}" if level else ""
            if node.note:
                safety = f"{node.note}  {safety}".strip()
            tags = (level,) if level else ()
            if node.note == "Permission denied":
                tags = (PROTECTED,)
            share = node.size / parent_size if parent_size else 0
            bar = "█" * round(share * 10) + "░" * (10 - round(share * 10))
            name = node.name + ("/" if node.is_dir and node.parent is not None else "")
            self.tree.insert(parent_iid, "end", iid=iid, text=name, tags=tags, values=(
                human(node.size),
                f"{bar} {share * 100:5.1f}%",
                f"{node.count:,}" if node.is_dir else "",
                fmt_date(node.mtime),
                safety,
            ))
            if node.is_dir and node.children:
                self.tree.insert(iid, "end", iid=iid + ":placeholder", text="Loading...")
            return iid

        def _load_children(self, iid):
            placeholder = iid + ":placeholder"
            if not self.tree.exists(placeholder):
                return
            self.tree.delete(placeholder)
            node = self.tree_nodes[iid]
            for child in node.children[:MAX_TREE_CHILDREN]:
                self._insert_tree_row(iid, child, node.size)
            rest = node.children[MAX_TREE_CHILDREN:]
            if rest:
                self.tree.insert(iid, "end", text=f"... {len(rest):,} smaller items",
                                 values=(human(sum(c.size for c in rest)), "", "", "", ""), tags=("muted",))

        def on_tree_open(self, _event):
            self._load_children(self.tree.focus())

        def on_tree_select(self, _event):
            if self.ignore_next_select:
                self.ignore_next_select = False
                return
            sel = self.tree.selection()
            node = self.tree_nodes.get(sel[0]) if sel else None
            if node is None:
                return
            target = node if node.is_dir else node.parent
            if target is not None and target is not self.map_node:
                self.set_map_node(target, sync_tree=False)

        def _reveal_in_tree(self, node):
            """Expand the tree down to node and select it."""
            chain = []
            n = node
            while n is not None:
                chain.append(n)
                n = n.parent
            for ancestor in reversed(chain[1:]):
                iid = f"n{id(ancestor)}"
                self._load_children(iid)
                self.tree.item(iid, open=True)
            iid = f"n{id(node)}"
            if self.tree.exists(iid):
                self.tree.selection_set(iid)
                self.tree.focus(iid)
                self.tree.see(iid)

        def _show_in_explorer(self, node):
            if node is None:
                return
            self.notebook.select(0)
            self._reveal_in_tree(node)

        # ---------- treemap ----------

        def set_map_node(self, node, sync_tree=True):
            self.map_node = node
            self.map_title.set(f"{node.path()}   ({human(node.size)})")
            self.draw_map()
            if sync_tree:
                self._reveal_in_tree(node)

        def map_up(self):
            if self.map_node is not None and self.map_node.parent is not None:
                self.set_map_node(self.map_node.parent)

        def _color_for(self, node):
            if node is None:
                return CATEGORY_COLORS["Other"]
            level, _ = classify(node.path())
            if level:
                return SAFETY_COLORS[level]
            return CATEGORY_COLORS["Folder"] if node.is_dir else CATEGORY_COLORS[category_of(node.name)]

        def draw_map(self):
            c = self.canvas
            c.delete("all")
            self.map_items.clear()
            node = self.map_node
            if node is None or not node.is_dir:
                return
            width, height = c.winfo_width(), c.winfo_height()
            items = [ch for ch in node.children if ch.size > 0]
            shown, rest = items[:MAX_MAP_ITEMS], items[MAX_MAP_ITEMS:]
            entries = list(shown)
            sizes = [n.size for n in shown]
            if rest:
                entries.append(None)
                sizes.append(sum(n.size for n in rest))
            if not entries:
                c.create_text(width / 2, height / 2, text="(empty)", fill="white")
                return
            for i, (entry, (x, y, w, h)) in enumerate(zip(entries, squarify(sizes, 0, 0, width, height))):
                tag = f"item{i}"
                self.map_items[tag] = entry
                c.create_rectangle(x + 1, y + 1, x + w - 1, y + h - 1, fill=self._color_for(entry),
                                   outline="#20242a", width=2, tags=(tag,))
                if w > 50 and h > 28:
                    label = entry.name if entry else f"{len(rest):,} smaller items"
                    max_chars = max(3, int(w / 7))
                    if len(label) > max_chars:
                        label = label[:max_chars - 1] + "…"
                    c.create_text(x + 6, y + 5, anchor="nw", fill="#111111", tags=(tag,),
                                  text=f"{label}\n{human(sizes[i])}", font=("Helvetica", 11))

        def _map_node_under_cursor(self):
            tags = self.canvas.gettags("current")
            tag = next((t for t in tags if t.startswith("item")), None)
            return self.map_items.get(tag) if tag else None

        def on_map_hover(self, _event):
            node = self._map_node_under_cursor()
            if node is None:
                return
            level, reason = classify(node.path())
            parts = [node.path(), human(node.size)]
            if node.is_dir:
                parts.append(f"{node.count:,} files")
            if level:
                parts.append(f"{level.upper()}: {reason}")
            self.hover_var.set("   |   ".join(parts))

        def on_map_click(self, _event):
            node = self._map_node_under_cursor()
            if node is None:
                return
            # Selecting in the tree normally re-targets the map; for a single
            # click on the map, highlight the row but keep the map where it is.
            if self.tree.selection() != (f"n{id(node)}",):
                self.ignore_next_select = True
            self._reveal_in_tree(node)

        def on_map_double_click(self, _event):
            node = self._map_node_under_cursor()
            if node is not None and node.is_dir and node.children:
                self.set_map_node(node)

        # ---------- other tabs ----------

        def _fill_largest(self, s):
            tree = self.largest_tree
            tree.delete(*tree.get_children())
            self.list_nodes.clear()
            for node in s.largest_files():
                path = node.path()
                level, reason = classify(path)
                iid = tree.insert("", "end", tags=(level,) if level else (), values=(
                    human(node.size), fmt_date(node.mtime),
                    f"{level.upper()}: {reason}" if level else "", path))
                self.list_nodes[iid] = node

        def _fill_types(self, s):
            tree = self.types_tree
            tree.delete(*tree.get_children())
            total = sum(v[0] for v in s.ext_stats.values()) or 1
            by_cat = {}
            for ext, (size, count) in s.ext_stats.items():
                by_cat.setdefault(EXT_TO_CATEGORY.get(ext, "Other"), []).append((size, count, ext))

            def bar(share):
                n = round(share * 12)
                return "█" * n + "░" * (12 - n) + f" {share * 100:5.1f}%"

            cats = sorted(by_cat.items(), key=lambda kv: sum(x[0] for x in kv[1]), reverse=True)
            for cat, exts in cats:
                cat_size = sum(x[0] for x in exts)
                cat_count = sum(x[1] for x in exts)
                parent = tree.insert("", "end", text=cat,
                                     values=(human(cat_size), bar(cat_size / total), f"{cat_count:,}"))
                for size, count, ext in sorted(exts, reverse=True)[:200]:
                    label = f".{ext}" if ext != "(none)" else ext
                    tree.insert(parent, "end", text=label,
                                values=(human(size), bar(size / total), f"{count:,}"))

        def on_close(self):
            if self.scanner:
                self.scanner.cancel()
            self.win.destroy()

    win = tk.Tk()
    app = App(win)
    win.after(200, app.start_scan)
    win.mainloop()


# --------------------------------------------------------------------------
# Text report (no GUI)
# --------------------------------------------------------------------------

def run_report(path, top=15):
    scanner = Scanner(path)
    print(f"Scanning {scanner.root_path} (read-only)...", file=sys.stderr)
    scanner.run()
    if scanner.error:
        sys.exit(f"Scan failed: {scanner.error}")
    root = scanner.root

    def safety(node):
        level, reason = classify(node.path())
        return f"  [{level.upper()}: {reason}]" if level else ""

    print(f"\n{root.path()}: {human(root.size)} in {root.count:,} files\n")
    print(f"Biggest items in this folder:")
    for child in root.children[:top]:
        kind = "dir " if child.is_dir else "file"
        print(f"  {human(child.size):>10}  {kind}  {child.name}{safety(child)}")
    print(f"\nLargest files:")
    for node in scanner.largest_files()[:top]:
        print(f"  {human(node.size):>10}  {node.path()}{safety(node)}")
    print(f"\nBiggest file types:")
    for ext, (size, count) in sorted(scanner.ext_stats.items(), key=lambda kv: kv[1][0], reverse=True)[:top]:
        print(f"  {human(size):>10}  {count:>8,} files  .{ext}")
    if scanner.unreadable:
        print(f"\n{len(scanner.unreadable):,} folders couldn't be read (need Full Disk Access).")


def main():
    parser = argparse.ArgumentParser(description="Visualize disk usage on your Mac (read-only).")
    parser.add_argument("path", nargs="?", default=HOME, help="folder to scan (default: your home folder)")
    parser.add_argument("--report", action="store_true", help="print a text summary instead of opening the GUI")
    args = parser.parse_args()
    if args.report:
        run_report(args.path)
    else:
        run_gui(os.path.expanduser(args.path))


if __name__ == "__main__":
    main()
