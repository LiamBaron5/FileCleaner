"""
Screener - looks through Downloads and Desktop and sorts every file into a
group with a suggested action: trash it, move it somewhere, or leave it.

This module is READ-ONLY. It only reads file information and contents (to
spot duplicates); it never changes anything. Acting on the suggestions is the
job of actions.py, and only happens when you press Apply.
"""

import ctypes
import ctypes.util
import hashlib
import os
import plistlib
import re
import stat
import struct
import threading
import time
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field

from core import HOME, classify, extension_of

# --------------------------------------------------------------------------
# Settings (the future rules file will be able to override these)
# --------------------------------------------------------------------------

DOWNLOADS = os.path.join(HOME, "Downloads")
DESKTOP = os.path.join(HOME, "Desktop")
DOCUMENTS = os.path.join(HOME, "Documents")

SCREEN_LOCATIONS = [DOWNLOADS, DESKTOP]

# Checked for duplicates only: a copy in Downloads of something already here
# is flagged, but files here are never suggested for anything.
REFERENCE_LOCATIONS = [DOCUMENTS]

ARCHIVED_SCREENSHOTS = os.path.join(DOWNLOADS, "Archived Screenshots")
SCHOOL_ROOT = os.path.join(DOCUMENTS, "School")

# Folders FileCleaner files things into. They are never screened, so things
# you've already put away don't get flagged again.
ORGANIZED_DIRS = [ARCHIVED_SCREENSHOTS, SCHOOL_ROOT]

# A download from any of these sites is a strong sign of school work.
SCHOOL_SITES = ("bates.edu", "instructure.com", "canvas", "blackboard", "moodle",
                "gradescope.com", "overleaf.com", "turnitin.com", "perusall.com",
                "piazza.com", "jstor.org", "edpuzzle.com")
# Weaker sign: exports from Google Docs/Drive are often, but not always, school.
MAYBE_SCHOOL_SITES = ("docs.google.com", "drive.google.com")

def _words(pattern):
    return re.compile(rf"(?<![a-z])({pattern})(?![a-z])", re.IGNORECASE)


# Words that almost always mean school work...
STRONG_SCHOOL_WORDS = _words(
    r"essay|thesis|homework|hw ?\d+|assignment|problem ?set|p ?set ?\d*|syllabus|midterm|"
    r"exam|quiz|study ?guide|lecture|worksheet|rubric|annotated bibliography|seminar|lab report")
# ...and words that often do, but also show up elsewhere.
WEAK_SCHOOL_WORDS = _words(
    r"paper|lab|notes|final|reading|response|chapter|ch ?\d+|week ?\d+|unit ?\d+|prompt|"
    r"presentation|bibliography|citation|draft|outline|project")
# Course codes like "ECON 101", "BIO_242", "FYS-123A".
COURSE_CODE = re.compile(r"(?<![A-Za-z])([A-Z]{2,4})[ _-]?(\d{3}[A-Z]?)(?!\d)")

SCHOOL_EXTENSIONS = set("pdf doc docx pages ppt pptx key xls xlsx numbers txt rtf md odt "
                        "ipynb tex rmd r csv py java c cpp m".split())
INSTALLER_EXTENSIONS = {"dmg", "pkg", "mpkg", "iso", "xip"}
INCOMPLETE_EXTENSIONS = {"crdownload", "download", "part", "partial", "opdownload"}

# Folders macOS shows as a single file. They're screened as one item.
BUNDLE_EXTENSIONS = {"app", "pages", "key", "numbers", "rtfd", "download", "bundle",
                     "photoslibrary", "fcpbundle", "imovielibrary", "logicx", "band",
                     "xcodeproj", "playground", "pkg", "mpkg", "framework", "plugin"}

SCREENSHOT_NAME = re.compile(
    r"^(Screenshot|Screen Shot|Screen Recording|CleanShot|Simulator Screen Shot)[ _]\d{4}-\d{2}-\d{2}",
    re.IGNORECASE)
COPY_SUFFIX = re.compile(r"( \(\d+\)| copy( \d+)?|-\d)$", re.IGNORECASE)

BIG_FILE = 100 * 1000 * 1000
OLD_AGE = 365 * 24 * 3600

# --------------------------------------------------------------------------
# Groups and actions
# --------------------------------------------------------------------------

TRASH = "trash"
MOVE = "move"
LEAVE = "leave"

# (key, title, one-line explanation), in the order they're shown.
GROUPS = [
    ("incomplete", "Incomplete downloads", "Downloads that never finished"),
    ("duplicate", "Duplicate copies", "Exact copies of another file"),
    ("unzipped", "Already-unzipped archives", "Zip files whose contents are already extracted next to them"),
    ("installer", "Installers", "Disk images and installer packages"),
    ("screenshot", "Screenshots & recordings", "Archived by default - press T on one to trash it instead"),
    ("school", "School work", "Likely school files, to file by year"),
    ("maybe_school", "Possibly school work", "Some signs of school work - check these"),
    ("big_old", "Big & not opened in a year", "Large files worth a look"),
    ("other", "Everything else", "No suggestion - left where they are"),
]
GROUP_TITLES = {key: title for key, title, _ in GROUPS}


@dataclass
class Item:
    path: str
    size: int
    is_dir: bool  # a bundle or project folder screened as one item
    added: float  # when it arrived in this folder
    last_used: float
    where_from: list
    is_screenshot: bool
    group: str = "other"
    action: str = LEAVE
    destination: str = ""
    reasons: list = field(default_factory=list)
    excluded: bool = False  # you chose "Don't change this file"

    @property
    def name(self):
        return os.path.basename(self.path)

    @property
    def location(self):
        for top in SCREEN_LOCATIONS:
            if self.path.startswith(top + "/"):
                return os.path.basename(top)
        return ""

    @property
    def year(self):
        return time.localtime(self.added).tm_year

    def suggest(self, group, action, reason, destination=""):
        self.group = group
        self.action = action
        self.destination = destination
        self.reasons.insert(0, reason)


def short_path(path):
    return "~" + path[len(HOME):] if path == HOME or path.startswith(HOME + "/") else path


def describe_action(item):
    if item.action == TRASH:
        return "Trash"
    if item.action == MOVE:
        return f"Move to {short_path(item.destination)}"
    return "Leave"


# --------------------------------------------------------------------------
# macOS metadata (read with system calls, so no extra installs are needed)
# --------------------------------------------------------------------------

try:
    _libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    _getxattr = _libc.getxattr
    _getxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p,
                          ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int]
    _getxattr.restype = ctypes.c_ssize_t

    class _AttrList(ctypes.Structure):
        _fields_ = [("bitmapcount", ctypes.c_ushort), ("reserved", ctypes.c_uint16),
                    ("commonattr", ctypes.c_uint32), ("volattr", ctypes.c_uint32),
                    ("dirattr", ctypes.c_uint32), ("fileattr", ctypes.c_uint32),
                    ("forkattr", ctypes.c_uint32)]

    _getattrlist = _libc.getattrlist
    _getattrlist.argtypes = [ctypes.c_char_p, ctypes.POINTER(_AttrList), ctypes.c_void_p,
                             ctypes.c_size_t, ctypes.c_uint32]
    _getattrlist.restype = ctypes.c_int
except (OSError, AttributeError):
    _libc = None

_XATTR_NOFOLLOW = 0x0001
_FSOPT_NOFOLLOW = 0x0001
_ATTR_CMN_ADDEDTIME = 0x10000000


def read_xattr(path, name):
    """Return the raw bytes of an extended attribute, or None."""
    if _libc is None:
        return None
    p, n = os.fsencode(path), name.encode()
    size = _getxattr(p, n, None, 0, 0, _XATTR_NOFOLLOW)
    if size <= 0:
        return None
    buf = ctypes.create_string_buffer(size)
    size = _getxattr(p, n, buf, size, 0, _XATTR_NOFOLLOW)
    return buf.raw[:size] if size > 0 else None


def read_plist_xattr(path, name):
    data = read_xattr(path, name)
    if not data:
        return None
    try:
        return plistlib.loads(data)
    except Exception:
        return None


def where_from(path):
    """Web addresses the file was downloaded from (recorded by the browser)."""
    value = read_plist_xattr(path, "com.apple.metadata:kMDItemWhereFroms")
    return [v for v in value if isinstance(v, str) and v] if isinstance(value, list) else []


def is_screen_capture(path):
    return read_plist_xattr(path, "com.apple.metadata:kMDItemIsScreenCapture") is True


def date_added(path):
    """When the file was put in its current folder ("Date Added" in Finder)."""
    if _libc is None:
        return None
    attrs = _AttrList(5, 0, _ATTR_CMN_ADDEDTIME, 0, 0, 0, 0)
    buf = ctypes.create_string_buffer(64)
    if _getattrlist(os.fsencode(path), ctypes.byref(attrs), buf, 64, _FSOPT_NOFOLLOW) != 0:
        return None
    length = struct.unpack_from("I", buf.raw)[0]
    if length < 20:
        return None
    seconds, _nanos = struct.unpack_from("qq", buf.raw, 4)
    return float(seconds) if seconds > 0 else None


def last_used(path):
    """When the file was last opened ("Last Opened" in Finder), if known."""
    data = read_xattr(path, "com.apple.lastuseddate#PS")
    if data and len(data) >= 16:
        seconds, _nanos = struct.unpack_from("qq", data)
        if seconds > 0:
            return float(seconds)
    return None


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _inside(path, folder):
    return path == folder or path.startswith(folder + "/")


def _is_bundle(path, name):
    ext = extension_of(name)
    return ext in BUNDLE_EXTENSIONS or os.path.isdir(os.path.join(path, ".git"))


def _folder_size(path):
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(dirpath, f)).st_size
            except OSError:
                pass
    return total


def _normalize(name):
    """'Google-Chrome_v120.3 (arm64).dmg' -> 'googlechrome' for app matching."""
    name = os.path.splitext(name)[0].lower()
    name = re.sub(r"\b(v?\d[\d.]*|mac|macos|osx|darwin|universal|arm64|x64|x86_64|intel|"
                  r"apple ?silicon|installer|install|setup|latest|release|final|stable)\b", " ", name)
    return re.sub(r"[^a-z]", "", name)


def installed_apps():
    names = set()
    for folder in ("/Applications", os.path.join(HOME, "Applications"), "/System/Applications"):
        try:
            for entry in os.scandir(folder):
                if entry.name.endswith(".app"):
                    names.add((_normalize(entry.name), entry.name[:-4]))
        except OSError:
            pass
    return names


def _matching_app(installer_name, apps):
    target = _normalize(installer_name)
    if len(target) < 3:
        return None
    for norm, display in apps:
        if len(norm) >= 3 and (target.startswith(norm) or norm.startswith(target)):
            return display
    return None


def _zip_contains_app(path):
    try:
        with zipfile.ZipFile(path) as z:
            return any(".app/" in n and n.count("/") <= 2 for n in z.namelist())
    except (zipfile.BadZipFile, OSError, ValueError):
        return False


def _file_hash(path, limit=None):
    h = hashlib.sha256()
    read = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
            read += len(chunk)
            if limit and read >= limit:
                break
    return h.hexdigest()


def school_score(item):
    """Return (score, reasons) for how much a file looks like school work."""
    name = item.name
    ext = extension_of(name)
    sources = " ".join(item.where_from).lower()
    from_school_site = next((s for s in SCHOOL_SITES if s in sources), None)
    if ext not in SCHOOL_EXTENSIONS and not from_school_site:
        return 0, []

    score, reasons = 0, []
    if from_school_site:
        score += 3
        reasons.append(f"downloaded from {from_school_site}")
    elif any(s in sources for s in MAYBE_SCHOOL_SITES):
        score += 1
        reasons.append("exported from Google Docs/Drive")
    code = COURSE_CODE.search(os.path.splitext(name)[0])
    if code:
        score += 2
        reasons.append(f"course code {code.group(1)} {code.group(2)}")
    words = os.path.splitext(name)[0].replace("_", " ")
    keyword = STRONG_SCHOOL_WORDS.search(words)
    if keyword:
        score += 3
    else:
        keyword = WEAK_SCHOOL_WORDS.search(words)
        score += 2 if keyword else 0
    if keyword:
        reasons.append(f'"{keyword.group(0)}" in name')
    if ext in SCHOOL_EXTENSIONS:
        score += 1
        reasons.append(f"school-type file (.{ext})")
    return score, reasons


# --------------------------------------------------------------------------
# The screener
# --------------------------------------------------------------------------

class Screener:
    def __init__(self, locations=None):
        self.locations = locations or SCREEN_LOCATIONS
        self.cancel_event = threading.Event()
        self.stage = "Starting"
        self.current = ""
        self.items = []
        self.blocked = []  # locations macOS wouldn't let us read
        self.error = None
        self.done = False
        self.cancelled = False

    def cancel(self):
        self.cancel_event.set()

    def run(self):
        try:
            self._run()
        except Exception as e:  # report instead of crashing the thread
            self.error = e
        finally:
            self.done = True

    # ---- steps ----

    def _run(self):
        self.stage = "Finding files"
        for top in self.locations:
            self._collect(top)
            if self.cancel_event.is_set():
                self.cancelled = True
                return

        self.stage = "Looking for duplicates"
        duplicates = self._find_duplicates()
        apps = installed_apps()

        self.stage = "Sorting"
        now = time.time()
        for item in self.items:
            self._classify(item, duplicates, apps, now)
        self.items.sort(key=lambda i: i.size, reverse=True)

    def _collect(self, top):
        stack = [top]
        while stack and not self.cancel_event.is_set():
            folder = stack.pop()
            self.current = folder
            try:
                entries = list(os.scandir(folder))
            except PermissionError:
                if folder == top:
                    self.blocked.append(top)
                continue
            except OSError:
                continue
            for entry in entries:
                path = entry.path
                if entry.name.startswith(".") or classify(path)[0]:
                    continue
                if any(_inside(path, d) for d in ORGANIZED_DIRS):
                    continue
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISDIR(st.st_mode):
                    if _is_bundle(path, entry.name):
                        self.items.append(self._make_item(path, st, is_dir=True))
                    else:
                        stack.append(path)
                elif stat.S_ISREG(st.st_mode):
                    self.items.append(self._make_item(path, st, is_dir=False))

    def _make_item(self, path, st, is_dir):
        added = date_added(path) or getattr(st, "st_birthtime", st.st_mtime)
        used = last_used(path) or max(st.st_mtime, added)
        return Item(
            path=path,
            size=_folder_size(path) if is_dir else st.st_size,
            is_dir=is_dir,
            added=added,
            last_used=used,
            where_from=where_from(path),
            is_screenshot=is_screen_capture(path) or bool(SCREENSHOT_NAME.match(os.path.basename(path))),
        )

    def _find_duplicates(self):
        """Return {path: reason} for every file that's a redundant copy."""
        by_size = defaultdict(list)
        for item in self.items:
            if not item.is_dir and item.size > 0:
                by_size[item.size].append(item.path)
        # Files in Documents count as "the original" when Downloads has a copy.
        reference = set()
        for top in REFERENCE_LOCATIONS:
            for dirpath, dirs, files in os.walk(top):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                for f in files:
                    p = os.path.join(dirpath, f)
                    try:
                        size = os.lstat(p).st_size
                    except OSError:
                        continue
                    if size in by_size:
                        by_size[size].append(p)
                        reference.add(p)

        added = {item.path: item.added for item in self.items}

        def keeper_rank(p):
            stem = os.path.splitext(os.path.basename(p))[0]
            return (p not in reference, bool(COPY_SUFFIX.search(stem)), added.get(p, 0), len(p))

        dupes = {}
        for size, paths in by_size.items():
            if len(paths) < 2 or all(p in reference for p in paths):
                continue
            if self.cancel_event.is_set():
                break
            # Compare the first 1 MB first; only fully hash files that still match.
            groups = defaultdict(list)
            for p in paths:
                self.current = p
                try:
                    groups[_file_hash(p, limit=1 << 20)].append(p)
                except OSError:
                    pass
            for candidates in groups.values():
                if len(candidates) < 2:
                    continue
                full = defaultdict(list)
                for p in candidates:
                    try:
                        full[_file_hash(p) if size > (1 << 20) else "same"].append(p)
                    except OSError:
                        pass
                for same in full.values():
                    if len(same) < 2:
                        continue
                    same.sort(key=keeper_rank)
                    keeper = same[0]
                    for p in same[1:]:
                        if p not in reference:
                            dupes[p] = f"identical to {short_path(keeper)}"
        return dupes

    def _classify(self, item, duplicates, apps, now):
        ext = extension_of(item.name)
        stem = os.path.splitext(item.name)[0]
        parent = os.path.dirname(item.path)

        if ext in INCOMPLETE_EXTENSIONS:
            item.suggest("incomplete", TRASH, "download never finished")
            return
        if item.path in duplicates:
            item.suggest("duplicate", TRASH, duplicates[item.path])
            return
        if ext == "zip" and os.path.isdir(os.path.join(parent, stem)):
            item.suggest("unzipped", TRASH, f'already unzipped into folder "{stem}"')
            return
        if ext in INSTALLER_EXTENSIONS or (ext == "zip" and _zip_contains_app(item.path)):
            app = _matching_app(item.name, apps)
            if app:
                item.suggest("installer", TRASH, f"{app} is already installed")
            else:
                item.suggest("installer", TRASH, "installer - not needed once the app is installed")
            return
        if item.is_screenshot:
            item.suggest("screenshot", MOVE, "screenshot", ARCHIVED_SCREENSHOTS)
            return

        score, reasons = school_score(item)
        destination = os.path.join(SCHOOL_ROOT, str(item.year))
        if score >= 4:
            item.reasons.extend(reasons)
            item.suggest("school", MOVE, f"looks like school work from {item.year}", destination)
            return
        if score >= 2:
            item.reasons.extend(reasons)
            item.suggest("maybe_school", MOVE, f"might be school work from {item.year}", destination)
            return

        if item.size >= BIG_FILE and now - item.last_used > OLD_AGE:
            item.suggest("big_old", LEAVE, f"large and last opened {time.strftime('%b %Y', time.localtime(item.last_used))}")
            return
        if item.is_dir and os.path.isdir(os.path.join(item.path, ".git")):
            item.suggest("other", LEAVE, "code project (has git history)")
            return
        item.suggest("other", LEAVE, "no clear suggestion")

    # ---- results ----

    def grouped(self):
        """Return [(key, title, blurb, [items])] for non-empty groups, in display order."""
        by_group = defaultdict(list)
        for item in self.items:
            by_group[item.group].append(item)
        return [(key, title, blurb, by_group[key]) for key, title, blurb in GROUPS if by_group[key]]

    def summary(self):
        trash = [i for i in self.items if i.action == TRASH]
        move = [i for i in self.items if i.action == MOVE]
        return {
            "files": len(self.items),
            "bytes": sum(i.size for i in self.items),
            "trash_count": len(trash),
            "trash_bytes": sum(i.size for i in trash),
            "move_count": len(move),
        }
