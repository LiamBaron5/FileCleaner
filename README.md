# FileCleaner

A desktop app for macOS that helps you tidy up your **Downloads** and **Desktop** folders safely. It finds clutter like old installers, duplicate files, unfinished downloads and screenshots, recognizes school work and files it into folders by year, and shows you what's taking up space on your Mac.

Nothing changes until you review the suggestions and press **Apply**. Deleted files go to the macOS Trash, and a whole batch of changes can be undone with one click.

---

## Features

### Screener
Scans Downloads and Desktop and sorts every file into a group with a suggested action:

| Group | What it finds | Suggestion |
|---|---|---|
| **Incomplete downloads** | `.crdownload`, `.part`, and other downloads that never finished | Trash |
| **Duplicate copies** | Exact copies of another file, including copies of files already in Documents | Trash (the original is kept) |
| **Already-unzipped archives** | `.zip` files sitting next to the folder they were unzipped into | Trash |
| **Installers** | `.dmg`, `.pkg`, and zipped apps (it also notes when the app is already installed) | Trash |
| **Screenshots & recordings** | Screenshots and screen recordings, found using macOS's own screenshot tag, not just the filename | Move to `Downloads/Archived Screenshots` |
| **School work** | Documents that look like coursework | Move to `Documents/School/<year>` |
| **Possibly school work** | Weaker matches, worth checking | Move to `Documents/School/<year>` (off by default) |
| **Big & not opened in a year** | Large files you haven't opened in over a year | Flagged for review |
| **Everything else** | Anything without a clear suggestion | Left alone |

Select any file to see a preview, its size, when it was downloaded, when it was last opened, and which website it came from.

### How school work is detected
Each file gets a score based on clues:
- It was downloaded from a school website (your college's domain, Canvas, Gradescope, Overleaf, and similar)
- Its name contains a course code like `ECON 101` or `BIO_242`
- Its name contains a word like *essay*, *syllabus*, *problem set*, *lecture* or *midterm*
- It's a document-type file (`.pdf`, `.docx`, `.pages`, `.pptx`, `.ipynb`, and so on)

Strong matches go to **School work** and weaker ones to **Possibly school work**. Files are filed by the calendar year they were downloaded.

### Disk Explorer
A read-only view of what's using space anywhere on your Mac:
- **Folders:** a tree sorted by size, with a treemap where each box's size shows how much space it uses (double-click to zoom in)
- **Largest files:** your 1,000 biggest files
- **File types:** space used by category and file extension

---

## Safety

FileCleaner is built to never touch anything your Mac needs:

- **Safety labels on every path.** macOS system locations (`/System`, `/usr`, `/Library`, and so on) are marked **Protected**. App data, settings, installed apps and hidden config files are marked **Caution**. The Screener never shows either kind, and the app refuses to change them even if asked.
- **Trash, not delete.** Files go to the real macOS Trash, so Finder's *Put Back* works.
- **Checked again right before every change.** The file must still exist, be inside Downloads or Desktop, and be headed to an approved folder.
- **Never overwrites.** If a file with the same name exists, the new one becomes `name (2).pdf`.
- **Undo.** *Undo last apply* puts an entire batch back and removes any folders it created that are now empty.
- **History log.** Every change is recorded in `~/Library/Application Support/FileCleaner/history.jsonl`.

---

## Requirements

- macOS
- Python 3.9 or newer, with Tkinter. The Python installer from [python.org](https://www.python.org/downloads/macos/) includes it. If you use Homebrew's Python, run `brew install python-tk`.
- No other packages needed. FileCleaner uses only Python's standard library and tools built into macOS.

---

## Getting started

```bash
git clone https://github.com/<your-username>/FileCleaner.git
cd FileCleaner
python3 FileCleaner.py
```

The first time you run it, macOS will ask whether Terminal (or VS Code) can access your Downloads and Desktop folders. Click **Allow**. If you clicked *Don't Allow* earlier, turn access back on in **System Settings → Privacy & Security → Files and Folders**.

### Using the Screener
1. The app opens on the **Screener** tab and scans Downloads and Desktop automatically.
2. Look over the groups. Select a file to preview it, and press **Space** for a full Quick Look view.
3. Change any suggestion with the keyboard (or right-click):

   | Key | Action |
   |---|---|
   | **T** | Trash this file instead |
   | **L** | Leave this file alone |
   | **S** | Go back to the original suggestion |
   | **Space** | Quick Look |

   The selection moves to the next file after each choice, so you can go through a group quickly.
4. Click **Apply suggestions…**, check or uncheck groups on the confirmation screen, and click **Apply**.
5. Changed your mind? Click **Undo last apply**.

### Command-line options

```bash
python3 FileCleaner.py                              # open the app
python3 FileCleaner.py --screen                     # print the Screener's suggestions (changes nothing)
python3 FileCleaner.py --screen --json out.json     # also save all suggestions to a JSON file
python3 FileCleaner.py ~/Downloads --report         # print a disk-usage summary of a folder
```

---

## Customizing

The Screener's settings are at the top of `screener.py`:

- `SCHOOL_SITES`: websites whose downloads count as school work (add your own school's domain here)
- `STRONG_SCHOOL_WORDS` / `WEAK_SCHOOL_WORDS`: filename keywords
- `SCHOOL_ROOT` and `ARCHIVED_SCREENSHOTS`: where files get organized
- `BIG_FILE` and `OLD_AGE`: what counts as "big" and "old"

---

## Project structure

| File | Purpose |
|---|---|
| `FileCleaner.py` | The app window and command-line options |
| `screener.py` | Sorts files and makes suggestions (read-only) |
| `actions.py` | The only code that changes files: moving to the Trash, moving into folders, undo, and the history log |
| `core.py` | Shared safety rules, file categories, and the disk scanner |

---

## Roadmap

- [x] Disk Explorer
- [x] Screener with suggestions
- [x] Apply suggestions, with Undo
- [ ] Choose a custom destination folder for any file
- [ ] Suggestions that learn from where you've put similar files
- [ ] A rules file for your own organization preferences
- [ ] Optional Claude integration for chatting about how to organize your files

---

## Disclaimer

FileCleaner only moves files to the Trash or into its own organizing folders, and every change can be undone. Still, check the suggestions before applying them, especially the first time.

Creating a tool for me to help reorganize the file paths on my Mac.
