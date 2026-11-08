# FileCleaner

macOS desktop app (Python 3.13 + tkinter, standard library only) for understanding and tidying files.

## Layout
- `FileCleaner.py` — entry point: tkinter GUI plus CLI flags (`--screen`, `--json`, `--report`).
- `core.py` — safety classification (`PROTECTED` / `CAUTION`), file categories, formatting, read-only disk `Scanner`.
- `screener.py` — READ-ONLY. Sorts Downloads/Desktop files into groups with suggested actions.
- `actions.py` — the ONLY module that changes files: move to Trash (via macOS API) or move into allowed organizing folders. Logs to `~/Library/Application Support/FileCleaner/history.jsonl` for undo.

## Rules
- Keep `core.py` and `screener.py` read-only; any file-changing code belongs in `actions.py`.
- Never hard-delete: Trash only, never overwrite, re-check safety right before acting.
- No third-party dependencies.

## Run / check
- `python3 FileCleaner.py` — open the app
- `python3 FileCleaner.py --screen` — print suggestions (no changes)
- `python3 -m py_compile *.py` — quick syntax check
