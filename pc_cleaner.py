#!/usr/bin/env python3
"""
PC Cache Cleaner - Windows only - Console version (step 1)

Scans well-known cache/temp locations, shows them as sections with sizes,
and lets the user delete everything safe or pick sections.
Only touches the locations listed in SECTIONS. Never follows junctions/symlinks.

Run:  python pc_cleaner.py
"""
import ctypes
import glob
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass
from typing import Callable, List, Optional

if os.name != "nt":
    print("This tool works on Windows only.")
    sys.exit(1)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
os.system("")  # enables ANSI colors in the Windows console

REPARSE_POINT = 0x400  # junctions / symlinks: we never enter them
FORBIDDEN = ("\\windows\\prefetch", "\\windows\\winsxs", "pagefile.sys", "hiberfil.sys")

# ---- Update settings: put YOUR GitHub username and repository name here ----
VERSION = "1.0"
GITHUB_USER = "felomelad623-lang"
GITHUB_REPO = "pc-cleaner"
UPDATES_ENABLED = "YOUR_" not in GITHUB_USER + GITHUB_REPO
RAW_BASE = f"https://raw.githubusercontent.com/{GITHUB_USER}/{GITHUB_REPO}/main/"

C_RESET, C_GREEN, C_YELLOW, C_RED, C_CYAN = "\033[0m", "\033[92m", "\033[93m", "\033[91m", "\033[96m"


# ----------------------------------------------------------------- helpers
def env(name: str) -> str:
    return os.environ.get(name, "")


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def is_forbidden(path: str) -> bool:
    low = path.lower()
    return any(f in low for f in FORBIDDEN)


def is_reparse(path: str) -> bool:
    try:
        return bool(getattr(os.lstat(path), "st_file_attributes", 0) & REPARSE_POINT)
    except OSError:
        return True


def walk(root: str):
    """Return (files[(path, size)], dirs[path]) under root. Skips junctions/symlinks."""
    files, dirs, stack = [], [], [root]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    try:
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if getattr(st, "st_file_attributes", 0) & REPARSE_POINT:
                        continue
                    if stat.S_ISDIR(st.st_mode):
                        if not is_forbidden(e.path):
                            dirs.append(e.path)
                            stack.append(e.path)
                    elif not is_forbidden(e.path):
                        files.append((e.path, st.st_size))
            
        except OSError:
            continue
    return files, dirs


def remove_file(path: str) -> bool:
    try:
        os.remove(path)
        return True
    except PermissionError:
        try:
            os.chmod(path, stat.S_IWRITE)
            os.remove(path)
            return True
        except OSError:
            return False
    except OSError:
        return False


# ------------------------------------------------------------ recycle bin
class SHQUERYRBINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("i64Size", ctypes.c_longlong), ("i64NumItems", ctypes.c_longlong)]


def recycle_info():
    info = SHQUERYRBINFO()
    info.cbSize = ctypes.sizeof(SHQUERYRBINFO)
    try:
        if ctypes.windll.shell32.SHQueryRecycleBinW(None, ctypes.byref(info)) == 0:
            return info.i64Size, info.i64NumItems
    except Exception:
        pass
    return 0, 0


def empty_recycle():
    # NOCONFIRMATION | NOPROGRESSUI | NOSOUND
    return ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, 0x7) == 0


# ---------------------------------------------------------------- sections
@dataclass
class Section:
    name: str
    desc: str
    level: str                      # "safe" | "caution" | "risky"
    kind: str                       # "files" | "recycle" | "cleanmgr" | "downloads"
    targets: Optional[Callable[[], List[str]]] = None
    reg_key: Optional[str] = None   # cleanmgr VolumeCaches key
    approx: bool = False            # size shown is only an estimate


def t_temp():
    return [env("TEMP"), os.path.join(env("SystemRoot"), "Temp")]


def t_browsers():
    local, out = env("LOCALAPPDATA"), []
    for base in (os.path.join(local, "Google", "Chrome", "User Data"),
                 os.path.join(local, "Microsoft", "Edge", "User Data")):
        for prof in glob.glob(os.path.join(base, "Default")) + glob.glob(os.path.join(base, "Profile *")):
            for sub in ("Cache", "Code Cache", "GPUCache"):
                out.append(os.path.join(prof, sub))
    out += glob.glob(os.path.join(local, "Mozilla", "Firefox", "Profiles", "*", "cache2"))
    return out


def t_thumbs():
    d = os.path.join(env("LOCALAPPDATA"), "Microsoft", "Windows", "Explorer")
    return glob.glob(os.path.join(d, "thumbcache_*.db")) + glob.glob(os.path.join(d, "iconcache_*.db"))


def t_errors():
    local, pdata, root = env("LOCALAPPDATA"), env("ProgramData"), env("SystemRoot")
    return [
        os.path.join(local, "Microsoft", "Windows", "WER"),
        os.path.join(local, "CrashDumps"),
        os.path.join(pdata, "Microsoft", "Windows", "WER", "ReportArchive"),
        os.path.join(pdata, "Microsoft", "Windows", "WER", "ReportQueue"),
        os.path.join(root, "Minidump"),
        os.path.join(root, "MEMORY.DMP"),
    ]


def t_recent():
    return [os.path.join(env("APPDATA"), "Microsoft", "Windows", "Recent")]


def t_update():
    return [os.path.join(env("SystemRoot"), "SoftwareDistribution", "Download")]


def t_delivery():
    return [os.path.join(env("SystemRoot"), "ServiceProfiles", "NetworkService", "AppData", "Local",
                         "Microsoft", "Windows", "DeliveryOptimization", "Cache")]


def t_winold():
    return [os.path.join(env("SystemDrive") or "C:", os.sep, "Windows.old")]


def t_downloads():
    return [os.path.join(os.path.expanduser("~"), "Downloads")]


SECTIONS: List[Section] = [
    Section("Temp files", "Temporary files made by Windows and programs (%TEMP% and C:\\Windows\\Temp)", "safe", "files", t_temp),
    Section("Browser cache", "Chrome / Edge / Firefox cached pages and images. Close browsers first.", "safe", "files", t_browsers),
    Section("Thumbnails", "Small preview images of files. Windows rebuilds them.", "safe", "files", t_thumbs),
    Section("Error reports & dumps", "Old crash reports and memory dump files.", "safe", "files", t_errors),
    Section("Recent files list", "Shortcuts to recently opened files (not the files themselves).", "safe", "files", t_recent),
    Section("Recycle Bin", "Files you already deleted. You will not be able to restore them.", "caution", "recycle"),
    Section("Windows Update cleanup", "Old update files. Cleaned by Windows' own tool. Size is approximate.", "caution",
            "cleanmgr", t_update, "Update Cleanup", True),
    Section("Delivery Optimization", "Update files cached for sharing. Cleaned by Windows' own tool.", "caution",
            "cleanmgr", t_delivery, "Delivery Optimization Files"),
    Section("Windows.old", "Your previous Windows version. After this you CANNOT roll back the update.", "risky",
            "cleanmgr", t_winold, "Previous Installations"),
    Section("Downloads folder", "YOUR files. You will pick them one by one.", "risky", "downloads", t_downloads),
]


# -------------------------------------------------------------------- scan
def scan(s: Section):
    """Return (size, count)."""
    if s.kind == "recycle":
        return recycle_info()
    size = count = 0
    for t in (s.targets() if s.targets else []):
        if is_forbidden(t) or not os.path.exists(t) or is_reparse(t):
            continue
        if os.path.isfile(t):
            try:
                size += os.path.getsize(t)
                count += 1
            except OSError:
                pass
        else:
            files, _ = walk(t)
            size += sum(sz for _, sz in files)
            count += len(files)
    return size, count


# ------------------------------------------------------------------ delete
def delete_files_section(s: Section):
    freed = deleted = skipped = 0
    for t in s.targets():
        if is_forbidden(t) or not os.path.exists(t) or is_reparse(t):
            continue
        if os.path.isfile(t):
            try:
                sz = os.path.getsize(t)
            except OSError:
                sz = 0
            if remove_file(t):
                freed, deleted = freed + sz, deleted + 1
            else:
                skipped += 1
            continue
        files, dirs = walk(t)
        for path, sz in files:
            if remove_file(path):
                freed, deleted = freed + sz, deleted + 1
            else:
                skipped += 1  # in use or no permission: skipped, not an error
        for d in sorted(dirs, key=len, reverse=True):  # deepest first, never the root itself
            try:
                os.rmdir(d)
            except OSError:
                pass
    return freed, deleted, skipped


def run_cleanmgr(keys: List[str]):
    import winreg
    base = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\VolumeCaches"
    armed = []
    for k in keys:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base + "\\" + k, 0, winreg.KEY_SET_VALUE) as h:
                winreg.SetValueEx(h, "StateFlags0099", 0, winreg.REG_DWORD, 2)
            armed.append(k)
        except OSError:
            print(f"  - '{k}' is not available on this Windows, skipped.")
    if not armed:
        return
    print("  Windows Disk Cleanup is running (a small window may appear)...")
    subprocess.run(["cleanmgr", "/sagerun:99"])
    for k in armed:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base + "\\" + k, 0, winreg.KEY_SET_VALUE) as h:
                winreg.DeleteValue(h, "StateFlags0099")
        except OSError:
            pass


def downloads_flow(s: Section):
    folder = s.targets()[0]
    if not os.path.isdir(folder):
        print("  Downloads folder not found.")
        return
    items = []
    for name in os.listdir(folder):
        p = os.path.join(folder, name)
        if is_reparse(p):
            continue
        if os.path.isdir(p):
            files, _ = walk(p)
            items.append((p, sum(sz for _, sz in files)))
        else:
            try:
                items.append((p, os.path.getsize(p)))
            except OSError:
                pass
    items.sort(key=lambda x: x[1], reverse=True)
    items = items[:30]
    if not items:
        print("  Downloads is empty.")
        return
    print("\n  Largest items in Downloads:")
    for i, (p, sz) in enumerate(items, 1):
        print(f"   {i:>2}. {human(sz):>10}  {os.path.basename(p)}")
    raw = input("\n  Numbers to DELETE (e.g. 1 3 5), or Enter to skip: ").replace(",", " ").split()
    chosen = [items[int(x) - 1] for x in raw if x.isdigit() and 1 <= int(x) <= len(items)]
    if not chosen:
        print("  Nothing selected.")
        return
    print("  You are about to delete:")
    for p, sz in chosen:
        print(f"   - {os.path.basename(p)} ({human(sz)})")
    if input("  Type YES to confirm: ").strip() != "YES":
        print("  Cancelled.")
        return
    freed = 0
    for p, sz in chosen:
        try:
            shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
            freed += sz
        except OSError:
            print(f"  Could not delete {os.path.basename(p)} (in use?).")
    print(f"  Freed {human(freed)} from Downloads.")


# ----------------------------------------------------------------- updates
def fetch_json(name: str):
    """Download a small JSON file from your GitHub repo. Returns None on any problem."""
    try:
        with urllib.request.urlopen(RAW_BASE + name, timeout=5) as r:
            return json.loads(r.read(200_000).decode("utf-8"))
    except Exception:
        return None


def ver_tuple(v: str):
    return tuple(int(x) for x in v.split("."))


def check_self_update() -> bool:
    """Offer a new .exe if one exists. Returns True if the program should close now."""
    if not UPDATES_ENABLED or not getattr(sys, "frozen", False):
        return False
    info = fetch_json("version.json")
    if not isinstance(info, dict):
        return False
    new, sha = info.get("version"), str(info.get("sha256", "")).lower()
    if not (isinstance(new, str) and re.fullmatch(r"\d+(\.\d+)*", new) and re.fullmatch(r"[0-9a-f]{64}", sha)):
        return False
    if ver_tuple(new) <= ver_tuple(VERSION):
        return False
    print(f"\nNew version available: {new} (you have {VERSION}).")
    if input("Download and install it now? (y/n): ").strip().lower() != "y":
        return False

    # The download address is built from YOUR repo only, never taken from the internet.
    url = f"https://github.com/{GITHUB_USER}/{GITHUB_REPO}/releases/download/v{new}/pc_cleaner.exe"
    new_exe = os.path.join(tempfile.gettempdir(), "pc_cleaner_new.exe")
    try:
        h = hashlib.sha256()
        with urllib.request.urlopen(url, timeout=30) as r, open(new_exe, "wb") as f:
            while True:
                chunk = r.read(1024 * 256)
                if not chunk:
                    break
                h.update(chunk)
                f.write(chunk)
    except Exception as e:
        print(f"Download failed ({e}). Continuing with the current version.")
        return False
    if h.hexdigest() != sha:
        print("Downloaded file did not match its fingerprint. Update cancelled for safety.")
        try:
            os.remove(new_exe)
        except OSError:
            pass
        return False

    old_exe = sys.executable
    bat = os.path.join(tempfile.gettempdir(), "pc_cleaner_update.bat")
    with open(bat, "w") as f:
        f.write(
            "@echo off\nset n=0\n:retry\nset /a n+=1\nif %n% GTR 20 exit /b\n"
            "ping -n 2 127.0.0.1 >nul\n"
            f'move /y "{new_exe}" "{old_exe}" >nul 2>&1\n'
            "if errorlevel 1 goto retry\n"
            f'start "" "{old_exe}"\n'
            'del "%~f0"\n'
        )
    subprocess.Popen(["cmd", "/c", bat], creationflags=0x08000000)  # no console window
    print("Updating... the program will reopen by itself.")
    return True


def path_is_allowed_for_remote(path: str) -> bool:
    """Remote sections may only point inside user/app-data folders, never anywhere else."""
    norm = os.path.normcase(os.path.abspath(path))
    if is_forbidden(norm):
        return False
    roots = [env("LOCALAPPDATA"), env("APPDATA"), env("ProgramData")]
    for root in roots:
        if not root:
            continue
        r = os.path.normcase(os.path.abspath(root))
        if norm.startswith(r + os.sep):
            depth = len(norm[len(r) + 1:].split(os.sep))
            return depth >= 2  # never a top-level app-data folder itself
    return False


def load_remote_sections() -> List[Section]:
    """Extra cleanup sections from sections.json. They can only be 'caution' file sections."""
    if not UPDATES_ENABLED:
        return []
    data = fetch_json("sections.json")
    if not isinstance(data, dict) or not isinstance(data.get("sections"), list):
        return []
    known = {s.name.lower() for s in SECTIONS}
    out = []
    for item in data["sections"][:30]:
        try:
            name, desc, paths = item["name"], item["desc"], item["paths"]
            if not (isinstance(name, str) and isinstance(desc, str) and isinstance(paths, list)):
                continue
            if not (1 <= len(name) <= 24) or name.lower() in known:
                continue
            paths = [p for p in paths if isinstance(p, str)][:20]

            def make_targets(pp=paths):
                found = []
                for p in pp:
                    expanded = os.path.expandvars(p)
                    if "%" in expanded:
                        continue
                    found += [m for m in glob.glob(expanded) if path_is_allowed_for_remote(m)]
                return found

            out.append(Section(name, desc[:120], "caution", "files", make_targets))
            known.add(name.lower())
        except (KeyError, TypeError):
            continue
    return out


# --------------------------------------------------------------------- UI
LEVEL_LABEL = {"safe": (C_GREEN, "SAFE"), "caution": (C_YELLOW, "CAUTION"), "risky": (C_RED, "RISKY")}


def show_table(results, admin):
    print(f"\n{C_CYAN}{'#':>2}  {'Section':<24}{'Size':>11}{'Files':>9}  Level{C_RESET}")
    print("-" * 62)
    for i, (s, size, count) in enumerate(results, 1):
        col, label = LEVEL_LABEL[s.level]
        tilde = "~" if s.approx and size else ""
        note = "" if (s.kind != "cleanmgr" or admin) else "  (needs Admin)"
        print(f"{i:>2}  {s.name:<24}{tilde + human(size):>11}{count:>9}  {col}{label}{C_RESET}{note}")
    total = sum(r[1] for r in results if r[0].level == "safe")
    print("-" * 62)
    print(f"Total in SAFE sections: {human(total)}")


def scan_all():
    print("Scanning... (can take a few seconds)")
    return [(s, *scan(s)) for s in SECTIONS]


def preview(s: Section):
    if s.kind == "recycle":
        print("  Recycle Bin: contents are managed by Windows; open it from the desktop to look inside.")
        return
    if s.kind == "downloads":
        downloads_flow_preview = s.targets()[0]
        print(f"  Folder: {downloads_flow_preview}  (you will pick items one by one)")
        return
    print(f"  {s.desc}\n  Locations:")
    for t in s.targets():
        if os.path.exists(t):
            print(f"   - {t}")
    biggest = []
    for t in s.targets():
        if os.path.isdir(t) and not is_reparse(t):
            biggest += walk(t)[0]
        elif os.path.isfile(t):
            biggest.append((t, os.path.getsize(t)))
    biggest.sort(key=lambda x: x[1], reverse=True)
    print("  Largest files:")
    for p, sz in biggest[:10]:
        print(f"   {human(sz):>10}  {p}")
    if not biggest:
        print("   (nothing found)")


def clean(selected, admin):
    print("\nYou are about to clean:")
    for s, size, _ in selected:
        col, label = LEVEL_LABEL[s.level]
        print(f"  - {s.name} ({human(size)}) [{col}{label}{C_RESET}]")
        if s.level == "risky":
            print(f"      {C_RED}WARNING: {s.desc}{C_RESET}")
    if input("\nType YES to confirm: ").strip() != "YES":
        print("Cancelled. Nothing was deleted.")
        return
    total = 0
    cm_keys = []
    for s, _, _ in selected:
        print(f"\n[{s.name}]")
        if s.kind == "files":
            freed, deleted, skipped = delete_files_section(s)
            total += freed
            print(f"  Deleted {deleted} files, freed {human(freed)}, skipped {skipped} (in use).")
        elif s.kind == "recycle":
            print("  Recycle Bin emptied." if empty_recycle() else "  Could not empty the Recycle Bin.")
        elif s.kind == "cleanmgr":
            if admin:
                cm_keys.append(s.reg_key)
            else:
                print("  Needs Administrator. Skipped.")
        elif s.kind == "downloads":
            downloads_flow(s)
    if cm_keys:
        print("\n[Windows tools]")
        run_cleanmgr(cm_keys)
    print(f"\nDone. Freed from direct cleaning: {human(total)} (Windows-tool sections are not counted here).")


def parse_numbers(text, n):
    out = []
    for tok in text.replace(",", " ").split():
        if tok.isdigit() and 1 <= int(tok) <= n and int(tok) not in out:
            out.append(int(tok))
    return out


def main():
    print(f"PC Cache Cleaner v{VERSION}")
    if "--no-update" not in sys.argv:
        if check_self_update():
            return
        SECTIONS.extend(load_remote_sections())
    admin = is_admin()
    if not admin and "--no-admin" not in sys.argv:
        print("Windows-tool sections (Update, Delivery Optimization, Windows.old) need Administrator.")
        if input("Restart as Administrator? (y/n): ").strip().lower() == "y":
            args = " ".join(f'"{a}"' for a in sys.argv)
            if ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, args, None, 1) > 32:
                return
            print("Could not get Administrator rights. Continuing without.")
    results = scan_all()
    while True:
        show_table(results, admin)
        print("\nCommands:  a = all SAFE+CAUTION  |  1 3 5 = pick numbers  |  p N = preview  |  r = rescan  |  q = quit")
        cmd = input("> ").strip().lower()
        if cmd in ("q", "quit", "exit"):
            break
        if cmd == "r":
            results = scan_all()
        elif cmd.startswith("p"):
            nums = parse_numbers(cmd[1:], len(results))
            if nums:
                preview(results[nums[0] - 1][0])
            else:
                print("Usage: p 2")
        elif cmd == "a":
            sel = [r for r in results if r[0].level != "risky" and (r[1] > 0 or r[0].kind == "recycle")]
            if sel:
                clean(sel, admin)
                results = scan_all()
            else:
                print("Nothing to clean.")
        else:
            nums = parse_numbers(cmd, len(results))
            if nums:
                clean([results[i - 1] for i in nums], admin)
                results = scan_all()
            else:
                print("Not understood.")
    if "--no-admin" not in sys.argv:
        input("\nPress Enter to close...")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
