"""
Actions - the ONLY part of FileCleaner that changes files.

It can do exactly two things, and only when you ask:
  * move a file to the macOS Trash (restorable from the Trash, or with Undo)
  * move a file into one of FileCleaner's organizing folders

Every action is checked again right before it happens (the file still exists,
it isn't a protected/caution path, and the destination is an allowed folder),
never overwrites anything, and is written to a history log so a whole batch
can be undone.
"""

import ctypes
import ctypes.util
import json
import os
import shutil
import time
from ctypes import POINTER, c_bool, c_char_p, c_void_p

from core import HOME, classify
import screener as scr

HISTORY_FILE = os.path.join(HOME, "Library", "Application Support", "FileCleaner", "history.jsonl")


def allowed_destinations():
    """Folders files may be moved into. Anything else is refused."""
    return [scr.SCHOOL_ROOT, scr.ARCHIVED_SCREENSHOTS]


# --------------------------------------------------------------------------
# Moving to the Trash (uses macOS's own API, so Finder's "Put Back" works)
# --------------------------------------------------------------------------

try:
    _objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
    ctypes.cdll.LoadLibrary(ctypes.util.find_library("Foundation"))
    _objc.objc_getClass.restype = c_void_p
    _objc.objc_getClass.argtypes = [c_char_p]
    _objc.sel_registerName.restype = c_void_p
    _objc.sel_registerName.argtypes = [c_char_p]
    _objc.objc_autoreleasePoolPush.restype = c_void_p
    _objc.objc_autoreleasePoolPop.argtypes = [c_void_p]
    _MSG_SEND = ctypes.cast(_objc.objc_msgSend, c_void_p).value
except (OSError, AttributeError, TypeError):
    _objc = None


def _send(obj, selector, *args, restype=c_void_p, argtypes=()):
    fn = ctypes.CFUNCTYPE(restype, c_void_p, c_void_p, *argtypes)(_MSG_SEND)
    return fn(obj, _objc.sel_registerName(selector), *args)


def _nsstring_to_str(ns):
    raw = _send(ns, b"UTF8String", restype=c_char_p) if ns else None
    return raw.decode() if raw else ""


def move_to_trash(path):
    """Move path to the Trash and return where it ended up."""
    if _objc is None:
        return _move_to_trash_fallback(path)
    pool = _objc.objc_autoreleasePoolPush()
    try:
        ns_path = _send(_objc.objc_getClass(b"NSString"), b"stringWithUTF8String:",
                        os.fsencode(path), argtypes=(c_char_p,))
        url = _send(_objc.objc_getClass(b"NSURL"), b"fileURLWithPath:", ns_path, argtypes=(c_void_p,))
        manager = _send(_objc.objc_getClass(b"NSFileManager"), b"defaultManager")
        result, error = c_void_p(), c_void_p()
        ok = _send(manager, b"trashItemAtURL:resultingItemURL:error:", url,
                   ctypes.byref(result), ctypes.byref(error),
                   restype=c_bool, argtypes=(c_void_p, POINTER(c_void_p), POINTER(c_void_p)))
        if not ok:
            message = _nsstring_to_str(_send(error, b"localizedDescription")) if error else ""
            raise OSError(message or "macOS refused to move it to the Trash")
        return _nsstring_to_str(_send(result, b"path")) if result else ""
    finally:
        _objc.objc_autoreleasePoolPop(pool)


def _move_to_trash_fallback(path):
    target = unique_path(os.path.join(HOME, ".Trash", os.path.basename(path)))
    shutil.move(path, target)
    return target


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def unique_path(path):
    """Return path, or 'name (2).ext', 'name (3).ext'... if it's taken."""
    if not os.path.lexists(path):
        return path
    folder, name = os.path.split(path)
    stem, ext = os.path.splitext(name)
    n = 2
    while True:
        candidate = os.path.join(folder, f"{stem} ({n}){ext}")
        if not os.path.lexists(candidate):
            return candidate
        n += 1


def _make_dirs(folder, created):
    """Create folder (and parents) as needed, remembering what was new."""
    missing = []
    while not os.path.isdir(folder):
        missing.append(folder)
        folder = os.path.dirname(folder)
    for d in reversed(missing):
        os.mkdir(d)
        created.append(d)


def _move(src, dst):
    try:
        os.rename(src, dst)  # instant and keeps all metadata on the same disk
    except OSError:
        shutil.move(src, dst)


def _check_safe(item):
    """Return a reason this item must not be touched, or None if it's fine."""
    if not os.path.lexists(item.path):
        return "no longer exists"
    level, reason = classify(item.path)
    if level:
        return f"protected ({reason})"
    if not any(item.path.startswith(top + "/") for top in scr.SCREEN_LOCATIONS):
        return "outside Downloads/Desktop"
    if item.action == scr.MOVE:
        dest = os.path.abspath(item.destination)
        if not any(dest == d or dest.startswith(d + "/") for d in allowed_destinations()):
            return f"destination not allowed ({scr.short_path(dest)})"
        if os.path.abspath(item.path) == dest or dest.startswith(os.path.abspath(item.path) + "/"):
            return "destination is inside the item itself"
    elif item.action != scr.TRASH:
        return "nothing to do"
    return None


# --------------------------------------------------------------------------
# History log (one JSON object per line, so a crash can't corrupt old entries)
# --------------------------------------------------------------------------

def _log(entry):
    os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
    with open(HISTORY_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")


def _read_log():
    try:
        with open(HISTORY_FILE) as f:
            return [json.loads(line) for line in f if line.strip()]
    except (OSError, ValueError):
        return []


def last_batch():
    """Return (batch_id, [entries]) for the most recent batch not yet undone."""
    entries = _read_log()
    undone = {e["batch"] for e in entries if e.get("op") == "undone"}
    for entry in reversed(entries):
        if entry.get("op") in ("trash", "move") and entry["batch"] not in undone:
            batch = entry["batch"]
            return batch, [e for e in entries if e["batch"] == batch]
    return None, []


# --------------------------------------------------------------------------
# Apply and undo
# --------------------------------------------------------------------------

class Batch:
    """Applies a list of screener Items. Run it in a background thread."""

    def __init__(self, items):
        self.items = items
        self.id = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"
        self.done_count = 0
        self.trashed = []
        self.moved = []
        self.skipped = []  # (item, reason)
        self.finished = False

    def run(self):
        try:
            for item in self.items:
                self._apply(item)
                self.done_count += 1
        finally:
            self.finished = True

    def _apply(self, item):
        problem = _check_safe(item)
        if problem:
            self.skipped.append((item, problem))
            return
        try:
            if item.action == scr.TRASH:
                where = move_to_trash(item.path)
                _log({"batch": self.id, "op": "trash", "from": item.path, "to": where, "time": time.time()})
                self.trashed.append(item)
            else:
                created = []
                _make_dirs(item.destination, created)
                for d in created:
                    _log({"batch": self.id, "op": "mkdir", "path": d})
                target = unique_path(os.path.join(item.destination, item.name))
                _move(item.path, target)
                _log({"batch": self.id, "op": "move", "from": item.path, "to": target, "time": time.time()})
                self.moved.append(item)
        except OSError as e:
            self.skipped.append((item, e.strerror or str(e)))


def undo_last():
    """Put back everything from the most recent batch. Returns (restored, problems)."""
    batch, entries = last_batch()
    if batch is None:
        return 0, []
    restored, problems = 0, []
    for entry in reversed(entries):
        op = entry.get("op")
        if op in ("trash", "move"):
            src, dst = entry["to"], entry["from"]
            if not src or not os.path.lexists(src):
                problems.append(f"{os.path.basename(dst)}: no longer at {scr.short_path(src) or 'unknown'}")
            elif os.path.lexists(dst):
                problems.append(f"{os.path.basename(dst)}: something new is already at the original spot")
            else:
                try:
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    _move(src, dst)
                    restored += 1
                except OSError as e:
                    problems.append(f"{os.path.basename(dst)}: {e.strerror or e}")
        elif op == "mkdir":
            try:
                os.rmdir(entry["path"])  # only succeeds if the folder is empty again
            except OSError:
                pass
    _log({"batch": batch, "op": "undone", "time": time.time()})
    return restored, problems
