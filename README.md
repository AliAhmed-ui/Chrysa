# Chrysa

An offline, open-source media center for the movies and TV shows already on your disk.
Dark Netflix-style interface, embedded player (libVLC), automatic resume, and no
network access at any point: artwork is grabbed from the videos themselves.

```
./run.sh
```

That one command installs what is missing, builds the Python environment, adds
**Chrysa** to your application launcher and starts the app. Later launches take the
same path and start in a fraction of a second.

## What it does

- **Understands download folders as they are.** Point it at `~/Videos` (the default)
  or any other folder and it sorts out:
  - movie folders with release tags — `Heat (1995) [1080p]`, `Tenet (2020) [1080p] [BluRay] [5.1] [YTS.MX]`
  - shows with season folders — `Friends/S01`, `BEEF/Season 2`, `season1`, `Specials`
  - season packs and loose files — `This.Is.Us.S01.COMPLETE.720p…/`, `ES_Project_Demo.mp4`

  Titles, years, seasons and episode numbers are parsed with
  [guessit](https://github.com/guessit-io/guessit). Samples, trailers, `.txt`/`.nfo`
  files and torrent-site adverts are ignored.
- **Home screen rows** — *Continue Watching* (anything between 2 % and 92 % watched,
  most recent first), *TV Shows* (one card per show), *Movies*, *Other Videos*.
  Tabs for TV Shows and Movies, a search box, and *Rescan Library*.
- **Show page** — banner with a play button that resumes the last episode you were
  watching, or starts the next one if you finished it; season selector; episode list
  with titles, running times, red progress bars and *Play* / *Resume* buttons.
- **Embedded player** — seek bar, ±10 s, volume, subtitle and audio track menus,
  fullscreen, *Next Episode*, and automatic play of the next episode when one ends.
  The control bars hide after three seconds without input.
- **Resume everywhere** — the position is written to SQLite every 3 seconds and
  immediately on pause or exit. Opening a partly watched item continues from that
  spot (*Start Over* is in the player and the right-click menu).
- **Watch log that follows you between systems** — on first launch Chrysa asks where to
  keep `chrysa-watch-log.json`, a small file recording which movie you stopped in and
  which episode of which season you left off at. Point both systems of a dual boot at
  the same folder and progress is merged silently at start-up and after every scan
  (newest wins; entries are matched by title and season/episode, not file path).
  *Change log location* in the sidebar moves it.
- **Browse (Search & Download)** — the switch at the top of the window. Right (default) is
  the normal offline library; left opens Browse. Nothing touches the network until you press
  *Allow Internet Access*. The first time, Chrysa asks where to save downloads (*Change download
  folder* changes it). Searching queries YTS, The Pirate Bay, 1337x, EZTV, Nyaa, SubsPlease,
  BitTorrented and the Internet Archive (`chrysa/bd.py`, also usable alone: `python3 bd.py "query"`).
  Results (↑/↓, Enter or click) are sorted by seeders; press **D** to download. Each download has a
  progress bar with size, speed, ETA and peers. Select one and press **C** to cancel it and delete
  its partial files. Unfinished downloads are saved when you quit and resume after you press *Allow
  Internet Access* next time. Downloading uses `aria2` (installed by `run.sh`) or the `libtorrent`
  Python bindings. Ctrl+D flips between the two modes. Only download what you have the right to.
- **Offline thumbnails** — a `poster.jpg` / `cover.jpg` / `folder.jpg` next to a video
  is used if present; otherwise ffmpeg grabs a frame 15 % into the file (retrying
  further in if that frame is black) and caches it.

## Keyboard and mouse

| Player | |
| --- | --- |
| `Space` or click | Play / pause |
| `←` / `→` | Back / forward 10 seconds |
| `↑` / `↓` or mouse wheel | Volume |
| `F` or double-click | Fullscreen |
| `N` | Next episode |
| `S` / `A` | Subtitle / audio track menu |
| `M` | Mute |
| `Home` | Start over |
| `Esc` | Leave fullscreen, then back to the library |

| Library | |
| --- | --- |
| `Ctrl+F` or `/` | Search |
| `F5` | Rescan library |
| `Esc` / `Alt+←` | Clear search / back |
| Right-click a card | Resume, start over, mark watched / unwatched, open folder |

## Requirements

**Windows 10/11 (64-bit):** double-click `run.bat` (or run `./run.sh` in Git Bash). It
installs 64-bit Python, VLC and FFmpeg with `winget` when they are missing or 32-bit,
builds `.venv` and adds a Start menu entry. At the end every OS is asked once whether to
create a desktop shortcut (`--ask-shortcut` asks again).

**Linux** with X11 or Wayland (Chrysa runs through XWayland so the video stays inside the
window). `run.sh` installs these when they are missing and asks for `sudo` only then:

| | Debian / Ubuntu | Fedora | Arch |
| --- | --- | --- | --- |
| Player | `vlc` `libvlc-dev` | `vlc` `vlc-devel` | `vlc` (`vlc-plugins-all`) |
| Thumbnails | `ffmpeg` | `ffmpeg-free` | `ffmpeg` |
| Python | `python3-venv` `python3-pip` | `python3` `python3-pip` | `python` `python-pip` |
| Qt platform plugin | `libxcb-cursor0` | `xcb-util-cursor` | `xcb-util-cursor` |

Python packages (installed into `.venv`): PyQt6, python-vlc, guessit.

`run.sh` options: `--setup-only` (prepare everything, do not start),
`--skip-system-deps` (never touch system packages), and any Chrysa option below.

## Command line

```
./run.sh --scan --list          # rescan and print what Chrysa found, no window
./run.sh --thumbnails           # pre-generate every thumbnail
./run.sh --add-folder /mnt/media
```

## Where things live

| | |
| --- | --- |
| `~/.local/share/chrysa/chrysa.db` | library, watch progress, settings (SQLite) |
| `~/.local/share/chrysa/thumbnails/` | cached frame grabs |
| `~/.local/share/chrysa/chrysa.log` | output of the last launch from the app menu |
| `~/.local/share/applications/chrysa.desktop` | launcher entry (rewritten by `run.sh`) |

Chrysa never writes into your media folders. Renaming or moving a video keeps its
watch progress (files are recognised by size and name). If a library folder is on a
drive that is not connected, its entries are kept rather than deleted.

## How files are classified

| Looks like | Becomes |
| --- | --- |
| Folder with season sub-folders | TV show; loose videos beside the seasons are *Specials* |
| Folder named `Show.S01…` or holding mostly `S01E02` files | TV show (packs of one show merge) |
| Folder named with a year or release tags, one big video | Movie (smaller videos in it are ignored) |
| Loose file with `S01E02` | Episode of the show in its name |
| Loose file with a year or release tags | Movie — unless it runs under 20 minutes |
| Any other loose file | Other video — unless it is feature length (60 min+) |
| Recordings named after a date (`Screencast From 2026-09-08…`) | Other video |

To give something its own artwork, put `poster.jpg` in a movie or show folder, or an
image with the same name as an episode next to it.

## Troubleshooting

- **Green or garbled picture** — open the `⋮` menu and switch off *Hardware Video
  Decoding* (or start with `CHRYSA_HWDEC=0 ./run.sh`).
- **Something else is off with playback** — extra libVLC options can be passed with
  `CHRYSA_VLC_ARGS`, e.g. `CHRYSA_VLC_ARGS="--verbose=2" ./run.sh`.
- **A title was parsed wrongly** — rename the folder to `Title (Year)` and rescan.
- **Start from scratch** — delete `~/.local/share/chrysa/` (library and progress) or
  `.venv/` (Python environment); `run.sh` rebuilds both.

## Development

```
.venv/bin/python -m unittest discover -s tests -t .
```

```
chrysa/
  search.py      online search providers       downloads.py   background torrent client
  config.py      paths and thresholds          naming.py      guessit glue and sanity checks
  scanner.py     folder walk + classification  probe.py       ffprobe
  database.py    SQLite schema, connections    library.py     sync, queries, resume logic
  thumbnails.py  ffmpeg frame grabs            subtitles.py   external subtitle discovery
  models.py      data objects, formatting      cli.py / app.py  entry points
  ui/            theme, icons, cards, pages, player, main window
tests/           unit tests for the core, plus an offscreen smoke test of the browsing UI
```

Built with [PyQt6](https://www.riverbankcomputing.com/software/pyqt/),
[libVLC](https://www.videolan.org/vlc/libvlc.html), [FFmpeg](https://ffmpeg.org/) and
[guessit](https://github.com/guessit-io/guessit).
