# A12 Bypass — Windows 32-bit Edition

This folder adds a **Windows 10 (32-bit) EXE** to the A12_Bypass_OSS project.
The EXE is built automatically by GitHub Actions — you do NOT need Python,
or any tools, installed on your own PC to get it.

## How to get the EXE (no tools needed)

1. Push these files to your fork (see TERMUX_PUSH_GUIDE.md).
2. Open your repo on github.com → **Actions** tab → workflow
   **"Build Windows 32-bit EXE"**.
3. Wait ~5–10 minutes. Click the finished run → **Artifacts** →
   download **A12Bypass_Windows_win32** (you must be logged in to download).
4. Unzip it. You get:
   - `A12Bypass_Windows.exe` — the tool, with Python bundled inside
   - `assets\` — device configuration files (must stay next to the exe)
   - `GET_DRIVERS.txt` — how to install iPhone USB drivers

The EXE is 32-bit, so it runs on Windows 10 32-bit AND 64-bit.
Python is bundled — nothing else to install except the USB drivers.

## What was changed vs. the original macOS client

| Original (macOS only)         | This Windows port                                   |
|-------------------------------|-----------------------------------------------------|
| `ideviceinfo` external tool   | pymobiledevice3 API (in-process)                    |
| `/usr/bin/log --archive`      | live syslog stream via pymobiledevice3              |
| `ifuse` mount for file copy   | pymobiledevice3 AFC service                         |
| `curl` downloads              | Python `requests`                                   |
| Requires the PHP server       | **Offline mode**: payload generated locally, no server needed |
| Server mode                   | still available (menu option 2)                     |

## Two bugs found and fixed in the original code

1. **SQL templates crash**: `offline_bypass.py` (and the PHP server template)
   create a table with 44 columns but insert only 43 values — SQLite rejects
   it ("table asset has 44 columns but 43 values were supplied"). The missing
   column is `bytes_total` (5th). Fixed here and documented inside
   `payload_templates.py`. Note: the PHP server has this same bug, so server
   mode would fail the same way until you patch `server/templates/`.
2. **`LocalServer.start()` calls `os.chdir()`** — asset paths are now resolved
   to absolute paths before the server starts, otherwise generation fails
   depending on the working directory.

## Known limitations (honest list)

- The syslog-based GUID collection was ported from the macOS flow and follows
  the same matching logic, but has **not been verified against a real device**.
  If the GUID is not found, the tool tells you and exits cleanly.
- The 42 bundled MobileGestalt plists in `server/assets/Maker/` are of unknown
  origin/validity — they are used exactly as the original project uses them.
- Device flow was never executed end-to-end (no iPhone in the build sandbox).
- The EXE is unsigned: Windows SmartScreen / antivirus may show a warning.
- Offline mode serves files over your LAN — the iPhone and the PC must be on
  the same Wi-Fi network; the shown IP must be reachable from the phone.
- Legal: this software is for educational/research use on devices you are
  authorized to work on (per the original project's disclaimer).
