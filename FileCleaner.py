#!/usr/bin/env python3
"""
FileCleaner - understand and tidy up the files on your Mac.

Features:
  Screener       Sorts everything in Downloads and Desktop into groups
                 (installers, duplicates, screenshots, school work, ...) and
                 suggests what to do with each file. Nothing changes until you
                 press "Apply suggestions...", and "Undo last apply" puts a
                 whole batch back. Trashed files go to the macOS Trash.
  Disk Explorer  Shows what is taking up space anywhere on your Mac, as a
                 folder tree and a treemap. Read-only.

Every path is also classified by how safe it would be to touch:
  PROTECTED - part of macOS itself; never delete or move.
  CAUTION   - app data, settings, installed apps, or developer tools.
  (blank)   - ordinary user files.

Usage:
  python3 FileCleaner.py                    # open the app
  python3 FileCleaner.py --screen           # print the screener's suggestions
  python3 FileCleaner.py --screen --json suggestions.json
  python3 FileCleaner.py ~/Downloads --report   # print a disk-usage summary

Tip: macOS asks before letting apps read Desktop, Documents and Downloads.
Click Allow. If you clicked "Don't Allow" earlier, turn access back on in
System Settings > Privacy & Security > Files and Folders for the app you run
this from (Terminal / VS Code).
"""

import argparse
import hashlib
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from core import (CATEGORY_COLORS, CAUTION, HOME, PROTECTED, SAFETY_COLORS, Scanner,
                  category_of, classify, fmt_date, human, EXT_TO_CATEGORY)
import actions
import screener as scr


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
    PREVIEW_SIZE = 420

    class App:
        def __init__(self, win):
            self.win = win
            # Disk Explorer state
            self.scanner = None
            self.root_node = None
            self.tree_nodes = {}  # tree iid -> Node
            self.map_node = None
            self.map_items = {}  # canvas tag -> Node (or None for "smaller items")
            self.list_nodes = {}  # largest-files iid -> Node
            self.ignore_next_select = False
            # Screener state
            self.screener = None
            self.screen_items = {}  # screener iid -> Item
            self.thumb_dir = tempfile.mkdtemp(prefix="filecleaner-previews-")
            self.thumbs = {}  # path -> PNG path ("" if no preview)
            self.thumb_queue = queue.Queue()
            self.preview_path = None
            self.preview_image = None  # keep a reference so Tk doesn't discard it
            self.original = {}  # path -> (action, destination) the screener suggested
            self.choices = {}  # path -> "trash" / "leave" / "suggested", kept across rescans
            self.batch = None

            win.title("FileCleaner")
            win.geometry("1350x850")
            win.protocol("WM_DELETE_WINDOW", self.on_close)
            self._build_ui(start_path)
            self._poll_thumbs()

        # ---------- layout ----------

        def _build_ui(self, start_path):
            self.status_var = tk.StringVar(value="Nothing changes until you press Apply.")
            ttk.Label(self.win, textvariable=self.status_var, anchor="w", padding=(8, 4)).pack(side="bottom", fill="x")

            self.main_tabs = ttk.Notebook(self.win)
            self.main_tabs.pack(fill="both", expand=True, padx=8, pady=(8, 4))
            self._build_screener_tab()
            self._build_disk_tab(start_path)

            self.menu = tk.Menu(self.win, tearoff=0)
            self.menu.add_command(label="Reveal in Finder", command=lambda: self._menu_action("reveal"))
            self.menu.add_command(label="Copy path", command=lambda: self._menu_action("copy"))
            self.menu.add_command(label="Show in Explorer", command=lambda: self._menu_action("show"))
            self.menu_node = None

            self.screen_menu = tk.Menu(self.win, tearoff=0)
            self.screen_menu.add_command(label="Quick Look", command=lambda: self._screen_action("quicklook"))
            self.screen_menu.add_command(label="Reveal in Finder", command=lambda: self._screen_action("reveal"))
            self.screen_menu.add_command(label="Copy path", command=lambda: self._screen_action("copy"))
            self.screen_menu.add_separator()
            self.screen_menu.add_command(label="Trash this file  (T)", command=lambda: self.set_choice("trash"))
            self.screen_menu.add_command(label="Don't change this file  (L)", command=lambda: self.set_choice("leave"))
            self.screen_menu.add_command(label="Back to the suggestion  (S)", command=lambda: self.set_choice("suggested"))

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

        def _setup_columns(self, tree, specs, stretch):
            for col, label, width, anchor in specs:
                tree.heading(col, text=label)
                tree.column(col, width=width, anchor=anchor, stretch=(col in stretch))

        # ======================================================================
        # Screener tab
        # ======================================================================

        def _build_screener_tab(self):
            tab = ttk.Frame(self.main_tabs, padding=(0, 6, 0, 0))
            self.main_tabs.add(tab, text="Screener")

            bar = ttk.Frame(tab)
            bar.pack(fill="x", pady=(0, 6))
            self.screen_btn = ttk.Button(bar, text="Rescan Downloads & Desktop", command=self.start_screen)
            self.screen_btn.pack(side="left")
            self.screen_spinner = ttk.Progressbar(bar, mode="indeterminate", length=100)
            self.screen_spinner.pack(side="left", padx=(10, 0))
            self.screen_summary = tk.StringVar()
            ttk.Label(bar, textvariable=self.screen_summary, font=("Helvetica", 13, "bold")).pack(side="left", padx=12)
            self.undo_btn = ttk.Button(bar, text="Undo last apply", command=self.undo_last_apply)
            self.undo_btn.pack(side="right")
            self.apply_btn = ttk.Button(bar, text="Apply suggestions...", command=self.open_apply_dialog,
                                        state="disabled")
            self.apply_btn.pack(side="right", padx=6)
            self._update_undo_button()

            paned = ttk.PanedWindow(tab, orient="horizontal")
            paned.pack(fill="both", expand=True)

            frame, tree = self._scrolled_tree(paned, ("size", "added", "action", "why"))
            self.screen_tree = tree
            self._setup_columns(tree, (
                ("#0", "Name", 300, "w"),
                ("size", "Size", 85, "e"),
                ("added", "Added", 90, "center"),
                ("action", "Suggestion", 250, "w"),
                ("why", "Why", 300, "w"),
            ), stretch=("#0", "why"))
            tree.tag_configure("group", font=("Helvetica", 13, "bold"))
            tree.bind("<<TreeviewSelect>>", self.on_screen_select)
            tree.bind("<Double-Button-1>", lambda e: self._screen_action("quicklook"))
            tree.bind("<space>", lambda e: self._screen_action("quicklook"))
            for key, choice in (("t", "trash"), ("l", "leave"), ("s", "suggested")):
                tree.bind(f"<KeyPress-{key}>", lambda e, c=choice: self.set_choice(c))
                tree.bind(f"<KeyPress-{key.upper()}>", lambda e, c=choice: self.set_choice(c))
            for seq in ("<Button-2>", "<Button-3>", "<Control-Button-1>"):
                tree.bind(seq, self._screen_popup)
            paned.add(frame, weight=3)

            detail = ttk.Frame(paned, padding=(12, 0, 0, 0))
            box = ttk.Frame(detail, width=PREVIEW_SIZE, height=PREVIEW_SIZE)
            box.pack_propagate(False)
            box.pack(fill="x")
            self.preview_label = ttk.Label(box, anchor="center", foreground="gray")
            self.preview_label.pack(fill="both", expand=True)

            self.detail_title = tk.StringVar()
            ttk.Label(detail, textvariable=self.detail_title, font=("Helvetica", 15, "bold"),
                      wraplength=PREVIEW_SIZE).pack(anchor="w", pady=(8, 6))
            fields = ttk.Frame(detail)
            fields.pack(fill="x")
            self.detail_vars = {}
            for row, key in enumerate(("Suggestion", "Why", "Location", "Size", "Added",
                                       "Last opened", "Downloaded from")):
                ttk.Label(fields, text=key + ":", foreground="gray").grid(row=row, column=0, sticky="nw", padx=(0, 8), pady=1)
                var = tk.StringVar()
                ttk.Label(fields, textvariable=var, wraplength=PREVIEW_SIZE - 110).grid(row=row, column=1, sticky="w", pady=1)
                self.detail_vars[key] = var

            buttons = ttk.Frame(detail)
            buttons.pack(fill="x", pady=(10, 0))
            self.ql_btn = ttk.Button(buttons, text="Quick Look (space)", command=lambda: self._screen_action("quicklook"))
            self.ql_btn.pack(side="left")
            self.reveal_btn = ttk.Button(buttons, text="Reveal in Finder", command=lambda: self._screen_action("reveal"))
            self.reveal_btn.pack(side="left", padx=6)
            paned.add(detail, weight=2)
            self._show_item_details(None)

        # ---------- screening ----------

        def start_screen(self):
            if self.screener and not self.screener.done:
                return
            self.screener = scr.Screener()
            threading.Thread(target=self.screener.run, daemon=True).start()
            self.screen_btn.configure(state="disabled")
            self.apply_btn.configure(state="disabled")
            self.screen_spinner.start(12)
            self.screen_summary.set("Screening...")
            self._poll_screen()

        def _poll_screen(self):
            s = self.screener
            if not s.done:
                current = scr.short_path(s.current)
                if len(current) > 80:
                    current = "..." + current[-77:]
                self.status_var.set(f"Screening: {s.stage}... {len(s.items):,} files found   {current}")
                self.win.after(150, self._poll_screen)
                return
            self.screen_spinner.stop()
            self.screen_btn.configure(state="normal")
            if s.error:
                messagebox.showerror("FileCleaner", f"Screening failed:\n{s.error}")
                self.status_var.set("Screening failed.")
                self.screen_summary.set("")
                return
            self._fill_screen_tree(s)
            self.apply_btn.configure(state="normal")
            if s.blocked:
                names = " and ".join(os.path.basename(p) for p in s.blocked)
                self.status_var.set(
                    f"macOS blocked access to {names}. Allow it in System Settings > Privacy & Security > "
                    "Files and Folders (for Terminal / VS Code), then press Rescan.")
            else:
                self.status_var.set("Screening finished. Select a file to preview it. Keys: T = trash, "
                                    "L = don't change, S = back to suggestion. Nothing changes until you press Apply.")

        def _fill_screen_tree(self, s):
            tree = self.screen_tree
            tree.delete(*tree.get_children())
            self.screen_items.clear()
            self.original = {i.path: (i.action, i.destination) for i in s.items}
            for item in s.items:
                if item.path in self.choices:
                    self._apply_choice(item, self.choices[item.path])
            self._update_summary()

            for key, title, blurb, items in s.grouped():
                actions = {i.action for i in items}
                if actions == {scr.TRASH}:
                    group_action = "Trash"
                elif key == "screenshot":
                    group_action = "Trash or archive (you choose)"
                elif actions == {scr.MOVE}:
                    group_action = "Move to folders"
                elif actions == {scr.LEAVE}:
                    group_action = "Leave"
                else:
                    group_action = "Mixed"
                group_iid = tree.insert("", "end", text=f"{title}  ({len(items):,})", tags=("group",),
                                        open=key not in ("other", "big_old"),
                                        values=(human(sum(i.size for i in items)), "", group_action, blurb))
                self.screen_items[group_iid] = (title, blurb, items)
                for item in items:
                    iid = tree.insert(group_iid, "end", text=item.name)
                    self.screen_items[iid] = item
                    self._refresh_row(iid)
            self._show_item_details(None)

        # ---------- details & preview ----------

        def on_screen_select(self, _event):
            sel = self.screen_tree.selection()
            self._show_item_details(self.screen_items.get(sel[0]) if sel else None)

        def _selected_screen_item(self):
            sel = self.screen_tree.selection()
            item = self.screen_items.get(sel[0]) if sel else None
            return item if isinstance(item, scr.Item) else None

        def _show_item_details(self, item):
            for var in self.detail_vars.values():
                var.set("")
            self.preview_path = None
            self.preview_image = None
            self.preview_label.configure(image="", text="")
            if isinstance(item, tuple):  # a group row
                title, blurb, items = item
                self.detail_title.set(title)
                self.detail_vars["Why"].set(blurb)
                self.detail_vars["Size"].set(f"{human(sum(i.size for i in items))} in {len(items):,} file{'s' if len(items) != 1 else ''}")
                self.preview_label.configure(text="Expand the group and select a file to preview it.")
                state = "disabled"
            elif item is None:
                self.detail_title.set("")
                self.preview_label.configure(text="Select a file to preview it.")
                state = "disabled"
            else:
                self.detail_title.set(item.name)
                self.detail_vars["Suggestion"].set(self._action_text(item))
                self.detail_vars["Why"].set("; ".join(item.reasons))
                self.detail_vars["Location"].set(scr.short_path(os.path.dirname(item.path)))
                self.detail_vars["Size"].set(human(item.size))
                self.detail_vars["Added"].set(fmt_date(item.added))
                self.detail_vars["Last opened"].set(fmt_date(item.last_used))
                self.detail_vars["Downloaded from"].set(item.where_from[0] if item.where_from else "(not recorded)")
                self._request_preview(item.path)
                state = "normal"
            self.ql_btn.configure(state=state)
            self.reveal_btn.configure(state=state)

        def _request_preview(self, path):
            self.preview_path = path
            if path in self.thumbs:
                self._display_thumb(path)
                return
            self.preview_label.configure(text="Loading preview...")
            threading.Thread(target=self._make_thumb, args=(path,), daemon=True).start()

        def _make_thumb(self, path):
            """Ask macOS Quick Look for a preview image (runs in the background)."""
            out = os.path.join(self.thumb_dir, hashlib.md5(path.encode()).hexdigest())
            png = ""
            try:
                os.makedirs(out, exist_ok=True)
                subprocess.run(["qlmanage", "-t", "-s", str(PREVIEW_SIZE), "-o", out, path],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
                pngs = [f for f in os.listdir(out) if f.endswith(".png")]
                png = os.path.join(out, pngs[0]) if pngs else ""
            except (OSError, subprocess.TimeoutExpired):
                pass
            self.thumb_queue.put((path, png))

        def _poll_thumbs(self):
            try:
                while True:
                    path, png = self.thumb_queue.get_nowait()
                    self.thumbs[path] = png
                    if path == self.preview_path:
                        self._display_thumb(path)
            except queue.Empty:
                pass
            self.win.after(100, self._poll_thumbs)

        def _display_thumb(self, path):
            png = self.thumbs.get(path)
            if not png:
                self.preview_label.configure(image="", text="No preview available.")
                return
            try:
                image = tk.PhotoImage(file=png)
            except tk.TclError:
                self.preview_label.configure(image="", text="No preview available.")
                return
            factor = max(1, -(-max(image.width(), image.height()) // PREVIEW_SIZE))
            if factor > 1:
                image = image.subsample(factor)
            self.preview_image = image
            self.preview_label.configure(image=image, text="")

        # ---------- your choices ----------

        def _apply_choice(self, item, choice):
            action, destination = self.original.get(item.path, (item.action, item.destination))
            item.excluded = choice == "leave"
            if choice == "trash":
                item.action, item.destination = scr.TRASH, ""
            else:
                item.action, item.destination = action, destination

        def _action_text(self, item):
            if item.excluded:
                return "Leave (your choice)"
            text = scr.describe_action(item)
            if (item.action, item.destination) != self.original.get(item.path, (item.action, item.destination)):
                text += " (your choice)"
            return text

        def _refresh_row(self, iid):
            item = self.screen_items[iid]
            leave = item.excluded or item.action == scr.LEAVE
            self.screen_tree.item(iid, tags=("muted",) if leave else (), values=(
                human(item.size), fmt_date(item.added), self._action_text(item), "; ".join(item.reasons)))

        def set_choice(self, choice):
            sel = self.screen_tree.selection()
            item = self.screen_items.get(sel[0]) if sel else None
            if not isinstance(item, scr.Item) or (self.batch and not self.batch.finished):
                return "break"
            if choice == "suggested":
                self.choices.pop(item.path, None)
            else:
                self.choices[item.path] = choice
            self._apply_choice(item, choice)
            self._refresh_row(sel[0])
            self._update_summary()
            # Jump to the next file so you can go through a group with the keyboard.
            following = self.screen_tree.next(sel[0])
            if following:
                self.screen_tree.selection_set(following)
                self.screen_tree.focus(following)
                self.screen_tree.see(following)
            else:
                self._show_item_details(item)
            return "break"

        def _pending(self):
            s = self.screener
            if not s:
                return []
            return [i for i in s.items if not i.excluded and i.action in (scr.TRASH, scr.MOVE)]

        def _update_summary(self):
            s = self.screener
            pending = self._pending()
            trash = [i for i in pending if i.action == scr.TRASH]
            move = [i for i in pending if i.action == scr.MOVE]
            self.screen_summary.set(
                f"{len(s.items):,} files ({human(sum(i.size for i in s.items))})   •   "
                f"To Trash: {len(trash):,} ({human(sum(i.size for i in trash))})   •   "
                f"To organize: {len(move):,}")

        # ---------- applying changes ----------

        def open_apply_dialog(self):
            if not self.screener or not self.screener.done or (self.batch and not self.batch.finished):
                return
            pending = self._pending()
            if not pending:
                messagebox.showinfo("FileCleaner", "There are no suggested changes to apply.")
                return
            by_group = {}
            for item in pending:
                by_group.setdefault(item.group, []).append(item)

            dlg = tk.Toplevel(self.win)
            dlg.title("Apply suggestions")
            dlg.transient(self.win)
            dlg.resizable(False, False)
            dlg.geometry(f"+{self.win.winfo_rootx() + 250}+{self.win.winfo_rooty() + 120}")
            body = ttk.Frame(dlg, padding=18)
            body.pack(fill="both", expand=True)
            ttk.Label(body, text="Apply these changes?", font=("Helvetica", 16, "bold")).pack(anchor="w")
            ttk.Label(body, wraplength=560, foreground="gray", text=(
                "Trashed files go to the macOS Trash, so you can still restore them. Nothing is "
                "overwritten, and \"Undo last apply\" puts the whole batch back.")).pack(anchor="w", pady=(4, 12))

            checks = []
            total_var = tk.StringVar()

            def chosen_items():
                return [i for var, items in checks if var.get() for i in items]

            def update_total():
                chosen = chosen_items()
                trash_bytes = sum(i.size for i in chosen if i.action == scr.TRASH)
                total_var.set(f"{len(chosen):,} files selected. Frees {human(trash_bytes)} once you empty the Trash.")
                apply_button.configure(state="normal" if chosen else "disabled")

            for key, title, _blurb in scr.GROUPS:
                items = by_group.get(key)
                if not items:
                    continue
                trash = [i for i in items if i.action == scr.TRASH]
                move = [i for i in items if i.action == scr.MOVE]
                verbs = " and ".join(p for p in (f"trash {len(trash):,}" if trash else "",
                                                 f"move {len(move):,}" if move else "") if p)
                var = tk.BooleanVar(value=key != "maybe_school")
                ttk.Checkbutton(body, variable=var, command=update_total, text=(
                    f"{title}: {verbs} file{'s' if len(items) != 1 else ''} "
                    f"({human(sum(i.size for i in items))})")).pack(anchor="w", pady=(6, 0))
                destinations = sorted({scr.short_path(i.destination) for i in move})
                if destinations:
                    shown = ", ".join(destinations[:3])
                    if len(destinations) > 3:
                        shown += f", and {len(destinations) - 3} more"
                    ttk.Label(body, text=f"into {shown}", foreground="gray",
                              wraplength=530).pack(anchor="w", padx=(26, 0))
                if key == "maybe_school":
                    ttk.Label(body, foreground="#b8860b", text=(
                        "Off by default: these are uncertain guesses, so check them first.")).pack(anchor="w", padx=(26, 0))
                checks.append((var, items))

            ttk.Label(body, textvariable=total_var, font=("Helvetica", 13, "bold")).pack(anchor="w", pady=(16, 10))
            buttons = ttk.Frame(body)
            buttons.pack(fill="x")

            def do_apply():
                chosen = chosen_items()
                dlg.destroy()
                if chosen:
                    self.start_batch(chosen)

            apply_button = ttk.Button(buttons, text="Apply", command=do_apply, default="active")
            apply_button.pack(side="right")
            ttk.Button(buttons, text="Cancel", command=dlg.destroy).pack(side="right", padx=6)
            dlg.bind("<Escape>", lambda e: dlg.destroy())
            update_total()
            dlg.grab_set()
            apply_button.focus_set()

        def start_batch(self, items):
            self.batch = actions.Batch(items)
            threading.Thread(target=self.batch.run, daemon=True).start()
            for button in (self.apply_btn, self.undo_btn, self.screen_btn):
                button.configure(state="disabled")
            self.screen_spinner.start(12)
            self._poll_batch()

        def _poll_batch(self):
            b = self.batch
            if not b.finished:
                self.status_var.set(f"Applying... {b.done_count:,} of {len(b.items):,}")
                self.win.after(100, self._poll_batch)
                return
            self.screen_spinner.stop()
            self.screen_btn.configure(state="normal")

            def files(n):
                return f"{n:,} file{'s' if n != 1 else ''}"

            lines = []
            if b.moved:
                lines.append(f"Moved {files(len(b.moved))} into folders.")
            if b.trashed:
                lines.append(f"Moved {files(len(b.trashed))} to the Trash "
                             f"({human(sum(i.size for i in b.trashed))}).")
            if b.skipped:
                lines.append(f"\nSkipped {files(len(b.skipped))}:")
                lines += [f"  {i.name}: {why}" for i, why in b.skipped[:12]]
                if len(b.skipped) > 12:
                    lines.append(f"  ...and {len(b.skipped) - 12:,} more")
            messagebox.showinfo("FileCleaner", "\n".join(lines) or "Nothing was changed.")
            for item in b.moved + b.trashed:
                self.choices.pop(item.path, None)
            self._update_undo_button()
            self.start_screen()

        def _update_undo_button(self):
            batch, _entries = actions.last_batch()
            self.undo_btn.configure(state="normal" if batch else "disabled")

        def undo_last_apply(self):
            if self.batch and not self.batch.finished:
                return
            batch, entries = actions.last_batch()
            if not batch:
                return
            count = sum(1 for e in entries if e.get("op") in ("trash", "move"))
            when = time.strftime("%b %d at %I:%M %p", time.strptime(batch[:15], "%Y%m%d-%H%M%S"))
            if not messagebox.askyesno("Undo last apply",
                                       f"Put the {count:,} files from the batch applied {when} back where they were?"):
                return
            restored, problems = actions.undo_last()
            message = f"Put back {restored:,} file{'s' if restored != 1 else ''}."
            if problems:
                message += f"\n\nCouldn't put back {len(problems):,}:\n" + "\n".join(problems[:12])
            messagebox.showinfo("FileCleaner", message)
            self._update_undo_button()
            self.start_screen()

        # ---------- viewing (non-destructive) ----------

        def _screen_popup(self, event):
            iid = self.screen_tree.identify_row(event.y)
            if not iid or not isinstance(self.screen_items.get(iid), scr.Item):
                return
            self.screen_tree.selection_set(iid)
            self.screen_tree.focus(iid)
            self.screen_menu.tk_popup(event.x_root, event.y_root)

        def _screen_action(self, action):
            item = self._selected_screen_item()
            if item is None:
                return
            if action == "quicklook":
                subprocess.Popen(["qlmanage", "-p", item.path],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elif action == "reveal":
                subprocess.run(["open", "-R", item.path])
            elif action == "copy":
                self.win.clipboard_clear()
                self.win.clipboard_append(item.path)
                self.status_var.set(f"Copied: {item.path}")

        # ======================================================================
        # Disk Explorer tab
        # ======================================================================

        def _build_disk_tab(self, start_path):
            tab = ttk.Frame(self.main_tabs, padding=(0, 6, 0, 0))
            self.main_tabs.add(tab, text="Disk Explorer")

            top = ttk.Frame(tab)
            top.pack(fill="x", pady=(0, 6))
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

            self.explorer_tabs = ttk.Notebook(tab)
            self.explorer_tabs.pack(fill="both", expand=True)
            self._build_explorer_tab()
            self._build_largest_tab()
            self._build_types_tab()

        def _build_explorer_tab(self):
            paned = ttk.PanedWindow(self.explorer_tabs, orient="horizontal")
            self.explorer_tabs.add(paned, text="Folders")

            frame, tree = self._scrolled_tree(paned, ("size", "share", "items", "modified", "safety"))
            self.tree = tree
            self._setup_columns(tree, (
                ("#0", "Name", 280, "w"),
                ("size", "Size", 90, "e"),
                ("share", "% of parent", 150, "w"),
                ("items", "Files", 90, "e"),
                ("modified", "Modified", 90, "center"),
                ("safety", "Safety", 200, "w"),
            ), stretch=("#0", "safety"))
            tree.bind("<<TreeviewOpen>>", self.on_tree_open)
            tree.bind("<<TreeviewSelect>>", self.on_tree_select)
            self._bind_context_menu(tree, lambda iid: self.tree_nodes.get(iid))
            paned.add(frame, weight=3)

            right = ttk.Frame(paned)
            bar = ttk.Frame(right)
            bar.pack(fill="x", pady=(0, 4))
            ttk.Button(bar, text="Up", width=4, command=self.map_up).pack(side="left")
            self.map_title = tk.StringVar(value="Press Scan to map a folder.")
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
            frame, tree = self._scrolled_tree(self.explorer_tabs, ("size", "modified", "safety", "path"), show="headings")
            self.largest_tree = tree
            self._setup_columns(tree, (
                ("size", "Size", 90, "e"),
                ("modified", "Modified", 90, "center"),
                ("safety", "Safety", 200, "w"),
                ("path", "Path", 700, "w"),
            ), stretch=("path",))
            tree.bind("<Double-Button-1>", lambda e: self._show_in_explorer(self.list_nodes.get(tree.focus())))
            self._bind_context_menu(tree, lambda iid: self.list_nodes.get(iid))
            self.explorer_tabs.add(frame, text="Largest files")

        def _build_types_tab(self):
            frame, tree = self._scrolled_tree(self.explorer_tabs, ("size", "share", "count"))
            self.types_tree = tree
            self._setup_columns(tree, (
                ("#0", "Category / extension", 260, "w"),
                ("size", "Size", 100, "e"),
                ("share", "% of scanned", 180, "w"),
                ("count", "Files", 100, "e"),
            ), stretch=("#0",))
            self.explorer_tabs.add(frame, text="File types")

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
            self.main_tabs.select(1)
            self.explorer_tabs.select(0)
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

        # ---------- other explorer tabs ----------

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
            for job in (self.scanner, self.screener):
                if job:
                    job.cancel()
            shutil.rmtree(self.thumb_dir, ignore_errors=True)
            self.win.destroy()

    win = tk.Tk()
    app = App(win)
    win.after(200, app.start_screen)
    win.mainloop()


# --------------------------------------------------------------------------
# Text reports (no GUI)
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
    print("Biggest items in this folder:")
    for child in root.children[:top]:
        kind = "dir " if child.is_dir else "file"
        print(f"  {human(child.size):>10}  {kind}  {child.name}{safety(child)}")
    print("\nLargest files:")
    for node in scanner.largest_files()[:top]:
        print(f"  {human(node.size):>10}  {node.path()}{safety(node)}")
    print("\nBiggest file types:")
    for ext, (size, count) in sorted(scanner.ext_stats.items(), key=lambda kv: kv[1][0], reverse=True)[:top]:
        print(f"  {human(size):>10}  {count:>8,} files  .{ext}")
    if scanner.unreadable:
        print(f"\n{len(scanner.unreadable):,} folders couldn't be read (need Full Disk Access).")


def run_screen_report(json_path=None, per_group=10):
    s = scr.Screener()
    print("Screening Downloads and Desktop (read-only)...", file=sys.stderr)
    s.run()
    if s.error:
        sys.exit(f"Screening failed: {s.error}")
    for folder in s.blocked:
        print(f"macOS blocked access to {folder} - allow it in System Settings > "
              "Privacy & Security > Files and Folders.", file=sys.stderr)

    info = s.summary()
    print(f"\n{info['files']:,} files ({human(info['bytes'])}). "
          f"Suggested for Trash: {info['trash_count']:,} ({human(info['trash_bytes'])}). "
          f"To organize: {info['move_count']:,}.")
    for _key, title, blurb, items in s.grouped():
        print(f"\n== {title}: {len(items):,} files, {human(sum(i.size for i in items))} ==  ({blurb})")
        for item in items[:per_group]:
            print(f"  {human(item.size):>10}  {scr.short_path(item.path)}")
            print(f"              -> {scr.describe_action(item)}  ({'; '.join(item.reasons)})")
        if len(items) > per_group:
            print(f"  ... and {len(items) - per_group:,} more")

    if json_path:
        rows = [{
            "path": i.path, "size": i.size, "group": i.group, "action": i.action,
            "destination": i.destination, "reasons": i.reasons, "added": fmt_date(i.added),
            "last_opened": fmt_date(i.last_used), "downloaded_from": i.where_from,
        } for i in s.items]
        with open(json_path, "w") as f:
            json.dump(rows, f, indent=2)
        print(f"\nWrote {len(rows):,} suggestions to {json_path}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="Understand and tidy up files on your Mac (read-only).")
    parser.add_argument("path", nargs="?", default=HOME, help="folder for the Disk Explorer (default: home)")
    parser.add_argument("--report", action="store_true", help="print a disk-usage summary of PATH instead of opening the app")
    parser.add_argument("--screen", action="store_true", help="print the screener's suggestions instead of opening the app")
    parser.add_argument("--json", metavar="FILE", help="with --screen, also save all suggestions to a JSON file")
    args = parser.parse_args()
    if args.screen:
        run_screen_report(args.json)
    elif args.report:
        run_report(args.path)
    else:
        run_gui(os.path.expanduser(args.path))


if __name__ == "__main__":
    main()
