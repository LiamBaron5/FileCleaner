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

from core import HOME, category_of, classify, extension_of

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
SCHOOL_ROOT = os.path.join(DOCUMENTS, "School")            # School/<year>/<course>
PERSONAL_ROOT = os.path.join(DOCUMENTS, "Personal")        # Personal/Finance, /Insurance & Health, ...
BOOKS_ROOT = os.path.join(DOCUMENTS, "Books & Papers")
DATA_ROOT = os.path.join(DOCUMENTS, "Data")
PROJECTS_ROOT = os.path.join(DOCUMENTS, "Projects")
PICTURES_ROOT = os.path.join(HOME, "Pictures", "From Downloads")  # .../<year>
MOVIES_ROOT = os.path.join(HOME, "Movies", "From Downloads")      # .../<year>
MUSIC_ROOT = os.path.join(HOME, "Music", "From Downloads")

# Your own topic folders: Documents/<topic>. A file goes there when its name
# contains one of the words, or it was downloaded from a site containing one.
TOPIC_FOLDERS = {
    "Skiing": ("ski", "skiing", "ussa", "snowboard", "usskiandsnowboard"),
}

# Every folder FileCleaner files things into. Suggestions only ever point
# inside these, and they are never screened, so things you've already put
# away don't get flagged again.
ORGANIZED_DIRS = ([ARCHIVED_SCREENSHOTS, SCHOOL_ROOT, PERSONAL_ROOT, BOOKS_ROOT, DATA_ROOT,
                   PROJECTS_ROOT, PICTURES_ROOT, MOVIES_ROOT, MUSIC_ROOT]
                  + [os.path.join(DOCUMENTS, topic) for topic in TOPIC_FOLDERS])

# A download from any of these sites is a strong sign of school work.
SCHOOL_SITES = ("bates.edu", "instructure.com", "canvas", "blackboard", "moodle",
                "gradescope.com", "overleaf.com", "turnitin.com", "perusall.com",
                "piazza.com", "jstor.org", "edpuzzle.com")
# Weaker sign: exports from Google Docs/Drive are often, but not always, school.
MAYBE_SCHOOL_SITES = ("docs.google.com", "drive.google.com", "colab.research.google.com")

def _words(pattern):
    return re.compile(rf"(?<![a-z])({pattern})(?![a-z])", re.IGNORECASE)


# Words that almost always mean school work...
STRONG_SCHOOL_WORDS = _words(
    r"essay|thesis|homework|hw ?\d+|assignment|problem ?sets?|p ?set ?\d*|syllabus|midterm|"
    r"exam|quiz|study ?guide|lecture|lec ?\d+|worksheet|rubric|annotated bibliography|seminar|"
    r"lab report|lab ?\d+|pre ?lab|prelab|pre ?class|class ?notes|mt ?\d|final exam|qz ?\d+|"
    r"problem ?\d+|mid-?semester|mid-?term")
# ...and words that often do, but also show up elsewhere.
WEAK_SCHOOL_WORDS = _words(
    r"paper|lab|notes|final|reading|response|chapter|ch ?\d+|week ?\d+|unit ?\d+|prompt|"
    r"presentation|bibliography|citation|draft|outline|project|reflection|tutorial|soln|solutions?|"
    r"discussion ?\d*|test ?\d+|table ?\d+|fig ?\d+|figure ?\d+")
# Course codes like "ECON 101", "BIO_242", "FYS-123A"...
COURSE_CODE = re.compile(r"(?<![A-Za-z])([A-Z]{2,4})[ _-]?(\d{3}[A-Z]?)(?!\d)")
# ...or a department name in any case followed by a number, like "Econ255Final".
DEPARTMENTS = ("afr|amst|anth|arab|art|astr|bio|biol|chem|chi|clas|cmpu|cs|dcs|econ|edu|educ|engl|"
               "envr|es|fren|fys|geo|ger|gsws|hist|japn|lat|math|mus|neur|phil|phys|pltc|poli|psyc|"
               "psych|rel|rels|russ|soc|span|stat|thea")
DEPT_CODE = re.compile(rf"(?<![A-Za-z])((?i:{DEPARTMENTS}))[ _-]?(\d{{3}})([A-Z](?![a-z]))?(?!\d)")
# A department written in capitals with no number, like "PHYS pre class 3".
DEPT_WORD = re.compile(r"(?<![A-Za-z])(PHYS|ECON|CHEM|BIOL?|MATH|PSYCH?|HIST|ENGL|ENVR|PHIL|POLI|"
                       r"PLTC|SOC|ANTH|DCS|CMPU|STAT|ASTR|NEUR|FYS)(?![A-Z]{2}|[a-z])")

SCHOOL_EXTENSIONS = set("pdf doc docx pages ppt pptx key xls xlsx numbers txt rtf md odt "
                        "ipynb tex rmd r csv py java c cpp m do nb jl".split())
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

# Personal records: Documents/Personal/<folder>. Matched on the file name or
# the site it came from. These are always filed away, never suggested for Trash.
PERSONAL_RULES = [
    ("Career", _words(r"r[eé]sum[eé]|cv|cover letter|portfolio|references|offer letter|"
                      r"recommendation letter|letter of recommendation"), ("linkedin.com", "joinhandshake.com")),
    ("Finance", _words(r"tax(es)?|tax ?return|w-?2|1099|1040|irs|pay ?stub|paycheck|bank statement|"
                       r"statement|invoice|receipt|budget|brokerage"),
     ("turbotax", "intuit.com", "irs.gov", "fidelity.com", "vanguard.com", "schwab.com", "chase.com",
      "bankofamerica.com", "wellsfargo.com", "paypal.com", "venmo.com", "robinhood.com")),
    ("Insurance & Health", _words(r"insurance|medical|health|prescription|vaccin\w*|immuni[sz]ation|"
                                  r"dental|doctor|claim|eob"),
     ("mychart", "cvs.com", "walgreens.com", "bcbs", "aetna.com", "cigna.com", "uhc.com")),
    ("IDs & Records", _words(r"passport|driver'?s? ?licen[cs]e|licen[cs]e|birth certificate|"
                             r"social security|ssn|visa|id card|transcript"), ()),
    ("Housing", _words(r"lease|rental agreement|rent|utilities"), ()),
]

# Books and papers: e-books, PDFs named with an ISBN, or from publishers.
EBOOK_EXTENSIONS = {"epub", "mobi", "azw", "azw3", "djvu"}
ISBN = re.compile(r"97[89]\d{10}")  # checked after removing spaces and dashes
BOOK_WORDS = _words(r"textbook|edition|\d+(st|nd|rd|th) ed|solutions manual")
BOOK_SITES = ("springer", "arxiv.org", "sciencedirect", "wiley.com", "jstor.org", "researchgate",
              "nature.com", "pnas.org", "academic.oup.com", "ieee.org", "oreilly.com", "libgen",
              "annas-archive", "tandfonline", "sagepub", "cambridge.org")

# Data files: datasets always; spreadsheets only when they came from a data site.
DATA_EXTENSIONS = {"csv", "tsv", "dta", "sav", "parquet", "feather", "rds", "rdata", "sas7bdat"}
SPREADSHEET_EXTENSIONS = {"xls", "xlsx", "numbers"}
DATA_SITES = ("fred.stlouisfed.org", "noaa.gov", "bls.gov", "census.gov", "data.gov", "kaggle",
              "worldbank.org", "ourworldindata", "ipums", "bea.gov", "oecd.org", "imf.org")

# Code: scripts, notebooks and project folders.
CODE_EXTENSIONS = {"py", "ipynb", "js", "ts", "jsx", "tsx", "java", "c", "cpp", "h", "swift", "go",
                   "rs", "rb", "php", "html", "css", "r", "rmd", "m", "nb", "do", "sql", "sh", "jl"}

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
    ("empty", "Empty folders", "Folders with no files in them"),
    ("installer", "Installers", "Disk images and installer packages"),
    ("screenshot", "Screenshots & recordings", "Archived by default - press T on one to trash it instead"),
    ("school", "School work", "Likely school files, to file by year"),
    ("maybe_school", "Possibly school work", "Some signs of school work - check these"),
    ("personal", "Personal records", "Taxes, insurance, IDs and other papers to keep - filed, never trashed"),
    ("books", "Books & papers", "Textbooks, e-books and research papers"),
    ("data", "Data files", "Datasets and downloaded spreadsheets"),
    ("projects", "Projects & code", "Code projects, notebooks and scripts"),
    ("topics", "Your topics", "Files that match your topic folders"),
    ("media", "Photos, videos & music", "Filed into Pictures, Movies and Music by year"),
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
    contents: list = field(default_factory=list)  # for a folder: names of the files inside (capped)
    has_git: bool = False

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


MAX_CONTENTS = 2000


def _folder_profile(path):
    """Return (total size, names of the visible files inside, capped) for a folder."""
    total, names = 0, []
    for dirpath, _dirs, files in os.walk(path):
        hidden = "/." in dirpath[len(path):]  # e.g. inside .git: counted, but not listed
        for f in files:
            try:
                total += os.lstat(os.path.join(dirpath, f)).st_size
            except OSError:
                continue
            if not hidden and not f.startswith(".") and len(names) < MAX_CONTENTS:
                names.append(f)
    return total, names


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


def _tidy(stem):
    """'Soln_SampleMidterm' -> 'Soln Sample Midterm', so keywords can be found."""
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", stem.replace("_", " "))


def school_signals(stem, ext, sources="", any_type=False):
    """Return (score, reasons, course) for how much a name looks like school work.

    course is a tidy course name like "ECON 255" when one is in the name, else None.
    strong is True when there's a clear sign (school site, course code, or strong word),
    not just a weak word plus the file type. Unless any_type is set, only
    document-type files (or school-site downloads) count.
    """
    from_school_site = next((s for s in SCHOOL_SITES if s in sources), None)
    if not any_type and ext not in SCHOOL_EXTENSIONS and not from_school_site:
        return 0, [], None, False

    score, reasons = 0, []
    if from_school_site:
        score += 3
        reasons.append(f"downloaded from {from_school_site}")
    elif any(s in sources for s in MAYBE_SCHOOL_SITES):
        score += 1
        reasons.append("from Google Docs/Drive/Colab")
    course = None
    code = COURSE_CODE.search(stem)
    dept = DEPT_CODE.search(stem)
    if code:
        course = f"{code.group(1)} {code.group(2)}"
    elif dept:
        course = f"{dept.group(1).upper()} {dept.group(2)}{dept.group(3) or ''}"
    if course:
        score += 2
        reasons.append(f"course code {course}")
    else:
        word = DEPT_WORD.search(stem)
        if word:
            score += 2
            reasons.append(f'department "{word.group(1)}" in name')
    words = _tidy(stem)
    keyword = STRONG_SCHOOL_WORDS.search(words)
    strong = bool(from_school_site or course or keyword)
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
    return score, reasons, course, strong


def school_score(item):
    """Return (score, reasons) for how much a file looks like school work."""
    score, reasons, _course, _strong = school_signals(os.path.splitext(item.name)[0], extension_of(item.name),
                                             " ".join(item.where_from).lower())
    return score, reasons


TOPIC_PATTERNS = {topic: _words("|".join(re.escape(w) for w in words))
                  for topic, words in TOPIC_FOLDERS.items()}


def personal_match(stem, sources):
    """Return (Personal subfolder, reason) or None."""
    words = _tidy(stem)
    for folder, pattern, sites in PERSONAL_RULES:
        m = pattern.search(words)
        if m:
            return folder, f'"{m.group(0)}" in name'
        site = next((s for s in sites if s in sources), None)
        if site:
            return folder, f"downloaded from {site}"
    return None


def topic_match(stem, sources):
    """Return (topic folder, reason) or None."""
    words = _tidy(stem)
    for topic, pattern in TOPIC_PATTERNS.items():
        m = pattern.search(words)
        if m:
            return topic, f'"{m.group(0)}" in name'
        site = next((w for w in TOPIC_FOLDERS[topic] if len(w) >= 4 and w in sources), None)
        if site:
            return topic, f'downloaded from a "{site}" site'
    return None


def book_match(stem, ext, sources):
    """Return a reason this looks like a book or paper, or None."""
    if ext in EBOOK_EXTENSIONS:
        return f"e-book (.{ext})"
    if ext != "pdf":
        return None
    if ISBN.search(re.sub(r"[\s_-]", "", stem)):
        return "ISBN in name"
    m = BOOK_WORDS.search(_tidy(stem))
    if m:
        return f'"{m.group(0)}" in name'
    site = next((s for s in BOOK_SITES if s in sources), None)
    return f"downloaded from {site}" if site else None


def data_match(ext, sources):
    """Return a reason this looks like a dataset, or None."""
    site = next((s for s in DATA_SITES if s in sources), None)
    if ext in DATA_EXTENSIONS:
        return f"dataset (.{ext})" + (f" from {site}" if site else "")
    if ext in SPREADSHEET_EXTENSIONS and site:
        return f"spreadsheet from {site}"
    return None


def media_destination(name, year):
    """Return (folder, reason) for photos, videos and audio, or None."""
    category = category_of(name)
    if category == "Images":
        return os.path.join(PICTURES_ROOT, str(year)), "photo or image"
    if category == "Video":
        return os.path.join(MOVIES_ROOT, str(year)), "video"
    if category == "Audio":
        return MUSIC_ROOT, "audio"
    return None


def school_destination(year, course):
    folder = os.path.join(SCHOOL_ROOT, str(year))
    return os.path.join(folder, course) if course else folder


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
                    # A folder right inside Downloads/Desktop (a class folder, a Drive
                    # download, a project) is screened as one item and kept together.
                    if folder == top or _is_bundle(path, entry.name):
                        self.items.append(self._make_item(path, st, is_dir=True))
                    else:
                        stack.append(path)
                elif stat.S_ISREG(st.st_mode):
                    self.items.append(self._make_item(path, st, is_dir=False))

    def _make_item(self, path, st, is_dir):
        added = date_added(path) or getattr(st, "st_birthtime", st.st_mtime)
        used = last_used(path) or max(st.st_mtime, added)
        size, contents = _folder_profile(path) if is_dir else (st.st_size, [])
        return Item(
            path=path,
            size=size,
            is_dir=is_dir,
            added=added,
            last_used=used,
            where_from=where_from(path),
            is_screenshot=is_screen_capture(path) or bool(SCREENSHOT_NAME.match(os.path.basename(path))),
            contents=contents,
            has_git=is_dir and os.path.isdir(os.path.join(path, ".git")),
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

        if item.is_dir and ext not in BUNDLE_EXTENSIONS:
            self._classify_folder(item, now)
            return

        sources = " ".join(item.where_from).lower()
        score, reasons, course, strong = school_signals(stem, ext, sources, any_type=True)
        # Documents need 4 points; other files (like a lab's graph) get no file-type
        # point, so 3 is enough - but only clear signs count for them.
        document = ext in SCHOOL_EXTENSIONS or any(s in sources for s in SCHOOL_SITES)
        is_school = score >= 4 if document else (score >= 3 and strong)

        def file_as_school():
            item.reasons.extend(reasons)
            item.suggest("school", MOVE, f"looks like school work from {item.year}",
                         school_destination(item.year, course))

        if is_school and strong:
            file_as_school()
            return
        personal = personal_match(stem, sources)
        if personal:
            folder, why = personal
            item.suggest("personal", MOVE, f"personal record: {why}", os.path.join(PERSONAL_ROOT, folder))
            return
        topic = topic_match(stem, sources)
        if topic:
            folder, why = topic
            item.suggest("topics", MOVE, f"{folder}: {why}", os.path.join(DOCUMENTS, folder))
            return
        why = book_match(stem, ext, sources)
        if why:
            item.suggest("books", MOVE, why, BOOKS_ROOT)
            return
        if is_school:
            file_as_school()
            return
        if document and score >= 2:
            item.reasons.extend(reasons)
            item.suggest("maybe_school", MOVE, f"might be school work from {item.year}",
                         school_destination(item.year, course))
            return
        why = data_match(ext, sources)
        if why:
            item.suggest("data", MOVE, why, DATA_ROOT)
            return
        if ext in CODE_EXTENSIONS:
            item.suggest("projects", MOVE, f"script or notebook (.{ext})", os.path.join(PROJECTS_ROOT, "Scripts"))
            return
        media = media_destination(item.name, item.year)
        if media:
            folder, why = media
            item.suggest("media", MOVE, why, folder)
            return
        self._classify_leftover(item, now, "no clear suggestion")

    def _classify_leftover(self, item, now, reason):
        if item.size >= BIG_FILE and now - item.last_used > OLD_AGE:
            item.suggest("big_old", LEAVE, f"large and last opened {time.strftime('%b %Y', time.localtime(item.last_used))}")
            return
        item.suggest("other", LEAVE, reason)

    def _classify_folder(self, item, now):
        """Suggest a home for a whole folder, judging by its name and what's inside."""
        names = item.contents
        n = len(names)
        if item.has_git:
            item.suggest("projects", MOVE, "code project (has git history)", PROJECTS_ROOT)
            return
        if n == 0:
            if item.size <= 1 << 20:
                item.suggest("empty", TRASH, "folder has no files in it")
            else:
                self._classify_leftover(item, now, "folder with only hidden files")
            return
        exts = defaultdict(int)
        for name in names:
            exts[extension_of(name)] += 1

        def count(extensions):
            return sum(c for e, c in exts.items() if e in extensions)

        def files(k):
            return f"{k:,} of {n:,} files"

        sources = " ".join(item.where_from).lower()
        score, reasons, course, _strong = school_signals(item.name, "", sources, any_type=True)
        inside = [school_signals(os.path.splitext(x)[0], extension_of(x)) for x in names[:500]]
        school_files = sum(1 for s in inside if s[0] >= 4)
        if not course:
            courses = defaultdict(int)
            for s in inside:
                if s[2]:
                    courses[s[2]] += 1
            course = max(courses, key=courses.get) if courses else None
        if score >= 3 or (school_files >= 2 and school_files >= 0.2 * len(inside)):
            item.reasons.extend(reasons)
            if school_files:
                item.reasons.append(f"{files(school_files)} look like school work")
            item.suggest("school", MOVE, f"folder of school work from {item.year}",
                         school_destination(item.year, course))
            return
        personal = personal_match(item.name, sources)
        if personal:
            folder, why = personal
            item.suggest("personal", MOVE, f"folder of personal records: {why}", os.path.join(PERSONAL_ROOT, folder))
            return
        topic = topic_match(item.name, sources)
        if topic:
            folder, why = topic
            item.suggest("topics", MOVE, f"{folder}: {why}", os.path.join(DOCUMENTS, folder))
            return
        code = count(CODE_EXTENSIONS)
        if code >= 0.4 * n:
            item.suggest("projects", MOVE, f"code project ({files(code)} are code)", PROJECTS_ROOT)
            return
        data = count(DATA_EXTENSIONS | SPREADSHEET_EXTENSIONS)
        if re.search(r"(?<![a-z])data(sets?)?(?![a-z])", _tidy(item.name), re.IGNORECASE):
            item.suggest("data", MOVE, '"data" in folder name', DATA_ROOT)
            return
        if data >= 0.5 * n:
            item.suggest("data", MOVE, f"folder of data ({files(data)} are datasets)", DATA_ROOT)
            return
        kinds = defaultdict(int)
        for e, c in exts.items():
            kinds[category_of("x." + e)] += c
        media = kinds["Images"] + kinds["Video"] + kinds["Audio"]
        if media >= 0.7 * n:
            top = max(("Images", "Video", "Audio"), key=lambda k: kinds[k])
            folder = {"Images": os.path.join(PICTURES_ROOT, str(item.year)),
                      "Video": os.path.join(MOVIES_ROOT, str(item.year)), "Audio": MUSIC_ROOT}[top]
            item.suggest("media", MOVE, f"folder of photos, videos or music ({files(media)})", folder)
            return
        if score >= 2 or school_files:
            item.reasons.extend(reasons)
            item.suggest("maybe_school", MOVE, f"folder that might be school work from {item.year}",
                         school_destination(item.year, course))
            return
        self._classify_leftover(item, now, f"folder of {n:,} files - no clear suggestion")

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
