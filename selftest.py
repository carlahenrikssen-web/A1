"""
Build-time self-test: verifies the whole payload pipeline works on the
build machine WITHOUT any iPhone connected.
Fails the GitHub Actions build if anything is broken.
"""
import glob
import os
import re
import sqlite3
import sys
import zipfile

import app_windows

failures = []


def check(name, fn):
    try:
        fn()
        print(f"[PASS] {name}")
    except Exception as e:
        failures.append((name, e))
        print(f"[FAIL] {name}: {e}")


def test_imports():
    from pymobiledevice3.lockdown import create_using_usbmux  # noqa: F401
    from pymobiledevice3.services.afc import AfcService  # noqa: F401
    from pymobiledevice3.services.diagnostics import DiagnosticsService  # noqa: F401
    from pymobiledevice3.services.syslog import SyslogService  # noqa: F401


def test_assets_present():
    root = app_windows.find_assets_root()
    maker = root / "Maker"
    assert maker.is_dir(), f"assets/Maker missing at {maker}"
    devices = [d for d in os.listdir(maker) if (maker / d / "com.apple.MobileGestalt.plist").is_file()]
    assert len(devices) > 30, f"expected >30 device asset folders, found {len(devices)}"


def test_payload_generation():
    server = app_windows.LocalServer(port=18099)
    assets = os.path.abspath(os.path.join(app_windows.app_dir(), "assets"))
    server.start()
    try:
        gen = app_windows.PayloadGenerator(server.serve_dir, assets)
        db = gen.generate("iPhone11,8", "A1B2C3D4-0011-2233-4455-66778899AABB", "SELFTEST", server)
        assert db and os.path.exists(db), "generator returned no final DB"

        conn = sqlite3.connect(db)
        n = conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='asset'"
        ).fetchone()[0]
        rows = conn.execute("SELECT url, path_extension, http_method FROM asset").fetchall()
        conn.close()
        assert n == 1, "final DB missing 'asset' table"
        assert all(u.startswith("http://") for u, _, _ in rows), rows
        assert all(ext in ("plist", "epub") for _, ext, _ in rows), rows
        assert all(m == "GET" for _, _, m in rows), rows

        bl = glob.glob(os.path.join(server.serve_dir, "belliloveu_*.png"))
        assert len(bl) == 1, f"expected 1 BL db, found {len(bl)}"
        conn = sqlite3.connect(bl[0])
        zurl = conn.execute("SELECT ZURL FROM ZBLDOWNLOADINFO").fetchall()
        conn.close()
        assert zurl and "fixedfile_" in zurl[0][0], zurl

        ff = glob.glob(os.path.join(server.serve_dir, "fixedfile_*"))
        assert len(ff) == 1
        with zipfile.ZipFile(ff[0]) as zf:
            assert zf.namelist() == ["Caches/com.apple.MobileGestalt.plist"]
    finally:
        server.stop()


def test_guid_regex():
    guid_re = re.compile(
        r"SystemGroup/([0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
        r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})/"
    )
    m = guid_re.search("x SystemGroup/A1B2C3D4-0011-2233-4455-66778899AABB/ BLDatabaseManager")
    assert m and m.group(1).upper() == "A1B2C3D4-0011-2233-4455-66778899AABB"


if __name__ == "__main__":
    check("pymobiledevice3 imports (no av/sslpsk/qh3 needed)", test_imports)
    check("device assets bundled", test_assets_present)
    check("payload generation end-to-end", test_payload_generation)
    check("GUID extraction regex", test_guid_regex)
    if failures:
        print(f"\nSELF-TEST FAILED: {len(failures)} check(s)")
        sys.exit(1)
    print("\nALL SELF-TESTS PASSED")
