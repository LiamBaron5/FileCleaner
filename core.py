"""
Shared building blocks for FileCleaner: safety rules, file categories,
formatting helpers, and the read-only disk scanner.

Nothing in this module modifies files.
"""

import heapq
import itertools
import os
import stat
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

