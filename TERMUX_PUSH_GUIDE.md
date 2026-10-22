# How to push the build files from Termux (copy-paste, ~5 minutes)

You are pushing 7 small text files to your fork
`https://github.com/carlahenrikssen-web/A1`.
After that, GitHub's computers build the EXE for you — you need
nothing installed on any PC.

---

## STEP 0 — One time: create a GitHub token (your "password" for git)

1. On any browser: github.com -> log in -> click your profile picture
   -> **Settings** -> **Developer settings** (bottom) ->
   **Personal access tokens** -> **Tokens (classic)** -> **Generate new token (classic)**.
2. Note/name: anything. Expiration: 90 days. Check the **repo** checkbox.
3. Click **Generate token** and **copy the token** (starts with `ghp_`).
   Keep it private.

## STEP 1 — In Termux: install git and download your fork

```bash
pkg install -y git nano
cd ~
git clone https://github.com/carlahenrikssen-web/A1.git
cd A1
git config user.email "you@example.com"
git config user.name "carlahenrikssen-web"
```

## STEP 2 — Create each file: paste content, save, next

```bash
mkdir -p .github/workflows
nano .github/workflows/build-windows.yml
```

- Long-press in the terminal -> **Paste** (paste the matching file's content)
- Save & exit: **Ctrl+O, Enter** (volume-down+O on Termux keys), then **Ctrl+X**
- Repeat for each file below:

| File to create (nano FILENAME)   | Where the content comes from |
|----------------------------------|------------------------------|
| `.github/workflows/build-windows.yml` | file 1 below |
| `app_windows.py`                 | file 2 below |
| `payload_templates.py`           | file 3 below |
| `requirements-win32.txt`         | file 4 below |
| `selftest.py`                    | file 5 below |
| `README_WINDOWS.md`              | file 6 below |
| `GET_DRIVERS.txt`                | file 7 below |

All of them go in the **A1 folder root** (do not put app_windows.py
inside client/ or anywhere else).

## STEP 3 — Verify the files are complete

```bash
wc -c app_windows.py payload_templates.py requirements-win32.txt selftest.py README_WINDOWS.md GET_DRIVERS.txt .github/workflows/build-windows.yml
```

Expected sizes (a few bytes off is fine, half the size = paste got cut,
redo that file):

```
  16922 app_windows.py
  20216 payload_templates.py
   ~1300 requirements-win32.txt
   ~3500 selftest.py
   ~3300 README_WINDOWS.md
   ~1100 GET_DRIVERS.txt
   ~2500 .github/workflows/build-windows.yml
```

## STEP 4 — Push

```bash
git add -A
git commit -m "Add Windows 32-bit EXE build (GitHub Actions)"
git push
```

- Username: `carlahenrikssen-web`
- Password: **paste the ghp_ token** (not your GitHub password)

If Actions asks to be enabled: open the repo on github.com ->
**Actions** tab -> green button **"I understand my workflows, go ahead
and enable them"**.

## STEP 5 — Get the EXE

1. Repo -> **Actions** tab -> workflow **"Build Windows 32-bit EXE"**
   (starts by itself after the push).
2. Wait 5-10 minutes; the run turns green (a yellow dot = still building).
3. Open the run -> **Artifacts** (bottom) -> download
   **A12Bypass_Windows_win32** (must be logged in to download).
4. Unzip on the Windows PC and read README_WINDOWS.md inside.

Optional — permanent download link (a Release):
```bash
git tag v1.0
git push origin v1.0
```
Repo -> **Releases** -> the ZIP appears there (one-time setting:
repo Settings -> Actions -> General -> Workflow permissions ->
"Read and write permissions" -> Save, before pushing the tag).

## Troubleshooting

- **"everything up-to-date" / push rejected** -> run `git pull` first, push again.
- **token rejected** -> generate a new classic token with the `repo` scope.
- **build fails on Actions** -> open the failed run, read which step is red,
  compare your `wc -c` sizes with STEP 3 (a truncated paste is the usual cause).
- **Too fiddly?** Alternative without Termux: on github.com press `.` (dot)
  on your repo to open github.dev in the browser -> drag the 7 files in ->
  commit -> push. Same result.
