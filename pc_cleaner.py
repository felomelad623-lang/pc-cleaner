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
import time
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
if sys.stdout:
    os.system("")  # enables ANSI colors in the Windows console

REPARSE_POINT = 0x400  # junctions / symlinks: we never enter them
FORBIDDEN = ("\\windows\\prefetch", "\\windows\\winsxs", "pagefile.sys", "hiberfil.sys")

# ---- Update settings: put YOUR GitHub username and repository name here ----
VERSION = "2.3"
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


def find_update():
    """Return (version, sha) if a newer .exe exists on your GitHub, else None."""
    if not UPDATES_ENABLED or not getattr(sys, "frozen", False):
        return None
    info = fetch_json("version.json")
    if not isinstance(info, dict):
        return None
    new, sha = info.get("version"), str(info.get("sha256", "")).lower()
    if not (isinstance(new, str) and re.fullmatch(r"\d+(\.\d+)*", new) and re.fullmatch(r"[0-9a-f]{64}", sha)):
        return None
    if ver_tuple(new) <= ver_tuple(VERSION):
        return None
    return new, sha


def install_update(new: str, sha: str) -> bool:
    """Download the new .exe, check its fingerprint, swap it in. True = program must close now."""
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
    return True


def check_self_update() -> bool:
    """Console mode: offer a new .exe. Returns True if the program should close now."""
    found = find_update()
    if not found:
        return False
    new, sha = found
    print(f"\nNew version available: {new} (you have {VERSION}).")
    if input("Download and install it now? (y/n): ").strip().lower() != "y":
        return False
    if install_update(new, sha):
        print("Updating... the program will reopen by itself.")
        return True
    return False


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


# ------------------------------------------------ restore point & quarantine
QUARANTINE_DAYS = 14
QROOT = os.path.join(env("LOCALAPPDATA"), "PCCleaner", "Quarantine")
QMANIFEST = os.path.join(QROOT, "manifest.json")


def create_restore_point() -> bool:
    """Ask Windows for a System Restore point. Needs Administrator + System Protection turned on."""
    if not is_admin():
        return False
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
             "Checkpoint-Computer -Description 'PC Cleaner' -RestorePointType MODIFY_SETTINGS -WarningAction Stop"],
            capture_output=True, timeout=180, creationflags=0x08000000)
        return r.returncode == 0
    except Exception:
        return False


def q_load():
    try:
        with open(QMANIFEST, encoding="utf-8") as f:
            data = json.load(f)
        return [e for e in data if isinstance(e, dict) and re.fullmatch(r"\d+_[0-9a-f]{6}", str(e.get("id", "")))]
    except Exception:
        return []


def q_save(entries):
    os.makedirs(QROOT, exist_ok=True)
    with open(QMANIFEST, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False)


def q_move_in(path, size, isdir, entries) -> bool:
    """Move an item into quarantine (same drive only, so it is an instant, safe rename)."""
    drive = lambda x: os.path.splitdrive(os.path.abspath(x))[0].lower()
    if drive(path) != drive(QROOT):
        return False  # different drive: refuse, so nothing is deleted without a backup
    qid = f"{int(time.time())}_{os.urandom(3).hex()}"
    dest_dir = os.path.join(QROOT, qid)
    try:
        os.makedirs(dest_dir)
        os.rename(path, os.path.join(dest_dir, os.path.basename(path)))
    except OSError:
        try:
            os.rmdir(dest_dir)
        except OSError:
            pass
        return False
    entries.append({"id": qid, "name": os.path.basename(path), "orig": path,
                    "size": size, "isdir": isdir, "time": time.time()})
    return True


def q_purge_expired():
    entries = q_load()
    keep = []
    for e in entries:
        if time.time() - float(e.get("time", 0)) > QUARANTINE_DAYS * 86400:
            shutil.rmtree(os.path.join(QROOT, e["id"]), ignore_errors=True)
        else:
            keep.append(e)
    if len(keep) != len(entries):
        q_save(keep)


def q_restore(e) -> bool:
    name = os.path.basename(str(e.get("name", "")))
    src = os.path.join(QROOT, e["id"], name)
    dest = str(e.get("orig", ""))
    home = os.path.normcase(os.path.abspath(os.path.expanduser("~")))
    if not name or not os.path.exists(src) or not os.path.normcase(os.path.abspath(dest)).startswith(home + os.sep):
        return False
    if os.path.exists(dest):
        base, ext = os.path.splitext(dest)
        dest = f"{base} (restored){ext}"
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        os.rename(src, dest)
        os.rmdir(os.path.join(QROOT, e["id"]))
        return True
    except OSError:
        return False


# ------------------------------------------------------- winget apps & prefs
# The list is fixed inside the exe. The page only ever sends a key from this list,
# never an ID or a command, so nothing else can be installed.
APPS = [  # (key, group, name, winget id, note)
    ("chrome", "المتصفحات", "Google Chrome", "Google.Chrome", ""),
    ("firefox", "المتصفحات", "Mozilla Firefox", "Mozilla.Firefox", ""),
    ("brave", "المتصفحات", "Brave", "Brave.Brave", ""),
    ("edge", "المتصفحات", "Microsoft Edge", "Microsoft.Edge", "غالباً متثبّت مع ويندوز، ولو موجود هيتحدّث"),
    ("opera", "المتصفحات", "Opera", "Opera.Opera", ""),
    ("operagx", "المتصفحات", "Opera GX", "Opera.OperaGX", "متصفح للألعاب"),
    ("vivaldi", "المتصفحات", "Vivaldi", "Vivaldi.Vivaldi", ""),
    ("thorium", "المتصفحات", "Thorium Browser", "Alex313031.Thorium.AVX2", "نسخة AVX2، لازم معالج حديث يدعمها"),
    ("tor", "المتصفحات", "Tor Browser", "TorProject.TorBrowser", "للتصفح المجهول، وأبطأ من العادي"),
    ("waterfox", "المتصفحات", "Waterfox", "Waterfox.Waterfox", ""),
    ("vlc", "الميديا", "VLC", "VideoLAN.VLC", ""),
    ("klite", "الميديا", "K-Lite Codec Pack (Full)", "CodecGuide.K-LiteCodecPack.Full", ""),
    ("7zip", "الضغط", "7-Zip", "7zip.7zip", ""),
    ("winrar", "الضغط", "WinRAR", "RARLab.WinRAR", "برنامج مدفوع وبيشتغل بفترة تجربة"),
    ("telegram", "التواصل", "Telegram", "Telegram.TelegramDesktop", ""),
    ("whatsapp", "التواصل", "WhatsApp", "WhatsApp.WhatsApp", "من متجر مايكروسوفت، وممكن يفشل على بعض الأجهزة"),
    ("discord", "التواصل", "Discord", "Discord.Discord", ""),
    ("foxit", "قارئ PDF", "Foxit PDF Reader", "Foxit.FoxitReader", ""),
    ("adobe", "قارئ PDF", "Adobe Acrobat Reader (64-bit)", "Adobe.Acrobat.Reader.64-bit", ""),
    ("vcredist", "حزم الألعاب", "Visual C++ 2015-2022 (x64)", "Microsoft.VCRedist.2015+.x64", ""),
    ("directx", "حزم الألعاب", "DirectX End-User Runtime", "Microsoft.DirectX", ""),
    ("anydesk", "أدوات", "AnyDesk", "AnyDesk.AnyDesk", ""),
    ("idm", "أدوات", "Internet Download Manager", "Tonec.InternetDownloadManager", "برنامج مدفوع وبيشتغل بفترة تجربة"),
    ("bluestacks", "المحاكيات", "BlueStacks", "BlueStack.BlueStacks", "محاكي أندرويد لتشغيل الألعاب والتطبيقات"),
    ("retroarch", "المحاكيات", "RetroArch", "Libretro.RetroArch", "محاكي شامل لألعاب الأجهزة القديمة"),
    ("pcsx2", "المحاكيات", "PCSX2", "PCSX2Team.PCSX2", "محاكي PlayStation 2"),
    ("duckstation", "المحاكيات", "DuckStation", "Stenzek.DuckStation", "محاكي PlayStation 1"),
    ("ppsspp", "المحاكيات", "PPSSPP", "PPSSPPTeam.PPSSPP", "محاكي PSP"),
    ("dolphin", "المحاكيات", "Dolphin", "DolphinEmulator.Dolphin", "محاكي GameCube وWii"),
    ("cemu", "المحاكيات", "Cemu", "Cemu.Cemu", "محاكي Wii U"),
    ("cpuz", "برامج النظام", "CPU-Z", "CPUID.CPU-Z", "معلومات المعالج واللوحة والرامات"),
    ("gpuz", "برامج النظام", "GPU-Z", "TechPowerUp.GPU-Z", "معلومات كارت الشاشة"),
    ("hwmonitor", "برامج النظام", "HWMonitor", "CPUID.HWMonitor", "مراقبة الحرارة والفولت وسرعة المراوح"),
    ("rufus", "برامج النظام", "Rufus", "Rufus.Rufus", "تجهيز فلاشة بوت لتثبيت ويندوز"),
    ("ventoy", "برامج النظام", "Ventoy", "Ventoy.Ventoy", "فلاشة بوت واحدة لأكتر من نسخة ويندوز"),
    ("crystaldisk", "برامج النظام", "CrystalDiskInfo", "CrystalDewWorld.CrystalDiskInfo", "فحص صحة الهارد والـ SSD"),
    ("aida64", "برامج النظام", "AIDA64 Extreme", "FinalWire.AIDA64.Extreme", "برنامج مدفوع وبيشتغل بفترة تجربة"),
    ("sysinformer", "برامج النظام", "System Informer (Process Hacker)", "WinsiderSS.SystemInformer", "الاسم الجديد لبرنامج Process Hacker"),
    ("revo", "برامج النظام", "Revo Uninstaller", "RevoUninstaller.RevoUninstaller", "إزالة البرامج مع بقاياها"),
    ("autoruns", "برامج النظام", "Autoruns", "Microsoft.Sysinternals.Autoruns", "نسخة محمولة، وبتعرض اللي بيشتغل مع بدء ويندوز"),
    ("speccy", "برامج النظام", "Speccy", "Piriform.Speccy", "مواصفات الجهاز وحرارة المكوّنات"),
    ("sdio", "برامج النظام", "Snappy Driver Installer Origin", "GlennDelahoy.SnappyDriverInstallerOrigin", "تحديث وتثبيت الدرايفرات، وبديل DriverPack"),
    ("driverbooster", "برامج النظام", "IObit Driver Booster", "IObit.DriverBooster", "تحديث الدرايفرات تلقائي"),
    ("kaspersky", "برامج الحماية", "Kaspersky Security Cloud", "Kaspersky.SecurityCloud", "برنامج مدفوع، وبيحتاج ترخيص أو فترة تجربة"),
    ("bitdefender", "برامج الحماية", "Bitdefender Antivirus", "Bitdefender.Bitdefender", "نسخة Antivirus Free Edition"),
    ("malwarebytes", "برامج الحماية", "Malwarebytes", "Malwarebytes.Malwarebytes", "فحص وإزالة البرمجيات الخبيثة، وبفترة تجربة للحماية الكاملة"),
    ("eset", "برامج الحماية", "ESET (NOD32 / Security)", "ESET.Security", "برنامج مدفوع، وبيحتاج ترخيص أو فترة تجربة"),
]
APP_BY_KEY = {a[0]: a for a in APPS}
NO_SOURCE_FLAG = {"whatsapp"}                 # Store-based package: let winget pick the source
WINGET_ALREADY = {0x8A15002B, 0x8A150061}     # "already installed" / "no newer version"
WINGET_TIMEOUT = 1800                         # seconds per program

PREFS = os.path.join(env("LOCALAPPDATA"), "PCCleaner", "prefs.json")


def prefs_load() -> dict:
    try:
        with open(PREFS, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def prefs_save(d: dict):
    try:
        os.makedirs(os.path.dirname(PREFS), exist_ok=True)
        with open(PREFS, "w", encoding="utf-8") as f:
            json.dump(d, f)
    except OSError:
        pass


def find_winget() -> Optional[str]:
    p = shutil.which("winget")
    if p:
        return p
    cand = os.path.join(env("LOCALAPPDATA"), "Microsoft", "WindowsApps", "winget.exe")
    return cand if os.path.exists(cand) else None


def winget_install(key: str):
    """Install one program silently. Returns (state, code): state is ok | had | fail."""
    app = APP_BY_KEY.get(key)
    if not app:
        return "fail", "bad-key"
    exe = find_winget()
    if not exe:
        return "fail", "no-winget"
    cmd = [exe, "install", "--id", app[3], "-e", "--silent",
           "--accept-package-agreements", "--accept-source-agreements", "--disable-interactivity"]
    if key not in NO_SOURCE_FLAG:
        cmd += ["--source", "winget"]
    try:
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True,
                           timeout=WINGET_TIMEOUT, creationflags=0x08000000)
    except subprocess.TimeoutExpired:
        return "fail", "timeout"
    except Exception:
        return "fail", "error"
    code = r.returncode & 0xFFFFFFFF
    if code == 0:
        return "ok", ""
    if code in WINGET_ALREADY:
        return "had", ""
    return "fail", f"0x{code:08X}"


# ============================================================ GUI (pywebview)
AR = {
    "Temp files": ("ملفات مؤقتة", "ملفات مؤقتة بيعملها ويندوز والبرامج"),
    "Browser cache": ("كاش المتصفحات", "صفحات وصور متخزنة من Chrome وEdge وFirefox. اقفل المتصفح الأول"),
    "Thumbnails": ("الصور المصغرة", "معاينات صغيرة للملفات، وويندوز بيعملها تاني"),
    "Error reports & dumps": ("تقارير الأعطال", "سجلات أعطال قديمة وملفات Dump"),
    "Recent files list": ("قائمة الملفات الأخيرة", "اختصارات للملفات اللي فتحتها قريب، مش الملفات نفسها"),
    "Recycle Bin": ("سلة المحذوفات", "ملفات اتمسحت قبل كده. مش هتقدر ترجعها"),
    "Windows Update cleanup": ("ملفات تحديث ويندوز القديمة", "بتتنضف بأداة ويندوز نفسها. الحجم تقريبي"),
    "Delivery Optimization": ("ملفات تحسين التسليم", "تحديثات متخزنة للمشاركة. بتتنضف بأداة ويندوز"),
    "Windows.old": ("Windows.old (النسخة القديمة)", "نسخة ويندوز السابقة. بعد مسحها مش هتقدر ترجع للتحديث القديم"),
    "Downloads folder": ("مجلد التنزيلات", "ملفاتك انت. هتختار بنفسك اللي يتمسح"),
}


class Api:
    """Functions the web page can call. The page never sends file paths, only section names / list numbers."""

    def __init__(self):
        self.remote_loaded = False
        self.found = None
        self.dl_items = []

    def get_state(self):
        if not self.remote_loaded:
            self.remote_loaded = True
            SECTIONS.extend(load_remote_sections())
        admin = is_admin()
        out = []
        for s in SECTIONS:
            size, count = scan(s)
            if s.kind == "cleanmgr" and size == 0 and s.name != "Windows Update cleanup":
                continue  # e.g. no Windows.old on this PC: hide it
            title, desc = AR.get(s.name, (s.name, s.desc))
            out.append({"name": s.name, "title": title, "desc": desc, "level": s.level, "kind": s.kind,
                        "size": size, "count": count, "approx": s.approx,
                        "locked": s.kind == "cleanmgr" and not admin})
        q_purge_expired()
        return {"version": VERSION, "sections": out, "admin": admin,
                "qcount": len(q_load()), "qdays": QUARANTINE_DAYS,
                "theme": prefs_load().get("theme") if prefs_load().get("theme") in ("light", "dark") else ""}

    def clean(self, names, restore_point=False):
        names = set(n for n in names if isinstance(n, str))
        restore = "off"
        if restore_point:
            restore = "ok" if create_restore_point() else "failed"
        freed = skipped = 0
        cm = []
        admin = is_admin()
        for s in SECTIONS:
            if s.name not in names or s.kind == "downloads":
                continue
            if s.kind == "files":
                f, _, sk = delete_files_section(s)
                freed, skipped = freed + f, skipped + sk
            elif s.kind == "recycle":
                size, _ = recycle_info()
                if empty_recycle():
                    freed += size
            elif s.kind == "cleanmgr" and admin:
                cm.append(s.reg_key)
        if cm:
            run_cleanmgr(cm)
        return {"freed": freed, "skipped": skipped, "windows_tool": bool(cm), "restore": restore}

    def downloads_list(self):
        folder = t_downloads()[0]
        items = []
        if os.path.isdir(folder):
            for name in os.listdir(folder):
                p = os.path.join(folder, name)
                if is_reparse(p):
                    continue
                try:
                    if os.path.isdir(p):
                        items.append((p, sum(sz for _, sz in walk(p)[0]), True))
                    else:
                        items.append((p, os.path.getsize(p), False))
                except OSError:
                    continue
        items.sort(key=lambda x: x[1], reverse=True)
        self.dl_items = items[:40]
        return [{"name": os.path.basename(p), "size": sz, "isdir": d} for p, sz, d in self.dl_items]

    def downloads_delete(self, idxs, keep=True):
        freed = kept_size = kept = failed = 0
        entries = q_load() if keep else []
        for i in idxs:
            if not isinstance(i, int) or not (0 <= i < len(self.dl_items)):
                continue
            p, sz, d = self.dl_items[i]
            if keep:
                if q_move_in(p, sz, d, entries):
                    kept, kept_size = kept + 1, kept_size + sz
                else:
                    failed += 1
                continue
            try:
                shutil.rmtree(p) if d else os.remove(p)
                freed += sz
            except OSError:
                failed += 1
        if keep and kept:
            q_save(entries)
        self.dl_items = []
        return {"freed": freed, "kept": kept, "kept_size": kept_size, "failed": failed}

    def quarantine_list(self):
        q_purge_expired()
        out = []
        for e in q_load():
            left = max(0, int(QUARANTINE_DAYS - (time.time() - float(e.get("time", 0))) / 86400) + 1)
            out.append({"id": e["id"], "name": str(e.get("name", "")), "size": e.get("size", 0),
                        "isdir": bool(e.get("isdir")), "days_left": left})
        return out

    def quarantine_restore(self, ids):
        ids = set(i for i in ids if isinstance(i, str))
        entries, keep, ok, bad = q_load(), [], 0, 0
        for e in entries:
            if e["id"] in ids:
                if q_restore(e):
                    ok += 1
                    continue
                bad += 1
            keep.append(e)
        q_save(keep)
        return {"restored": ok, "failed": bad}

    def quarantine_purge(self, ids):
        ids = set(i for i in ids if isinstance(i, str))
        entries, keep = q_load(), []
        for e in entries:
            if e["id"] in ids:
                shutil.rmtree(os.path.join(QROOT, e["id"]), ignore_errors=True)
            else:
                keep.append(e)
        q_save(keep)
        return True

    def get_apps(self):
        return {"winget": find_winget() is not None,
                "apps": [{"key": a[0], "group": a[1], "name": a[2], "note": a[4]} for a in APPS]}

    def install_app(self, key):
        if not isinstance(key, str):
            return {"state": "fail", "code": "bad-key"}
        state, code = winget_install(key)
        return {"state": state, "code": code}

    def set_theme(self, t):
        if t in ("light", "dark"):
            p = prefs_load()
            p["theme"] = t
            prefs_save(p)
        return True

    def find_update(self):
        self.found = find_update()
        return self.found[0] if self.found else None

    def install_update(self):
        if not self.found:
            return False
        ok = install_update(*self.found)
        if ok:
            import webview
            webview.windows[0].destroy()
        return ok


HTML = r"""<!doctype html>
<html dir="rtl" lang="ar"><head><meta charset="utf-8">
<style>
@property --p{syntax:'<number>';inherits:false;initial-value:0}
:root{--bg:#dfe4f4;--frame:#f3f5fc;--side:#fff;--card:#fff;--line:#e1e5f3;--tx:#171c33;--mut:#6a7396;--ac:#5b3df5;--stx:#4a5273;--onbg:linear-gradient(135deg,#5b3df5,#8b6bff);--ontx:#fff;--glow1:#2ee6c833;--glow2:#ff4d8d2e;--cy:#14b8a6;--pk:#f43f7e;--vi:#6d4cff;--ok:#15803d;--warn:#b45309;--bad:#be123c;--sh:0 12px 28px #2a2f5a26;--fsh:0 30px 70px #2a2f5a33;--sw:#c9cfe6}
:root[data-theme=dark]{--bg:#080b14;--frame:#0f1424;--side:#151b32;--card:#1a2140;--line:#29325a;--tx:#e9edff;--mut:#8e98c2;--ac:#7c5cff;--stx:#aeb8e0;--onbg:#fff;--ontx:#151b32;--glow1:#2ee6c85c;--glow2:#ff4d8d55;--cy:#2ee6c8;--pk:#ff4d8d;--vi:#8f6bff;--ok:#4ade80;--warn:#fbbf24;--bad:#fb7185;--sh:0 12px 28px #0009;--fsh:0 30px 80px #000c;--sw:#39426e}
@media(prefers-color-scheme:dark){:root:not([data-theme]){--bg:#080b14;--frame:#0f1424;--side:#151b32;--card:#1a2140;--line:#29325a;--tx:#e9edff;--mut:#8e98c2;--ac:#7c5cff;--stx:#aeb8e0;--onbg:#fff;--ontx:#151b32;--glow1:#2ee6c85c;--glow2:#ff4d8d55;--cy:#2ee6c8;--pk:#ff4d8d;--vi:#8f6bff;--ok:#4ade80;--warn:#fbbf24;--bad:#fb7185;--sh:0 12px 28px #0009;--fsh:0 30px 80px #000c;--sw:#39426e}}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;padding:18px;background:var(--bg);color:var(--tx);font-family:"Segoe UI",Tahoma,sans-serif;font-size:15px;overflow:hidden}
body::before{content:"";position:fixed;inset:0;pointer-events:none;background:radial-gradient(420px 380px at 100% 50%,var(--glow1),transparent 70%),radial-gradient(420px 380px at 0% 50%,var(--glow2),transparent 70%)}
.app{position:relative;display:flex;height:calc(100vh - 36px);border-radius:28px;overflow:hidden;background:var(--frame);border:1px solid var(--line);box-shadow:var(--fsh)}
aside{width:224px;flex:none;background:var(--side);display:flex;flex-direction:column;padding:20px 14px;gap:6px;border-inline-end:1px solid var(--line)}
.logo{display:flex;flex-direction:column;align-items:center;gap:8px;padding:2px 8px 18px;color:var(--tx);font-size:18px;font-weight:700;text-align:center}
.logo img{display:block;filter:drop-shadow(0 6px 14px #0003)}
.tab{display:flex;align-items:center;gap:10px;width:100%;text-align:right;background:transparent;color:var(--stx);padding:12px 14px;border-radius:14px;font-size:14.5px}
.tab svg{width:20px;height:20px;flex:none;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
.tab:hover{background:var(--line);transform:translateX(-3px)}
.tab.on{background:var(--onbg);color:var(--ontx);font-weight:600;box-shadow:var(--sh)}
.sfoot{margin-top:auto;display:flex;flex-direction:column;gap:12px}
.swrow{display:flex;align-items:center;justify-content:space-between;padding:0 8px;color:var(--stx);font-size:14px}
.sw{position:relative;width:48px;height:27px;padding:0;border-radius:20px;background:var(--sw)}
.sw i{position:absolute;top:3px;inset-inline-start:3px;width:21px;height:21px;border-radius:50%;background:#fff;box-shadow:0 2px 6px #0004;transition:inset-inline-start .2s}
.sw.on{background:linear-gradient(90deg,#6d4cff,#a07bff)}
.sw.on i{inset-inline-start:calc(100% - 24px)}
.user{display:flex;align-items:center;gap:10px;padding:10px;border-radius:14px;background:var(--frame);border:1px solid var(--line)}
.av{width:36px;height:36px;border-radius:50%;flex:none;display:grid;place-items:center;color:#fff;background:linear-gradient(135deg,var(--cy),var(--vi) 60%,var(--pk))}
.av svg{width:18px;height:18px;fill:none;stroke:#fff;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.user b{display:block;font-size:13px}.user small{color:var(--mut)}
main{flex:1;min-width:0;overflow:auto;padding:24px 28px}
main::-webkit-scrollbar{width:8px}main::-webkit-scrollbar-thumb{background:var(--line);border-radius:8px}
.pane{display:none}
.pane.on{display:block;animation:in .22s ease}
@keyframes in{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
.head{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;margin-bottom:16px}
.head h1{margin:0 0 4px;font-size:23px}
.lbl{color:var(--mut);font-size:13px}
button{font:inherit;border:0;border-radius:12px;padding:10px 18px;cursor:pointer;transition:transform .15s,background .15s,filter .15s,border-color .15s,box-shadow .15s}
button:not(:disabled):hover{transform:translateY(-1px)}
button:focus-visible,input:focus-visible{outline:2px solid var(--vi);outline-offset:2px}
.pri{background:linear-gradient(135deg,#5b3df5,#8b6bff);color:#fff}
.pri:not(:disabled):hover{box-shadow:0 8px 20px #6d4cff66;filter:brightness(1.08)}
.pri:disabled{opacity:.4;cursor:default}
.ghost{background:transparent;color:var(--tx);border:1px solid var(--line)}
.ghost:not(:disabled):hover{border-color:var(--vi)}
.ghost:disabled{opacity:.4;cursor:default}
.hero{position:relative;overflow:hidden;border-radius:24px;padding:24px 28px;display:flex;justify-content:space-between;align-items:center;gap:18px;margin-bottom:14px;color:#fff;background:linear-gradient(120deg,#2f239e,#5b3df5 55%,#b44cf0);box-shadow:0 18px 40px #5b3df54d}
.orb{position:absolute;border-radius:50%;pointer-events:none}
.o1{width:150px;height:150px;inset-inline-start:30%;top:-48px;background:radial-gradient(circle at 30% 30%,#d8cbff,#7c5cff 55%,#2a1f7a);opacity:.5}
.o2{width:62px;height:62px;inset-inline-start:46%;bottom:-14px;background:radial-gradient(circle at 30% 30%,#8cecff,#3b82f6 60%,#1b2a7a);opacity:.7}
.o3{width:34px;height:34px;inset-inline-start:22%;bottom:18px;background:radial-gradient(circle at 30% 30%,#ffc2dc,#ff4d8d 65%,#7a1f4d);opacity:.65}
.hero>*{position:relative}
.hero .lbl{color:#ffffffcc}
.big{font-size:40px;font-weight:700;line-height:1.2;margin:2px 0 14px}
.hero .ghost{color:#fff;border-color:#ffffff66}.hero .ghost:not(:disabled):hover{border-color:#fff}
.hero .pri{background:#fff;color:#2f239e}
.ringw{position:relative;width:128px;height:128px;flex:none}
.ring{position:absolute;inset:0;border-radius:50%;--p:0;background:conic-gradient(#fff calc(var(--p)*1%),#ffffff33 0);-webkit-mask:radial-gradient(farthest-side,#0000 calc(100% - 12px),#000 calc(100% - 11px));mask:radial-gradient(farthest-side,#0000 calc(100% - 12px),#000 calc(100% - 11px));transition:--p .5s ease}
.ringt{position:absolute;inset:0;display:grid;place-content:center;text-align:center}
.ringt b{font-size:24px}.ringt small{font-size:11px;opacity:.8}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:16px}
.stat{border-radius:18px;padding:14px 16px;color:#fff;transition:transform .15s,box-shadow .15s}
.stat:hover{transform:translateY(-3px);box-shadow:var(--sh)}
.stat .lbl{color:#ffffffd0}.stat b{display:block;font-size:20px;margin:2px 0}
.st-safe{background:linear-gradient(135deg,#0f9d8f,#3b82f6)}
.st-caution{background:linear-gradient(135deg,#6d4cff,#8f8bff)}
.st-risky{background:linear-gradient(135deg,#ff4d8d,#ff8a4c)}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:13px 16px;display:flex;align-items:center;gap:12px;margin-bottom:8px;transition:transform .15s,box-shadow .15s,border-color .15s}
.card.safe{border-inline-start:4px solid var(--cy)}.card.caution{border-inline-start:4px solid var(--vi)}.card.risky{border-inline-start:4px solid var(--pk)}
.card:hover{transform:translateY(-2px);box-shadow:var(--sh);border-color:var(--vi)}
.card input{width:18px;height:18px;accent-color:var(--vi);flex:none}
.inf{flex:1;min-width:0}.inf b{display:block}.inf small{color:var(--mut)}
.badge{font-size:12px;padding:3px 11px;border-radius:20px;white-space:nowrap}
.badge:empty{display:none}
.badge.safe{background:#14b8a626;color:var(--ok)}.badge.caution{background:#f59e0b24;color:var(--warn)}.badge.risky{background:#f43f5e24;color:var(--bad)}.badge.mut{background:var(--line);color:var(--mut)}
.pulse{animation:pl 1.2s ease-in-out infinite}
@keyframes pl{50%{opacity:.45}}
.sz{min-width:92px;text-align:left;font-weight:600}.num{direction:ltr;unicode-bidi:isolate;display:inline-block}
.sp{width:18px}
#upd{display:none;background:linear-gradient(120deg,#5b3df5,#b44cf0);color:#fff;border-radius:16px;padding:11px 16px;margin-bottom:14px;justify-content:space-between;align-items:center}
#upd button{background:#fff;color:#2f239e}
#ov{display:none;position:fixed;inset:0;background:#05081099;backdrop-filter:blur(4px);align-items:center;justify-content:center;padding:20px;z-index:9}
.modal{background:var(--card);border:1px solid var(--line);border-radius:22px;padding:22px;max-width:520px;width:100%;max-height:85vh;overflow:auto;animation:pop .15s ease}
@keyframes pop{from{opacity:0;transform:scale(.97)}}
.modal h3{margin-top:0}.modal ul{list-style:none;padding:0;margin:0 0 12px}
.modal li{display:flex;justify-content:space-between;gap:10px;padding:7px 0;border-bottom:1px solid var(--line)}
.warn{background:#f43f5e1f;color:var(--bad);border-radius:12px;padding:10px 12px;margin-bottom:10px;font-size:14px}
.chk{display:flex;gap:8px;align-items:center;margin-bottom:12px}
.row{display:flex;gap:8px;justify-content:flex-start}
.bar2{display:flex;gap:8px;align-items:center;position:sticky;top:-24px;z-index:2;background:var(--frame);padding:12px 0 10px}
.bar2 .lbl{flex:1}
.gh{display:flex;align-items:center;gap:8px;margin:18px 0 8px;font-size:15px;font-weight:600}
.gh::before{content:"";width:8px;height:8px;border-radius:50%;background:linear-gradient(135deg,var(--cy),var(--pk))}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:8px}
.grid .card{margin:0;cursor:pointer}
.lock .card{pointer-events:none}
.track{height:10px;background:var(--line);border-radius:8px;overflow:hidden;margin:6px 0}
.fill{height:100%;width:0;background:linear-gradient(90deg,var(--cy),var(--vi),var(--pk));border-radius:8px;transition:width .3s}
#prog{margin:4px 0 6px}
@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style></head><body>
<div class="app">
<aside>
  <div class="logo"><img src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAMAAAADACAYAAABS3GwHAADSWElEQVR42uz9d7idR3nvD39m5imr7bV7UdvqzZZlufcCGJti000nQBKSkIRwSHJCQopNCAmEAyeFJISEhEBCbwEDxuBecZUs2+pd2tLudZWnzMz7x8xaEvmd817v73ovTGz0cAnLsnZb675n7vItcPo5/Zx+Tj+nn9PP6ef0c/o5/Zx+Tj+nn9PP6ef0c/o5/Zx+Tj+nn9PP6ef0c/o5/Zx+Tj+nn+fXI06/BP+tX2t7+mU7nQDPmcdaK/7La2sBpJTW//efeNHtf/n9T7wxQmCMEad+nlP/2+nkOJ0AP/PXrhXQrSA/9d//D0/5/+Xrnfpf/88vfDI5TifE6QT42QT9/yHgxfDwcFfXkiXh9ddeu3TV6tUrRRAoBfT09a3t6ek9UwmlzH95xSVgoP3/WImQiFqtPnZ85OhjOssWsiyzBw8c2H/LbbcdlY2G3bp16zzQ/D8lxOlkOJ0AP7XSRghxamDFL3n1S/qvuPzFq1cOr1zX2dO5pK934Jw4ijviUryoXCwvEYHEIkSoVLFULCmLpVUEWSy0EkgIrLAIBMJYpFJkaUqz2WhYTW4xNOr1Y/V6YwRj9Pzs7L7xiYlnJicmjt/zwL1b//0znzl86m3xf/l+Tz+nE+D/feD/5EnfU/3jD/7umWdtOfO83t7+jX39fZvLpcryYrk0oJSKioWikFJijMYai5DSn8gWa7QVUoAw/mWXINxR3e4NrMBag7U+GYQSGJc2QkqEUghryZKEZproPMvnZ+Zmdo+Pjz8yOT62c/vWpx/7m098dOfs7Ow0gJASo7U4nQinE+D/r8B/85vfuerqa686f+PGzdcPLOq/vFzpWFIoFqJABlhjsFiCIERJayMprQShgYaGepaQ5pqFRItGM6WWJDTTnCTP0HkOQqKkIlSSOAopFQoU45hyQVEOlC1FEZGCwNU1NjVa5LnFGCOEFUglyY0maTbShfmFoxOTU08cOXTwnh/e8u1b//mf/3n3qSXS6UQ4nQD/PwV/K1De9ra3rXvFa258xdqNG17d1dV1ZrlS7RRKYXROGIY2jBQAjcQwU5sXJ6bmOXBklENjExwbm2ZkbILJ+Rr1TNPILYmxpLkm16CNxhiNlAolBUEgkTIgCgPiUFEKJd3lAoO93fR3d7Kop8Ki3m6GF/XTXy3RW63aUgDkhjTX6DwTQRCClNRrC8nU+PgTO57Z8fVbvvvtb3/+n/95j/vR7OkkOJ0A/9fQF0K4U/+K665Y9Ou/+jtv2nDGma/v7es9p1yuRMYYVBjYMFTUEsP+YyPimX3HeHzHPnYeHGFkdJzpekpdK3QQQBggoxgVhEjhjnCpXEkkkK4XsCCVfxuEQLRKIiGwVoI1GGMQeQ5WExhDQRq6SgHrhpewZnE/m1YtY9OKIZb0dttYQJJkGKNFFATUagvpxOTEE8888/RX/v7jH//ivffee1wI0eoRTifC6QT4f5z66sMf/+iVV1/14t9cNrz8ZeWOSsEiiAqhbeawY/9BcftD27jrkafZc/gY0/UMLWNUoUBcKBCGISIMEVKBBCuka3BbjS4CKRRWAFIgcUGPEO7vu8IdIaTPCYtEIqR/swxYnWO0RqcpJk0JMQxUYjasWMSlm9Zw6ZmrWLWk3wYasqQpAhUwX1toHjl08Ht33nnbJ//wd95/D6BP3wanE+Angn/x4g29f/UPH/ulzWef/cu9ff1rw0ARx6E9Mdfk+/c/Lr71owd4YscBZpoJQbFMWCwRhiFWCJACISW4xtVNeYzFoDEWDMY1txgE1ge+9cHvhqFCSJAKKwWIEIIAKQRSKYQUKBmgpEBY6z6HECjhbpIsyUnSBqRN+kox565bwg2Xb+GKzettZxSQp7kw2jAxMb5n67Yn/vl//M9f/czIzpHJ00nwc54ArQD4ld/4jbPe+Ma3/vq6jRvf2tnZUSkE0o7VE/79lrvEv3/nPnYeHsXGMaWOCkGgsNaABQ1I6YIw1xkmayB1TkFBKZZ0liO6O8v0dRbp6yjRWS5QLgaU4pg4VATKN6hakGpLM9PUkpypZsrUQsLUfIPp+QZzjYx6YshsgAlCgqhIGBUQUgAGYV3yCCFIs4xms0ZscjYt7+eNL7yAl56/yXbFISYzYnZ+bmHXMzv//evf+Mrf/93//t/bf96TQPy8B/+HP/zRK1726lf9+eLFSy6rVssCgf36jx4QH/+377P9yDhBuUpcLAI5COtwCcadwggQpkmFOiv7O1i3tJu1S3pYMVBmcU+Jvq6YrlJMMVSEgSQM3MTHGoWUprUNQKAwAiwCayGzGq0t9TRnrpExMZdxYjbhyGSN3cfn2XNilsNTCQ1VRcsAKSXGAsrXUVJilSRNmoi0yZbhfn71pZfy4i3rrdBG1OtNe+zYsfu//Y1vfuCmP3z/vT/PSfDzmADCWosQwn7oQ39x5ate/7qPLls2fHFnMbL7Jmf4k7/5D/GNu7chS51EpTJaa4RwJ3Wz2QBhKBYKWJuT1OtsWTvAn7ztctZGkwxUFJEyKGnB5lidg9FgwJxS7mAVVhj/BgjXM/gm2L0h1tX9QoJQCBmCVGgRkBIwmxd4eq6T3/7UrUynAhkECClJcoNUIVEhxEiLlQKJJGkmRFmDV523hve95hq7pFqm0UzF0cNHHvraV774/g/+8R/f45OAn7fmOPg5PPkRQtibPvQXV776Rhf81WJkv3j7/fzx335FHJmzFHsGMUajsxQlFY16nSxbYMNZa5AqYs+OA5TimCAI2bn3MMfqhtVlyGePATlZ0kRnCWnWJE8zMNaVTQpX1/ueQSBRSiKFREiXAFJINwUSEhUEyCBGhSFSBoggwoiQau8Gjh9YYHRqlrijGyykzYQVg53kOuPg+BRxuUpYjMlzQxhGyEKBrzy6n6f2/Ye46e0vsxevWWoXL1168ate//qPAu8XQpyaBD83j/p5LHtu+tCHrnzV6278y+XLl19cKUb2E//xXfGev/gXUQ+rxB0dZHmOkpIsTVlozLB27SLe8s5X84a3v5yVa1Zwz+33gpUoIZmfr3N0rs75V1/O1J6HqR/Zxdz4UeanR0nmpzDNGjZrgG4iTYo0GdLmSDSByVE2Q9kcYVKkTkEniLwJWQPdmCOvz5LXZ8hq0yQLM0zpiGOVTXzgn77PVN2iVEiWazpDzSd+7+28+kXnMdhXZXx8nBMTc8ggIlACow1xqcRUYrjj4SdFb3dZnLVisS2VKssWL1l2xtDQwO4XXHXVIWut+OAHP3g6AZ6vwf/BD3/0ile89jV/uWx4+OK4FNs/+tvPiz/7p29R7l+CCAK00QRKsjA3S3dfgV941+t42y+/jmWrF7HQWGDp4l6yesITjz5NIS4ShjGHjxxjyRkbWdTfgR7ZSTWUBEqhwoAgDFBKogLlTn9h3Q0g/PTz1H/iSi0hFSoIUIFEqYBAKaSABR2QrLqSf3lglFvv3UG12o0RgsbCDO9+07W8YPMatE3YsnqYKy/cRGcl5sCBQ8zWmhSKRbTOCIKQXEXc/tBW4jgS569fbsvF8tLFw0s3Ll60aPfVV175c5UEPxcJcNNNN8kXvOAF9qYPfejKV7z2NX+5ZOnwxcVy0d701/8mPv7Zb9M5sBijjSvPbU69PsOV11zAu3/3l1i7aSWNZp3UaJpZjtSG9etW89D9j7OwkBEGEUYLjo6O88JXvRJbG6NYnyBSbn7vRv6u7EFaJGClK3UkFiGNG4e6lYF7pPC1v0IIhZCSVIbMDZ7FrngTH/3H76LCKioKSZIGZ67s571vfwXTWZ1EWLTNiYTlnLXDXHLeemamptl74BhxVASjscYg4iL3PvYUUSES521cYUvF4rLFS5duHBoc3PPzdBOon4eT/wUveIH9/T/+44te+4a3fHzRkmUXd3QU7V999j/Fh//hi3QOLCbPU5CQpylCprz119/Cy970clKZ0chSasZwcGKa3cfHWEiarF6+jECF/Pj+xymUKgRhzIljo1QGB9hy8UXUj+ygYusujv1pf7LhdaA3KaTrN/1V4HZm0i/LBEK5t0aqECsDpqJ+amuv5e++/jiPbTtItacHZIDNavz2u17FssV9PH7iOEcaDRrGoEIFVlMtRFx+/pnEgeKJp3a7hhuLNZagWOb+x59ioK9LbF611Jai4rIlw8t+rpJAPd+DX0ppL7nkkoF3v/d9N69ctealHR1F+927HhHv+9O/o9TVjzY5IpBkzTpxEX7pD36dDZecxdTCDKm1HJmcZdfIKBPNJjaKWEhTlBScc+ZGntn2NCdOTBGGMUqG7Ny1l81XX0FnbxVzfB8VqR3k2U+RJMYvfd2fSb8Bln4bbH1iCKGwQiCEwArFbNDJzIoreHAk4lOfuYW42EVYiKnVFnjBxet5+w1XsGd2kpE0IQ0V01nCRKNBgrt58rzJWRtX09ffy6OPbQMkSkp3E0QFfvzEds5cPSyWD/TaSqWybPHS4Y2LFw39XJRD8vk87vSIzsLv/fFN71m+euUroji0+49N8T8//A9Q6sQKiZCKLEmICoJ3fODX6d4wzMj8HBONjEf2HGDHiVFqFqwKyQ3kMmDP2Bg1lXHj216BIMUagwoC5qeb/PM/fYP5pRcytfIyJnQBbQGbI02OMAahNcIa93tjUFYjrPszhXZjU5sjrUGLgImgi0N9Z3OovJ5Pff4HpJnbDGdpSk8R3vnaFzGnMw7Pz5ErgcZiVMiCCNgzt8DjE5McyzXHF2a58Py1vPddryPQDXSeInAJtyBiPvL5bzPaSAQoO7xixcXXXf+KD7zmNa9ZLoSw/4XqefoGeK6c/jfffDP/++/+7vorrn7hH1Y7unplGPC7H/xr8eCOg5Q7qhirwRhy2+DNv/9ues9cxUzSYHRmjl2HR2hai1Ah1noiixVIKcm0JstSztq4ganRSXY+tZs4LhKFMUf2HaFmJRte+GImc8NCZmlEZeaDMvNRJ3NxFzNhlZm4m5mom+momyn/z5mgymzYwWzQwVTYzZF4iP09m1lYcjH/+u+388A926hUqgiguTDNL73+hbzwvI1smxxlUmdYFaAtaGvRxqKloGZhtFbHKoUwmhVLB1k8OMTDjz6JCCMsEIYxIxMz5EnCVeevxyQJHR3V4d7+wbkvf+HfH7z55pvNBz/4wedlEjwv9wA33XSTlFKal7z61UuvuOrqX+/u7l5SjEP7tR/cL775w4eoDiwiyzI37anNcsOvv4VF521gulHj6IlJjk9MEhUKjlBiDUJItLGIwCKsIVAhx+dqjMxPc8ObXs62J55hbrpJpCI6Sh18/0u3IqTgmtdcx/jyCwmyeUIyV9sHEqQEFFZIrDhJjhdWI63FWk1OiC11o1QXP/jy3Xz/W3fTWekCa2k2amxcPcBrXnIp+2qzjDRq5GGE9SQzhz+yaGuxQqEV7JudoVEqY4HzzlvLq0av5AvfupOOzl50rilXu/nmvY9z3YVniotWLbaFuBCvWr36pa985Ss/L6U8eNNNN8kPfvCD9vQN8Bwofe69915rrZUf/LM/e8dZm7e8Iy7EUS1J+Y0/+oQYbxiUCpFCUF+YY/MLzueKt76S2Szh2PExRo6PUShEgEBagQCUhytbLEhXo+fGkmZNViwdYvHAEA/f9yhKKDCGKIzZ8cR2jh8bp1jtx0RdJLLKgikzlxWZbcbMNkMWmhHzjYCFRsBcQzGfxixkJWq6wnwj5NC+Sb75b7dw3w/up6Pc6VCkwlJQGR9431voHujkidER6ioAJNoaDGCsxVjrksvgYRuK2aSJERBK2LhmBQcPHuXYiSmiOMIKSTO3NOdneMklZ2MzLaI46urs7jr21S996ZF7773XPB9LoefVDWCtFUopa4yR//CZz7zhwosu/a1isVjuCAP75e/cKbbtHaEyMEiWZgitKQ90csUbX8ECGaPjkxw+fJxiqYTVLtCssggrsVaAtVgrSLMcbQzluEg9E0zOLLDlnPVccOEWfnzfVkrFIsZoysVOtt//JE//+CmK5SIqckhPY0BjwGiUDRwYDg9bUBLpm2ObG2pzDaSVVEudGKNRUlCv1bj+2vPZsHKIAwsNTLkDqXOaSYq1BiGlo1BaEBYsLhGENQRByLH5GqFUrOqMef2rX8TeT/wH2n9csbPKfTuP8vCug+LqtStsICodF5x/4Xs//ZnPjv/KL73jy1JKo59nFMvnTUZ7ogeA+qd/+Zc3vODa6/50YHBotTbaChAve8cHeGT/BHGliATma3O84K2vYMtrXsjEzDQ7nt6LkCFBFCIC5aYk0sFxlFQYC0pJSoWYIormsTFGdx9k+uAI8xNTNBY0WWLcDSH9y6qU4zGaHCuFH3M6FoyDRUs/hnD/zbSWYf6XCgQYV88L6adGwlIIDEpZ+oYXs/yMNQyfuYruxf2YwNJo1LDWEgiFccc/xhqsASMkubWEWnNGTzfLurr58tfv5Jbbn6CjqxcsJPU6N5y1mL/6ldeSNlIbRaEYPTG674e33fYn7/rFt3+Z5xmXQD0/Yt+xnN7zq+85448+/KfvvviSy36nv39wVZqktiOOxF2PPsVff+4W4o4uLGCMoWNRD5e/+XryULBv70FqtSZhFDlosXCoTDepFxhjKcUFKlYy++Qetn3zh2z93t0c2baH6eNTJAsZaBecDp7s/unvpfb21wrbPuFb+y6E8AR4fJBL99+txRrjWDDCoU8dYV6Q5pZ6wzB6bIxd23by5INbObLrAIEVDCxahAgVjbTpUaLudnEMfMdF0FaQ5zndhZAlg4M8tm03zdztKVQQMDo+wZVnr2OgWhJZom1HtaNn8eLF573gRS8sjUxOHnjn294293zBDD3XE0BYa7n55pv5q09+8mU3vuUtH9+44Yw3lTs6erDWFsJIxIHkbz//bR586hBRuYQQikbW5KxrLmLFhZsYHR1nZGScMI7b21cXCq6WBklnVKS+4wBPfOV7PHPnj6mNzREFMVFUIIwiAhW64BfGn94WP+p3pZS/aFuUx3Zi2Nbv8DIpBqzAott7gtbfF8b9HYEAIwiUIIoiCnEJoWFyZIJnHtnOoZ37GVq0iJ5F/dSSuttBeP6lsAaJS7BGlhIpwaL+Pk6MT7Nn3zHiOEYowezCAkurEVesX4EFoY22pVK5e3Bo8LKLLrxkw4o1q8Zuv+22Az4JxOkE+BnW/EII+4m/+buXv+zl139kyfDweR3lgiwFyja1EaMz8zx18AR/+7lvM5dYhHJrD1FUXPyqFyM7CuzffxCt3T5AqcAFjHIYhlApOkzIzv/8AU9+50c0Z5oUCxWCMMRg2uObVqC7xe5J6RPrw0MqV/I4tphXBxIt6L6j5zr0s/QNq2kXqO63FuVvCmuNT0+XpsbX71EYUYwLzIxO8+RDTxKomNXrV5PmKZ6/0040C2RAnml6yyXiMOaxx3ciVYgxrpGen1lgy8YVaKMpFIuiFEobRYWgt7dv/ZIlS85Zvnrt4du+/909z/VFWfBcPvmFEPYTf/d3L7/uJS/7yLKlyzYVQ2ke3XNYfOGbPxD3PraLI6MzLDRSrAqRYYDVkJmU/jXDlBf3cXxsgoVaigoijLFgXHBZNIUoptTIePhL32Jiz2GK5U4EgtzoNoDNOHgPRrSC8mSzLACpXKXvLgQBVjvtH58sSIE1GuE1gozx3AM/yvRkSoQFLU9eHxaXYMaa9g2iMRgriEsdIALu/PpdTE1Mc+2brmUhq6PRvqAT7uOlYCpNGJ2fY+nwIENDXYycqBGFIWEQsWuiwev/9F8pSsOSzgpXbl4lXnPNJXZ5d5WlS5addcP1139Eufrzu89lLoF6Lp32N998swDkXXfdhRDCfuxv/ubl1173so8sXz68Kdfa/uknPyd/64N/L+56fB+jNUOTAILINZjWOoizSVl92dl0b1jBkSMjZKl1fNzWTS4EgQqoJJYff+E/mT40Rqnc0V6GKVolhecBt3E+0gU2LfyPH51aL2zl88PrZLXRn67Iav+M7YWbg0FwUljLB/1JxKj/GOlFs5zsBEJIhJSUCmUO7j1AbW6BM7acQZLVscJirGyT9DNrsTpnoLuT8dEZ9u8fIYxjjDYYqchlQJ2A4/MpD+08zA9//ISIC6E4Y/VSWy6XBweGBjevWbP68KUXX7zbj0jlXXfdxc033yyeK4sz8VwIfN/p/sTp8rFP/M3Lr33lyz+ybMmyTXme21/67Q+J7/zoMcqLh0GFGOOCxmp/OnuySRoYrnr3myitXsTB3ftBu6mNlAKUQEpFX1hh2zduZfSZvZSKFbROXRNrpYMx+Gi2PqjdIMfDOVvfpqc44pddrumV7Z7AkeI9UM5/IiEMxmFEUeAmR9J9jLs0VLs3EL7Esi0SjVFY4W4njzlFhTGz9RkuednFXPqKK5iuTSOVQPsxrzGS0Go2LV3M3qeO8R+f/z6FQoU81w7BerJ7IZASkyck0xP80ssu43fe9FJrm7k4PnL0qR9893u//9u/9Rvf/a9TuVP0Sv/b3gz/XUsgYa1FSmlbL96aLVv6f/HNb17TyPN8qL9/1dUvuuYDfQOLNkVxaH/vT/9WfOcHP6Zj6QrSLEfkGqkUSZLgxKKUm/7kmrhaIuruZHJ6jiy1hMolijERAN2FAkcffJzRp/dQKpUxeepOYDdMd/ghP76UfuTpTmXpyhWXtSCFa6Jtq8GVbkwrTtEDxWCVk0fBamy7bzBOUsWHoJSBS1CrEJ726/SFHKrTWIOVLmjd13DNuNEZnaUqD932YwaHF7PkrGHmG/Pu7/l9hDY5E7UFOvqqjkqpHWDPWos2jtYZhiF5apDWUuzu5dPfvofOjrL41euvtENDize95Prr/yIoFiuzk1PH+hf1L3nqkUe2ffKTn9wnhMj+vx1ipxPg/xD0QCvwAXjrW39pzSted8PLV65Zc01XZ/cmY9FREBbLlfLiSjG2t9zxsPiXr9xKZWgJSdJESEWe1AmyWc5ctYTjczmzDYsMFbnOKXeUMVKxMDWDzS1aGKQBlKGgIprHJth93yPEUQGjM/yQ0gVym8wiIVCgJEIG7YB2/ay/eYxBGT/KNI4DYH0PwKnwB+Nvj1Y1L4yvj7yIllAgFFYq10z78sdanCyj9KoQ1oAxftrT2otorNYUCbnzmz/kxuG3QCzQxrfDArSBmUaDnlI3cSGiPpuhlMJqTXeo6QibHJ4YRxW7QQXY1FDq6uYfvnYbF25cI84eHrQD/YvOuuFl13/CGJNEhaj78ksv3/uaN7zpwQP79t/3za99+yEhxGH/vqK1/m+VDMF/s9O+9WeVD3/84+edvfGsq1auXn1t/0Df+eVKR+yCELQ2NJpNm+Za/OO/fQ0TFtBoBKDrcwxXa3zwV1/IOedu5s2/9zkmpqCgKmiTEhUimmlKUm9A7icz7romDgS7H3gM09SEkeKUWQ4I6U98hVUBMnDaPUgFUiID105ZY5DWYHNPiM8NgtwlhT11WuRSwOW8b1B9L+LuBIVQwiWYClzwBwFCKd83uO/Z6gxhDGiNELkLetMqwbQDu6mAuZEZtt3zBOe+7EJm6vNIX8IZBPPNjM6iICyEmIk6gYCkUWN5t+Qv3nYhD2/fzSe/t4+pvIcgiBBSMp8J/v27d7HlPW8WNBLb19uzOAgUOs/p7e48f9nipeev33DGL5x7wYWPvekX3viVL37uS9+/5ZavHm4F/n+XW+FnmgAtvL4P/Ph973v/ugsuveiClcuHr1y0eOnVlY7KcLFYEH4rajXQbKboXFMoxGLXgREefXI3cbmK0RarNVVm+OR7X8J1WyrsnzpIMj+LoAp5BllCGEgaCw2yWkokJDrLEUJSKETURsaZ3H/UsbyMOWnNIk5laylkEEEQIMLIyR+GMdaPUKU1kDpSPHmGEBohLWRuBu8i3rpbwRpHarHGb4898kg4pQekgjBABCEqLCDCGCsVwvplmm5CrhCZK9OwrgyzGN80u0frnGJUZOcjT3HGJWch2rcA5FhqaUpD5wSBwmSpG/HqjOZ8jd7mPt55TkB3YQMf+PIukmAAazRxucT9T+7h0Ng0K7s6RJ5ntklOGMeEuUYF0NVV7ezp6Xrh0NDQxZs2bXrDu3/jl+/cs2vPA1/+8n9sF0KMnTLK/plNkIKf5anvsz/+0Ec+ct7Fl15+4/Jly1/c2VldWSmXS546RRRIO5NrnthzUHzrzgd57RUXctHaVUhh2bv/EHONjGKXxBpLY2GCV16zkivPiJg+vpe5ZClJ7qct1oBxJUzebKDTFC0CZKgw0hBQYGzvQXSiCeLAS5Q7DSArBErgJywKoaQLyrhMYgWZVRQKFYzVJPU6QVhCBTE2aSBI3LhSaQT+VmhZAwiBQPt6H58Iyp/6yt0yYQFVLJNq11NEUYRF0GykxKpEIDOMtQhjMdKAyX2v4kaywhoQkiAImJ+Y49Cuwyw9bzULCwugJLk16CxnIUkQ0qLTJgRFrDU0GhkL8/PMLYxy9aoNXLWhi2/tWKBYqaKUZGpunt0HjrDu4s0IKcX2qVHuP3GcS4ZXsr5apaqkNWlOR0dHqauz+oKlS5ZctXnzltErr7r6saeeeeqrf/tX/3CbEOLEqTud530CtH5QIQQ33Hjjynf98q/98voz1r+ht7t3dVwsuBM5UFZJGKk1xQ8efFp889EdbD8+Sz4zxY0vuhSEJVaS0fFJrIiRKsBgUCLlok0D5LVJjh0d5XAS00jdbN0aDdYgwgiT5thcY5RA5BarAmymmR0Z9xBoaJtYSOEbXQdWc3W/JAhiGmnKii3r2HTpWXT09SKsYXZkgkfvf4qxQ6MUip1oMYvEySRam4NpLc78dtYVI5441pokKVABREVU3EEztyxaOcSWizfQNdhDrg3HDp5g231P0ZhKKAQxJs/c55ayvcV2jbts9wuBFex/ei+Dm1fT9PsHYw25MaRaE0Wh+x6tQVpXajbrDbKgSUVPc9ayEt94puFuJqEwUjA9M0Pov14WhNw+PcndzQYr4gIX9/SKiwaHWBIFljQniiJZGiwv6h8cuH5o6ZIrz9y0+b6tTz72xV9629u+LYSY+1kkQfCzCP7Ozs7uv/3UP71m0+az3zq0ZMmlxWIhyvPMXeJxwPbRCfHdB7dx69Y97JmpIUoVwo4uesKAjmIJjHsPas3UzflVhLUZhYKityPixLFRmguazFqMdQwq66mJUinyJEMYixWWHEMgIJ2epTE1RdCWG3QlidPpsQjldHlEGBLGRZrGcP61F3L5jS+iJi3NPCMOAvpW97P+vHV8/wt3svOxPcRxB9pohIkg136i5JthLFYYzwN2CwF3wyhEEKOiMg1j2HL5Jq6+4VJ0BIlOQSh6Vw2y9qzV3P7lH3Fi1yEnzGtcgiFyf8VorBFY4WRRpJBMHRqhOVcniwTCaCSgrSHJtNuAW+vgR0i0tpjckOomebNGf7WPIAAtJYEKIAjJtfscympKQUBU7mC2ELE1y3hq5AjfGx3hir5BcfngIoaLJWya2zzPqHRUqhvP2PiyvqGBi394z32Xf+fb3/p7IcSTz3YSBM928L///e8/56U3vPI9a9ZueE21s7Mz0SkqUjaNlLh79yHxjTsf4o4n9zGRS6JqJ2G1GykkeZpiTU6gpAebQZr7Ole62jwIY4S11BZSEAFJpv0sXrVLTJ0bskYrUDRWC5Q0NCYnSet1QhWBsW3kpjsnpVdvCwiCmMwqlm9Zy0WveRFTecNtkX0gzWWaQhjykje+kInRKaaPTqBUjJWpK21EjrZOLNetk900BqWQwuH6CVyfkWgYXr+Mq15xObNmAZv4Ta7IyUyCqIZc8+aX8O2//wr1sSmUijCmibESyE9OlqzBWIOSkM4sMDs5i1zSSZ5lbnBgITfGSfca4SYN1pVcWmussCSNhDAIUIHyJZtb1uXt9bQgEBK0J+IoRVYqcdAaDoyO8v3jo1zY080Vi5aI9R0VitrYNEmoVqs955xz7q/29HRvqJTLvy+EeOjZTILg2Qz+D3zgpotf/5Y3fmTZsuVXSRUgQ2ltXOD723aKL9x2Hw/sOEyNkGJHB8Vy5KACWYYR4U+s7FovjcH4f5EeV+N6AeN6OGzm6myDdD+oVI4xleeY3J/CFkSoMHOz2CzDytCNE6XFtioSP60RUiBUAGHImZedQxrkBKl1N0zr+xKSZpYTFGPOuuRMbv/i7URB6IJLSK/+jJ+L0pZCtwgMoKSTQ5QywAjLpovOJA0ybApKKIxfc4VSUNcZqqPEGZeewwPfuJ2yh227HsO4/gUn09hqnJNmwvzsHNUlXWjj0KnaQqIN2gd/CzskAaMtRgqyNMfkudt4W4eoNRp07t4MY1u7CTBWtJlpVkhsqcDRTHNoapIfjY9zbmcnL1o2LM6pdlHOcyuEYNXK1VfdeOMb/5KM33s2kyB4VoP/rW/8y+XLV1xRKhVsAtz+5C7xL7fexz07DpGomGK1l5JwdanNcjfyk748aP25sScRktI3j8alA0CaZjRtjsk9dEAqJz6L8TeBgzejHbTY4mb1eaPhegSMayA9otNRqqyTJVeuLIorJapDXc60QuKZWALVoiQiqeuUzsEepLJun+BhDt79C9vmAZwyZvLLMSEsxhiiUkRHXweNLHeLKWEcPMI35pEQWJvTt6SfMAqxWdY6jB2swuT+ZxLt/QQ6I6nVsb68sY7ERqYN2mW5T1QQ0pJrQ2osItPo3J7sL4S7IXLT0jh1gD2LZ6O1kEytNXggCMIis9pwe22BR57azjnVDl62bLk4p6uLktZ2+aqVV9z41jf+JTx7SRA8G8F/05/92cWvfdXr/nL5ilVXhMXQ3rfrgPirr9zKvbuPooudFLoHXaOotQeHncTHYwxWWCwSrVOSPCc3bsgX0CohHIDAAkmiyZUgSzVapG56Y7zOjlRu7Ig7waQV7WAxuQt46bE7toW5NApjrCOn4IIDCaFyp7XwuH3jF2XWun7DWOsskIxFGoM2bjkmhXC3gdf5N9ZLoyCxLecYozE6A+P8BzJ/U6lW7+xPWikgkBCFAUoF2ES2jfZaPgItKxqHIsV9v1mOMf41AKw+BdnawixZAUaS5xqNJg+0g5Voi9XGQbatJTO6jWPyv3E/u/ClnXGnghtECISVBIWIujHcVVtg2/atXN7bzytXrhbr44JdsXzVFa9/6xv/Miw9O0kQ/LSD/8Yb3zZ8wyteedPw6lVXRFFo//V7d4vf/8x/Mh8V6ekbIAwV2ug2QKsFERPCEbsBlHXIyjzVNNOMzBhSoJk66yC0oynmuabWyMkLwvlmCe2xj6YNK3a9oQFr3frJupOvVZq0IMOijTnAz2lcOaCkor7QZHp8jiWLO8l05j7etoxe3E0VEjB2+AR5MyVS2ilFWxc8xieNxU+YjGuMUQ5iarVGhoZ6rc702CwDQx1okyHtSbZYCzYRyYCx6QWyJCeSCo0biZ4k5LiEcDeDbZddVufkXgPVlTMWY4S/FQVWu6DOU0MuDFbmftLkHDDBYExOnrsESI0ms6CtQBvjvr6HXAjhbJ+EH4G14OBRIaZeKHDb3DQ7Hn6Q924+W1zY1WNXrVx1xQ2vuvFPj49OvVsIse+nmQQ/dV2ga2548UWLFi25RCmFlnDbw9sxlR56+wcwVqMz/4IaryHlSSnWu6VIpUBYFmamSKYmiYOgjZpMcwN57iiHxiCEot7MaSQZWZZzkopyymmotQsQ4wLFalcqBYXYqzWItn8XHpFpW/hka0FryAVPPbyDwIRkHsogkQ62bHOUhDCz7HtiN6GwLnCsBp1h/ImJ7y2EjxWE0wSy/vuzRkOu2bdtDwUcuM94CLZplxeWGMnTjz2DRZDb3N8+xgWwMU76xbrbp1U+qsgt38g1NjOgwbpO2P2cWnvKpiuP8tyQ54Ys1+6k1+77zLLUlUGAkZIkz2jOzZI2m+1exPP4HYBQesiIt49t9RlxpcJxJdlbW0AoSSADFg8uueii8y4876cdnz+1BFBKWYDhZSvPDAuFqta5TTWibpyhg810m7gtrAu71sLKIpy5nJU0ZqaRs5O8YFU/H3n36xke6CPNM6QrK/F3OcJoTK6pNTLqqaGpIdPaTXq88BTGoLPMQwiMD343IRGl4kmlWuEa0tY5an1TaXROniUUAsGBrXt55LbHKYVVkGG7xCrERbpkifu+fiej+44SKo3Jm+g8d1/Xf22MwFjd9gX2Ue3/jsb4adK+bTvZdd9TdBWrHorhUKJKhfQWOnj0jic4uOsIgbSYzCWYMeYUmIXF+HrSIlBKUqiUyHPXxBoLubVkuVuIWZ15TwPjkKXGorWr+ZuZIc+M8ynLU9CG0AMNM52zslTm1zZt4uKoQGm+RtaoOxCIFO2dhGjT4sRJVK12ZKWmbjXUhkIcF4cGF604NZaeWyWQP+dUKCIVKCEEtp7mzC00fUBZr5Fp/QTBYW+C0J3CycwE/bHkJRdv4NWXn8eWtcuJgGYzIfOfvRgH+OMLtKWZZcw2DLYsSBKDFo7kYvMMIwXoHJOmmCz3ZYHCL4hRnZ2oqODqVBl4iLJtN5DG5Ig8xWSKrC6Jix089K27mDh6go0XbqKzt4NQwcTYJI/d8Qj7t+0l9nqj1hjP8c1dcHuwjgOseciF9ihQKTG5QcoMdEoclLn/lvuYHJ3kjIs2UukpI5AsTM7y4H13s3vrfiLApHXQKVa7G9Va49HZbgomBBidE3UU6ejqZDrNXcOutec9G7IsBeOcKXWeoiIDJiNNM2wpYGIuJ0stKnLHujSGSiH2tt+SDmO4fskyLl+8hKemp7ljZISHJseZDgNUpewqXD8yRrrfSjSgMEIyl2ZoixAWGwRhWCwWujhJm3huToGM8SecVDTTlEYzcSeICVC48kYJicEQyIB8oUZvmPGqqzbx5hdcysbF/Whgod6kbgxBEBDIACysWrEUlKTVf2kinjk6x2XLushnprzQlAaTIWXodEDzFGld0inhFl65dgjHoNqBnqt57q4jkEvjG0fr6mCbpa66NjlRELPn7sfZ89B2Sp0dSCGpz9Xd6S0FOm1g8wyba4TWGG18g+qAauAaYmklKDfdEdogZI41ASJPsTIgQrHjvqfY99guStUCUgjmp+bImylxILFJA6MTbJZideZuGWtdvWT9pllKrIHSQB9htYNsZpKgxZMUCqMtWZq6Mah2TbgwOXmWk2ZNRNzNE4dm0QQERrsEsJYl/X0ukJRECItuNOiQgsu6uzm/p5dn5me59cgRfjw9yUwxxgaBf78cJwILRlhyYVnIU3Ks2yzjlpTPgz2AbE8IkiwjyRIk2gVG4OtrDEoGpI0aL9q4lJveeC0bBvvIDdRrTYSUdJUKZLh5dWgsOjOcv3k9fT0V5lJXQoWFCg/unOCc4U7O7V9CnkoCKdukcITAaEc4x/gxioW0mSIHi5QHepmennccAnS7h2jh+o3OsKlBGVcGWJkShTE2M6TjCUIEhDLAotFJA/LU3Rp5js1zdzpbgzCObGKFOxiMsChtPByiRVvMXENq6pBrClGIaaTMzc8hsYRSIG2OaWSQZVidYXSOMbnfSbgJWUvZTkpBU2esWj2MiQOMcUhY48e80oBuJO698r+E1WgrKfSu4O4D8OODdYJiP0a7JVtfR5EzVy9zEyolCGWGCCRGxORJRmwN55c72HzmJh6Zm+VTe3Zx0GOTrF8AGuucLw2Cep5jHDfUTxQkP22U3LMgjivazKc8cyeKsE4kFnty1i4BU1/gZRdvZtNgH/PzDdI0o6NcQBUj7tt7iF/928/yxbseoqMUkyQJ65f286JLzyGt11FBiBDQlJ380+2H+PaOnCNZN7ks+BZYggjIU42Vyi3ZHBUF3czItaF35fBJTI5HrDn9UCdYa3TuEjdtYtImNk0wjQVsfR7SBjatkTdmMfU5bNJANxuYJHHB6XX5rT/xjGgP7E/+MrrdYJKnLoHSJqQ18tocJAuEOiHQKSapYZs1TNLAZE1MlmCyFFJHYhHGl4atZaEBIsmyM9ZQa9Q8EtSXR0jQ2kFE/FbLGkhEmb3pUj7zuOHv7zhCGnQ6QKEKaNYbXHrWKlYPdJNmKcXQsjCylakdt2Bnd1KMMkqlGGkMYZJwSbWTLZUOdJphPTTF+nLL+C10TWtyPzF6trAQz8IN4Ht94RZQvhxuz6Ylyi+5LEo5RbTMWlSgEKHi21uf4QsPbOXhoxPMWsGxdBevveJ8yqFCG8t7fvG1fPu2ezAm88uxgHmqfOmRMYrbpshFjAoCkAEikKRZjlSBO2G1q7+lhvmpGYbWrKDQu5VsZsERX6zxl7FjjkjhZua58sEqtff4UliVt3cH1mpMlruJj3U7CvxWVtjWJMc4yUXcbNzN1T37V4DIcxQWYRXC5O60bi3oWostk7f7CusnSNZvo2171OQ2y0napH/dYnqGh9hx+DBCBP78EX7jbtCpRqkIawVBEHF4XvPntxxiQQvKXQMODIhDnJZsg19+zYtQWHdr5HNk408i60eZmtmFrA5TGdpMoXcDRoZgLRUh23uS9ri5dcJbS5prcocSbm03Wz/FTy0hnoUbwC+ArNuY6pZmpZ+1O0M4gVQBUkjKRVfjhkHAXDPhf/3nHdxxogb9i+hcvJjd8w1++Mw+CoWI6YU6F52xhg/85ltpjhxCWosM3CsWlLppyjJaRAgZYKVEKCeFbtPcS4o47EukJMnMHCIOWbxlIzpNXXAa48snjyRtNRt+SmKNc23PdY7NMkSeYfMEm2fuY4ShtbeWwi2ypHTj/sAzu5SQniBv2xtV4W8dk6eYLEWnTUzaQKd1dOu0T93Jb3XmAj93Uy5rDSI32NztHoTOEVaTmiZnvfgSZk1GluQuIXPtEltK0jQlT9zh0EqaNChgqoNUeoccGND7CsyOHOI3X/ciLlm/ivlmQhhYGlMHoT5GHEjKsoGcfoqpvfcgTBNw9lCBlK7PaYW0aYW3a6Pzlgoezx5Z/VnpAYT1NG0f/EoEJ8dgwo32jLcRKsWBewGkJDGGqFqlqCO01dg0hWKZz939KFeesZaOKGCyVuc9v/xG8iznzz7+r+ggptDRiVAh0m9vtXGzVqUEOs2xaUIQh+RZ3j5hVGYYPzLCigvOYnT70zRHJgnDAOsRk0IoN0/3QhrCnJQkERj0qVilNuDfN9DG9zqt2f8ppaFDHziqW3tmbixI6xmOnkgjBEa2xLI8n9jjQNyUyd0kwhqM0O5jtEUGkvrsNCsuXM+izevZvnOPy+fU3Vgo50Jfn593dbuKMWi3sSXyekbuZ2wu1NC1GX77zS/hf7ztVcw36kgMMp1m/uD9BGYBrVUbqyQi3+b791+J1mSttSPwtM/WzsXzLxBuPyLNsxGdP+0v4Nf/9hRJDzyxpIUqxLuhBFIQq5PkEGMtWZ6iczfdIM9QSrB1dIp/uO0eVCFGW0ut0eD3fuNtfPWzH+X8M4dJZyZpjI/RnJun2Uwcpr+lv5mmZAs1lAqdWYUxmNwQWJg/OkqtUefsG14ESruvC1jRMrIGSUtv04/0cuNqdu0WV7ROY238v5uTuCIj3OjXOJhBSy7C2lPOPOuJ7tpZ0Wtt0LnbcVit0Tp3O4XMQGawucXxbFwt7RZ8wjWZErKsTlhVXPT6lzI6NUltfBap/elvXEILFPOTU+7naBv4ORhE0mgwPz1BfeIEGwYK/ONNv8YfveuNpI06Jk8pBU1mDtyDmd2LsjnozJdjOTZP3Ya7JRdDq/bHKW9bR+V0MpSyLSHj9QeeFfeWn/4YFDy+J/CB4OHJApRonS7SSwKKdl9sjBsUam0xWepONyDXhrCjyufue4Jl/X287aLNNBp1pufnedGVF3DZxedwz32PcOd9T7D/0HGOzy6w69AJtIx9DW6Yn5ikb2CIpnEJ2oJLqCznyFO7OPPS8zjzFS/iya9+jzgqY4XBGGeLaoUL4vZCxwPXWuK8rT93qoqt5R7tBVsLxCNOjgj+K4SkraForUNfGmG8SrVfGvuDpCWy5ZxebHvaZtEoGaCzlIQGL/7lt6MGezj84OMEqQHhsP95ADISBNoyd/QEIbItqxLajMVdMb3lMhtWnMkV523khRefS1ccMruwgMTQEWtqRx5k4fCDVJQ7SIR0t50xftKnc4/l8ip4/n3GM+5auCYvReH/XGCEU9J+zidAYDzNWwgUTnendQW2ZMdbtXULJ5ODw/t4ppJtnWicFJsSnV189D9/RK3R4B1XX0TFGhoLDZQU3PDCS3jVCy8htZZUG6598+/w6K4TRMUSgRDUJ8axzQVkIMjrSRtkFghoTMyz/6ldrLvwXNJGys7v3E4YlxCBwGh7MmDtybasBQOmDXI7BdckWuXOSeUH7esu2RLAakFaW75gtq0W6oRRWkoTLcBaq9H1kyRjHSRb+o9SMiBtNtCiyZXvfhNDl53LjkefIp2qUSwU/IESOH6DkCSTs8wdnaCoAoSArNlky4oePvuh91KOAwrK/Xz1pMn8fEIhFBRknflD9zOz907KKsWKwO03rPY3ncboBKNTcqFRhO2Fm/K9SgsZ695W63DZvgy05tmpgX5qt4z4ia/g1ZA9etKN53Aza2vasoR5bmhk1oGqcMsq08Kw2JOw2xZkN6/28L9++ADv+9w32DY6SalSpKtUIE0y5mpNFmoNugLFy6+6ENuooZTHoDSa1I4fpxgEmNw3kR4aUZARU/uOc2jnAZZffQlnvf4lWJmRNZvI0ClAuB9HO3ixcWVU6/QVrbGv9InR+nUyWxyozXjF5zZgRrZRSy2IgLAnW+N2v9HCJHmSi2l9H4BVEhsGLNTmUZ0B1/3Br7H8uivZv2Mv4/uPOV6CtVhtybMMYw0xgvGde9FzC37yYskaC1x30ZksKoUEeUbSaJIkKZFSdJYhbB5idNvXmdn1A0qi4TkC3uegBRsx2sGxMWiryY0l15q8tRMx7vtv8RbwzW9L9Es8S23ws0KIcZxaV0Io6XFfRiNbtECjyXNLbjRN7SAM2r/h2p5kHLlz0FMUcU4tUe8gPzwyxeP/8k1esGYp12xax7mrh+ktFVBJRgZce/WFfOzTX0Frp5YQWJg5fITqQD8qCLBJBtKdqAJBUcUcf2ofFsPyy86jc3gJu2+7l/EdB5AogjhGSDfWbTO73N1/yonW0kw8yWA7xcPABc1PbHncx7alWMRJ1GdLSUK0ckWcRJ8KJCJwnyvPUjKbsOriM7j0nTeilg2y58mdHHtyH0XhhG+FFEijsUIRhiF6doHRZ3YRSYvQGXkKfZWIl15xARYIAkEkBaQzNKePMntiB/XjTxGmE5RDsFZijEC0yj18U9uSeTwleQ20DxvTGiJ4l3BrHBVTtpSspfdPeD4kQOsmsMJ47MlJgwiHxdEoIfyNkCN9MEj/YhpjCC0YoTyN3DlXaOmgBGFHJ7PC8LUDY3x3zzEGVc5Nr3kp165dQb2ecO7GVVxy9lpuf/wAhWoVa3LS+VlmjxyhPLyc+UZC6IpUnwRQJOTE9v2kzSbLzz+TS977Tsa3Ps2+Ox5kfM9BbJJjVYAKY5RSbYFbhAOr4dGbwhPM3e9bGqGesO4nP24s7EJfWr95bkkntmxWrTgFLe56GZ27E9XoHFUKGVy/gs0vvYrll57HTJKy4/4nGN97hILy/ZeRYCC3GhGEFFTI8e3byGYWqBQLSGFJavOce/Yw6xb1kec5YaCYO/EUJ7Z/lyibQtomxUAiwrAtGgDOcrVVn7WEwfANthMLds0v2vV31tg25EQYjdEWKbwxidacCuh+TiaAPSXwjbBo6fm51pPOhWPcthaVVniGlnW1bOsTSL/U0T+hLuuQjdaXGrnJEUIQd1RQQcSB2Vm+8MiTXLluuftzKXn3O17LnQ9/CCG7yLQmEiFTBw9T6u0jKpVIa3WUE+jy8AxBURWY2n2M+clZlm7ZwOD5m7j8grOZ3XOAY1u3M77nELPHp0hrdadtJd1Sj0B5owu3cc592SbtSUW4tiR6m094amth2g2x0dqRabQrK2hBqxVEHUV6hwcYPHMdKy/aQv/G1SRKsOvAEY7uPEAyO08xjl0y+uGpzXNMoKiUSjRGxjjxzB6KUdzuO0Rzjje95EoUkBqL0jVm9j1AsXmMKPIqdcYpUQvvbXyqTUAL3eoA1YGTkvF8CW1PlrVuIegSXOOgJWEUECjpylEhnpUeIPhp9gD25CYAoSFWAWEgMEmGEIFbNEmfJf5az9IWzdoSBIpAKWxqvDygaJ+wtpUQmrY+p85d41WqdvD4iSmeHJ3mwoEeZpoZL3vhxVxz2RZ+8MDTlDo70VkKacbYzp0s2rwZHUXoJGlzjFveXcWwSDadsO/uJxjbc4RFZ6xmcO0KNp+9EZk0mZ2YYWF8kvnj48yOTpJNzVCfnqM+3yBpJtg0c7CEzPiSSfvTETcObfENhDy5IJLOTdIohYxDCsUypVKBQmeJsLtK19AAfcNDVJctonNoEEoFFppN9h88xomDI9Qm5gmEohAU22Z50jvmpdbQ2dFFlBp2Pfg4gXZyL4GS1GemecFZq3jZ5edRTzMCBbWJ3SRTe6kEkGvdXmCKNkzcthOg7YHgwYNCBo5DrcFKSZY5PoH1RiBGe0Ci9OaCSjoGH20Y0HO/BJLGM6o0BIFyyE+doKSfhUvZXohYoJEmTuTBWmIpiQOnpCA8n9f6+rKNWZHtXbrrEzzEeNoIvvHIk5x9/dVO2UAr/vB97+T+H/8PdJ5hrUGpgObkNJO7dtG7fj3zGLI0IxAWqWy7Zo+CkFhIsqOz7D3xOEee2E3n4l66lvXTMdRH16J+Fl+wmVAKyHOSNCVNUtJGE11rYpKEPE3RSYbOc3SaOU5yi8sspduEK4kKJSqMiEoF4nKJqBwTl0oUoiJRMYIoxCjnVbywUOfQiXGmR8aZGZshnW+gBMS+PNHavW5CSEdrtJbOvj7KccwzP7qXbHKeUlzwvGhNyTT53V9+ExJLI0uIRcrU3vtRep5cKP8e2TZZqD0Js7RvzRaZ1FqJCGKkCp3ShIDU5Ahv/tHqdYRxUiw2zwnbgwA/LjfPgxIo9zN9Yw0yCImUR/tZ4wO5BRGOneJZkhLgoAKxkhSCACmdfKETXW4Fuv8qRrT19wVu/pxZQ1go8N0nd3Hdlo1csqSf2Vqdizat4Xfe/RY++NFPU168hDTJiIKA2ZERUIruteuoyRCTpF4apSVD7mh+QRgQKkk+3WBy+jDju46iShGFaplST4VSdweFrgpxtUSxFBF1dRD3d6Okg3ooj3CUUvmf49R52UllZ2O9Ip3RaJ1RzzMW6nNkUznNRpOskTEzOsHc+Bw6y0FbgiCkoAIMri+QAqRUfk9hyZWic1E/PaUKO350N/OHT1CKC24SFEbMHT/C7//Sq7l002qmZ+aoFCQLx56kduxJykGO0R6gKRSgfU+Dx3GdnPi2DjJjJagYoUKCTJFZSLUBqxwX23hUqCfOW2NRfoHSwgs9G5uwn34T7Bee2hiiICAO3TRCtacFtJs9gZPtUB47U4oCinHgGqZWWWUsUpq2rAjCtB2FWuJPwgqUlMyJkL+79R42/OLrKAchs7Umv/2rb+DhrU/z/R89TLl/gDTLiKICc8dGMVbSs3Y9WVygWau520t4TJC3MjLa/b4gQjACvZDRnJ+iNjLtplNSIGKFVJLQn+gykKgwIAgDCEDKACHNSR2g1gLMj1VzbbDaNYoOMmP8AaBcky6dUK9uZERKIQMnCZDr1JVSSnh9freIFqWQgUWLCFPNk7fexdyhYxQLFYzRRGHE/Pg415y/kfe987XMzzcIFYTpBIefvo3QzGNt6PcwEiuNTwJx8rT2uqQI3+gKgbYGqQoIGaKkO7Ay7d1w7CnHZEuFTzu1P4X4iR3Lc78EAiE9EjSKA0pFp0HvRmAtxyp/vUpFvdH0CeBkP2KlHDCsTSR3N4rwSjpCyJPCta2pi7cGDYtFHjo2wWfueID/cc1l5At1jM751Efez/VvfR/b956g1N1NnmuCMKI2OkGaavrXrqXS2Umz0USnKUI7Yr6VHtEphZukWHcbKSWJAuXrYolNja/1M3fq+e2oVcqfeE6Ph9Zi0FlHIhw0xzfTFqVClAoQgQtypHJSMFKgwgBZESTzCwRSuZ7I7xy0lz0XoaLc00VndxeN4yd45p6HyObqlOMiWhuiKKY5M83GRVX++k/eQ4CgbnK6i4axJ+8km9pLVHD6ocL4Zte4z+9e85aPpmyBe07ysDWgIhAKJQQZjjgvpXCKEWiEdocV0okKxzJoT9JaLLPn+iLM1ur1mSzLNEKJIMCWY9kGPDkWgPKkJPcGz9Ub/jh3zKByFLm9mS8LWjNmjJPowM+V3ULKnyZ+06jzjLBS5rP3PsIPdh2gXCnRSDL6uip86VMfYs3SHupzC4SFIhZBEIbk8wscf3I7s4cOEiuoVDtR5RK5VP77dUsfp9Dg9X2sxebG7xkc0VZaN8FSgSJUIXEQUQgCClGBclSiUChTKpQoxhGFKKIQxRTCmDCMCYICYVhEqbDN5XUVY+bgz1qTZjmqECECBwu3CIyFTFgIQoo93fQtXkRs4MA9D7H9Oz+E2RolFTmjkCCkNjXFuu6IL33iD1g52EOSpnSUA2qjTzG28x4KgSTT0tXwxo+sjVPS0nmLV61dj2X1Sc6zv8WkittIUIDETUbdhOzUJZ8BkWsqUXhyAujU6vRPmQ/z00uAlhHCvn37nqg16uMqkCigs1zyy6NTFBj8qW6lZC5xlEMhHO6yGocIbVC4U1h4NQehzSkJYTyS0IHBrJUY7RdVxpAWK3zsWz9g+8Q03dUy9UbChuWL+O5nP8I5aweojY+ighAjvNaPyZk7fJSRbduZPXSAMM8pd1QIyxVsFGOkJEegrWvgFa0NtwsAofVJ7qu/5dx/1k69GdM2tDCtramnS1prTk7AvVYoSIxo4Yy8iKg1ZCYn6ugglxIbKIJKiY7eXrp7e4lTzcijT/LULbcx/tROClY5SHraRJmc6ZHDXLi6n69/8k9Yt2wR9UZGR6mASsY49NgtxKKBkR6ubvDfpwt4Y70IuyezYB0TTRvtZB991IZxmZb1U6oNC0neVsJzk7b2BAMB9JSKKLAqCES91pgbHTm+69RYek6iQWenp2eMMU0hne9VX2+XF4M9le52Urxkvp6RnwTN0lksIrVBaNoiU8J4bL5nWdGSVWlXpS0OmAuWMCpywkT88Re/w/aRUarFmEYjY93yxXz/8x/j1S8+Hz0/7QRfPQwxUAqRZswcPcbI9u1MPPMMydgYkVMsoFAsoiJXQeZak3tpFnIfCP6kFlp4uUYDWnj1E+NUqY3FatHecmPd9MTak+rObWtTDVlmyLVBa40wEmkVYaFIz9AAHZ1VQm2pHT7G4Qce5ukf3MHo9qcJmhmFIPLcBccNSKbGeOM15/LVv/ljlgz2UaunbsbfnGbfg19DLYw4jwR7UukNc4rIlr993UjTeuiD0wmyXpXCaEsYlV0hIyDVmlqeuSRufS5f/xvPleiMwjZorpk2pw8d3rP3Od8DhEEgA2elRQD0dnZ64Bc/EfreJYb5Wp2GtgTC6Xl2d1Qh146OqHPvnugaLtfw0ubSGumuaHWqA6P39ArLFfaPHOLwiVHOXzyIVoLZRsJAV5Vv/P3N3Pg7f8XXf/Sw2xTnGVY4XEwgJZicZHKS+uQkMgqJiyUKHWXichEVFwgDhRDKT6akP/E9XKHF+/DYhdbExHrhKJTwezDh9hzW1/L2ZKOphEJ6JTqkaP/cZiEhPTFDbX6W2swUjZk5bDNFWYjDCBFGbplmNEIIlFQ0F+Z56yuv5Z9//51kQL2eEoduATU1eZjp4/vojVqKkKItTmZwZadsScfYk8Jlwv88rd2JMC6RgzBuD7rqWcZCnmEi1Z4AIk7aT8VIqio4eSxby7PBiv+pJ0CSJNoYbR1oQdHb0+3tebxptPUTHePAcjPzC8w3EnrikBzo7aggcz8319bbgnqCyikNh21BJTmJk2nTT4wlnpngT990A6/avIFmrjFAZzHm4IkJ/uxv/527HnqCoFg4CcEVp6zzvLGGtGCShLTRoDkx5tr3OCSMYsJigTCOUFGEigvIwBlvCylRSiGD4JRmV3mLpJbl0slm2AlIufGu0GDzJkY7EYCs2SCt12nWa6T1BmkjIU9zJBYlBYH3EWsjbO3J0WRr4FKoVLjr4e381v/6DL/z9lezor+HJMvRGfQvOQN5+es58NDXKJt5j7x1m3vftnmxXNuWXEG0gH2yjfvXNsOYEBnFviQSzOaaWW0cFdOXsXgIt0IRS0l3HKG8bthCfeHw9j17xp7zCfDU1q3H5xfm9vcPDq2QwEBnmUjKk67qHtNurUEJxfTCArONOj2FbjTQVQ5QJvU0RH2yBhbGJZHXFzKerorUGCO9fagm1xml+gIfesNLeN1Z62kkKUJIylHAV2+9j9/7809zcGQa1dWDDJ1YFUpidOZlz51sCw7ejxIOdekE65zPbl7LSGtzPmC8c6R0eqQtxxcZtkz1FEq52lpaiVBB2/XxpFCWx9YbC3mC1rmDYhs3XXLgUYXyvxzwzjinSS8Z7zQ93XAh9HRT4evz0YWUf/jmPdx2/+P80btex9te9gKsgTSX9K68ClTMvrs/T5Fpr1/qaJP4m8rBPvwk2ks8Yk0bR4WxGFlABjG51iAUs2lGw+RYGyKNl7Ix1m34rSWWimoUtyzUmJ+b3f3lf/u34y007XMPCuG/8W9+85tjv/sHf7jfGl6Igr5qiUIoSbWzMvX2EO5UUIL5WsrEQsrKHkuaWTqLEZGEZqa9qrhFekFcay2BF7tt4fCF8SWE9FIpc9N84MbruP6s9UzXm4RSEsUBv/+xf+Gjn/4astRJoa+fLM+QKiDLM4zWRNUOuhYNYLVmduSEw/vkGimMCzpF2+NXBcpp4wvdZnk53X13a2EMOhftAMnsSUKLkF6JueVI43WDBK4klNYivbSJCCTGcwtaGp4t5WqjDblxGqkikJR6uujtHyRJU+bHp7BpThyESA9IqvYMcLye8isf+mfue/gpPvb+X6FajKk3E3qHLyC/pMaeu/6NatBs9wFSypOyLcYHcIv17Gs94zH/hBFBWCDTFkLJZK1GlmmEdvgmty1uiQhYyoGiVIjcFM3hjQzPgm/Ys4IGldLpxligr7uLUhzT9CwqhDi5VQRqzYzjk3OY4UHSNKWr0kE5iqnnGhEohBXolmiVr02dlPfJra0UglBJ0slR3veKq7jxvLOYna8ThwG5UvzK+z7Cl791F8XBxWjjbgkpJUlthsqyASrLFhN09hCUy0hjqK5YTDK9QHNqioWJCbKFOlmWtWHOSkqkCp3JhbRtfqtyBEoP6HMHgpTeawzVZnO1WSDGYryJdSswbHvOjleYtu2plzYa421VZRARlit0dVcp9fcS91QhLtAZxnTPTpOMHGFuZIx6U1IqdTjlt1AR9y/mX77/EAdHTvC5//UH9HaUma/VGVx9Kc25SQ4/9FU6Y6f2jAcgtr6/loGCk1f1m33hONgy6iAMiyTGbY1PzM2T5TkyN20FcDymiEzTEUlKUmJzTRCpZ40V/2wkgK43GnN5niOCkK7ODqqFiPG5jKAUOpqj1+VXRtHIckbGxpFqHcYaujs66CgUOb6Q4JxI3d1rDA4HL/hJFQEJKlQ0p6d43TnreNcVF3h3E4kII971u3/O1791F6WhxaR5elIpWTdZf+0lnP3m6yn39XDvLfcyemCMchQ5FYfeTqp9nXStWYFuJOT1BeozC2TNJnmjQdpI3XWv8/aUQ5B7uICzZjKZxiRNh+iUyuP4PRPKAIEiKsbIQKDTFvCsTZtx6FelCEKFigLCQkxcLlLoqKIqJVRcwEYBWkoaEtA5SVJnUZ/g197wcrLjY3z+i3exZ+8kpUoX1miSPKerf5A7th3g7b//MT7/8Q9QDEJmGynLt7yE+vQYM7vvpFIK2w70Lax/S9NUtJ3sXSmUZxlRoRMblhC5wQjBWL1Bpi2hNl4WE2cQqCy62aS70kVRSiulEfVaPZuZnt4HZG3X7udgAlhjjBBC6KOHDj22adNZc9VqZ7VSCG1/ZyR2TTaIrD3lZ3O4aG1gZGLGETxyTblSpL9aZuf0AjIOHP3PFxnG+lGnh9oKAUpJskadM7tKfOC11xEaTYalUizwvj//NF//1u0UhhaT5A2UCMmaC4RlxZW//gssvu4SGtJy5MQUpUU9lBaapFPzRO44JxfW1d7VAkFPha5lDtyHztGZRmQ5upmQJxlZkqGTBGEMeZLSmJ6kJ7SctWIVZ29cxZplS+nt6cZaw/jkNAeOneCJZ3bz2I59TGeW6mAfQTF2pneBRIYxKopRcYiKnEexCIJT1KItuSeVCCk9jLrO8uEuzljXybweY/XqAr/72y/nu7c8yS3fe4wgrKKCkCRp0Nnbx20/3sVvf/hT/OOfvRfdyGhkEWsvu5FtUyOkc/txU2IPF2/RQ4VzNTDWlWVIg84tpWovBE41T2OZaKTuJMhN2yu57WecZSyqdhAAUgUk6fzckcOHnwKs0fq56Q9wKip6rj43abROAWIJi3r70DvHEKboGk03qnD1sgg4NjpDbt1AsRDA0qFe8j1HwJbaI8Q2kd60WJdujCiMJKrN8z/f+joWxTFzSZOeYoFPfu1W/v4zXyEeWOJKHiHJ6vOUBqtc/YFfo3jOGmaTJkeePMTooTHCKKBrWS95pcjC6CRpMycKI8cEQ3g4h/EKFxJZjBDlmIAOIuWWfLFwN1G3rvO6i17EKy4+hzWDPf/X5Uvdws7DJ/jqXQ/yzUeeJilVCAolcusJIn6W5vwIpJ+geWatFMhAOU51llDuClixbik9/UVm01menmgwOjvHqmqFl77yHIaW9vK5z/6QLCkRxQWSLKN7cJAv3Xo/Z5+5mt950/XM1GrE5T5WXng9T9/2aTqsU3xo8ZKtcMsXx3fwEx1j0ECxoxcrQqTIqSUpo7M1Vwp6dTwhjVvwaYvIDYOluM1ptmC1Mfn/AVn/3FqE3XzzzQCkjSy12FQAEbBs0RAm85IZfrIgfPMYBIqRsRMsJNqZUgMrFw1C5nVsOGnghtf3N/5aDoQknZvhhnPW8aI1y5lPU8pRzGN7D/OHf/6PhN1DnpAekjcTKku6uPbPfht19moaScKBx/dwfNcRIhTCSJpJguiI6Vy5hMLiHrLAkuW6PV5t45A8DCDPc9IsJUnc5GZ6bITNPSH/9Ftv5ndf+SLWDfaQ5xnNJKWZ5zStoZbnzDZTphfqJI0mG5YNcNPbX80nfvPNDAU509OTpMbQzJ0bS24N2su0oFqexU5iJs1TZMGybEMf688fJuwSTCxMs5DmzOcBO6Y0j080OFKfYcN5i3n3e19FGCWkaeqMCLOMYncvf/mPX+GRvYfpKBZp1DN6hs+lunwLzTRzN68BIzy/oUXi8aNNJ1EUElV6ybwzz1yWM9aouxJKt5p2v0DTjqLaVyqfnIYZkzYajfTUGHquJoAFeOjR+/ZNTU/vbX3BJX2dyDw/RXy/JY5vCZVkdHyKmVoDlDvhl/d3EAecrDtPvuptESprndZNr9C840WXtumHubX84Z//A3M17dzXrcDmmqi3zAv+8D1kq4cwmebgQ89w/OlDxEHsmFdePD/NMxo2I+rtoHPFIEFvmdTmpHmC9to91pNNhIc/KKVIZ6e4bt0iPv2bb+WsoX6SNCXTOSoI0XHEmBDsXKjxTK3OVCARlRJhKSZJUxq1OtdsWMVn3/9rnL+kh6zZIIiitmmIU5ZxY8lcu8QLCoKhNX2sOW85nUtLzKezzDUaZMbBkJuZIZURe6dTnplKOFGvs2hVlV/4pZdgqbVfQyUDZlLJh/728+RWOEKSKLDi7BeSyrL3/zI+gK1X/6Y979cGZNBBsWMAbQQiiJhqNJnLtBdF8Gp7HlNktSYWgr5KBbxU5fTU9N6H7rtv36kx9JwsgVqN6Vc//9Xp9//PmyYQkAHDQ30UpHCqyUq5F0O69b9UkqmFOmOTcyxe3ofWhlWL+uksFZnTxik7tLa/3vfOGINUEcn8LNecu5qzB3tpNpp0FAv8x/fv5Qf3PEHUO0iWJa4ZVTmX/cYvItcvwRjDyLa9jDx9iI5KBybJEYForZfb5VnuN57F/irFaoVkrkZeS5xtkgi8bqbjtTbnZ7liaS8fe/tr6ZKWXOeEUcS4Mfxw117u2n+Qg3PzLGQ5BIrOQszanh5euGo5ly9dTMVokmaD1Z0V/ubdb+YXPv4Z9jcaBMVSGyqsrUYGUCrHdA500NFXRkaaRjaPrmX+VnVMY+llCFNjMSpm92QDJQJMp2X4zEFe/qrL+OaX7qXS2YfWmkp3F7c/vINv3vkgb7rmUpIkoXNwI51LN1Pbfw9xqLwTTUtg2zpjvRbmqdJF3NFD01gIBIcnp6hnGSIueAkXD10RDlbREUr6ykW3b0CSNhsTX/3qV6eflQnlT7cDaM9mrLVOvTYHliwepBRJtNbtDaX216kQgoW65vDICYpRiM0zlvb1MNjZ0XZ+NNrNk60RaI8tE9pS1jmvu/Qcd6HIgJkk4xP/9CVEocPhZ2RAnjRZ/eJL6bhgA800YfbAKAcf30upWPHukRabO4tVm+bYTCO1cQp3BkxuMMoSVQtEXUVMkNNYmKIxOQrzUwQLU6wvCW5688vpUpDrHKUCHhif4De/cys3PfgIt07MsNsqjsclxgol9ljF98Ym+JP7HuLmu+/lUJpRLBRJkoS1nR18+O2voTedI5qfgrlxdH2SQtHQvahM13CVoEtSy2vM12ro1EAmMJlFa8hyQ24kSe6F6izYoMDO8RrHGjBam+OcK9dx5vkrqdcXkGGItaBKVf7hC7ewkGXOG5iIResuJLMFz8rzjppWtze7wgryzBBXB1GFLnfaA0dmFsiMbXuwaWOcTqxxNNbeYpFKGJxitEfWRgr9lMehP+0m2HqDs/rY+MSTC/X6awuFYtDXVbYDPRWxZyZ3FjtSIIXypBhJjmX/sVEC4YDtPUXF6sX9PD1xgDAK/Pzd6QtJBEookoU5rli9hPNXLqXeTOgsxHz+e/fy+M7DxF0D5DrDGkthqJflL7qUifk5gkyy65FdREEB4yHOUgh/Elm3wLCg05ys1sTmCSJLKQkHoxisdrBi2WoWd3YwVApZVK3QW+lgsKvKskqJTOeEQcTdoxPcdNf9HA4D5OAgcW48Y0t6bjNATB4qfjg7z/jd9/JnV17O2kKBLM+4avUwt9z0Hsbn5plPEmaylOPNBoempjg6N8347CzTjSYNozGBQkURYRyiQkkgnGxJqo2zP1aub8lUwO7RWeIlXZh0nhe/6lKOHL6FPHNb5qijyuP7T3D7w9t55WXnkmUZPYvXoapDZPP73eLP65AKQpSVoCyZNvT3LkVQQImEHDgyu4ANQ49yb4mbudNXpzmdYURRKSu1FfVaIx8dH3sSqD/nbVJPeczU2PgBo/MEbNDdUWDl0sXsGN1PXCqQ+zW6EQESiYwL7Dl4xHEqpKAIbFo+yLcf3YmwZYcVNU7Z3m3DJDQWePGWy4mBeaNpWvjit36IUCVnAiEVJmuy8qoLaXYWyRqaE3uPYmoZoQpdEyeFV2+wpPUm5AkFk9NbClna3cmqwcVsWLqYVb1dLOvtobsYUxCCGAhPKfkya0jTjFBJ9jab/OV9D3EsLhHGEbnOkdLpboo8c5DgUKECZ3IdV7vYvjDL3zz2OB++/DIqQmB1zsruKqu7qz9BOU2BOjBZr3Nkaor9M9PsOn6cfVOTnJifZU5npFFEUCo4MXap2nyJQATM54bDM02GOyP6eyMuvvos7vj+0xRLRaxQJLLA1370EK+47FyM0cSFHnqWbWTs8b3I2LaJTULkXgZSoQko9S5pAxEbxnB0oeZ2FK05T+7sYSVAlrOqp4PIFyR5licTY5MHOKkCyfMhATh89NjhRq0+WilVVsXAmhVLMY/s9kV8yxTYSaBEcYG9R08wm2o6lAO9nbGkn6JHVbY0Ph0OSGJ0k55Ycd76VTRzQxTH7Dx4nAe37kaVSh5ObCkMdNO9eQNz9TqiaZgfmaEYxG57maWktTqBSRkoBawb6mfzsiE2r1jCqv4uFnVWKXLSXMamGt10N3UTQSoFMpBuCy0lKnLV5b8/spUdqSHoDMlz52Fg0pThvk4Gu3swxnB8aprxWgMVR+RpRtBR5YHpOW47epTXL13a2pKQWycjo1uqasYSI1gSRgwvXcrlS5eSbTqLWWMYmZlh3+Q4O44fZ/fYKHumppkTAhEXCOMQgUAFBcbnE3rKMVGzxtqzVvDQA7tdGWUgKpW5b9tuDk3OMNxTJdeagWUbOfbYrRTy5kk9V7wvQW7IZYVq1xC5tcggYKpWZ6LRRMZF2u6t1mmyGiFQxrC6142GRSBpNGqjh08cOvy8oUS2nttvveXga179yqNyUK4COHP1MkIPG2gTPax2mNEw4OhUjcOjE2xZ0k9qLOuXL6W7GDKlDVK2Nonuf1mjwZrlAyzt6SJNEqrlIvc+9CQz0zXigRLWQJ6n9K9bgS5GiDSnPjoLTU0zaxBkDZYWI85dM8hlG1dx3pphhruqVFonOpBlufe/BRkEqEgR4Yz96sCcNsxkGTPNlJmFGrONBkfqDb57cARZKKCz1DXvWcZ5G1exYfkQxis9r17cy9OHRth9fJQgLmIzTR4X+NLeg9RyQ1cY0lGI6S4W6QwDykpRlqG7dby0ZJYmXlgMqkLR09XJlp4e7Nr1zOQZuycmeezwYZ44cph9s7NMag3FIjYOmahrCkLSVQ1YunqA3dtOUAjLhCri2Pgo9z72NG+79jLqzQU6+oaJKn2Y+QNIEXgbWScXb7Uh7Ooi6ujB5JooDNg7Ns5Ms4kslJ3CtRcQE8IRgjqkYLi3uw2ZqdVrR2+/5daDz5sEaCH5tm7dOj+/MH/MGI2Sio2rltFRUKStUqZNaDFIFTJdS9l54CjnLxuklqQMdldY2tvJ2MgscbnokI84fLxOm5w5PEhHIFlouNLg/ke2uVtF555ID5XhpaS5pj41x/iOfSxSkguW93PN5gu5bP0qhqslnDsY6EyTWAeDDgNFGLqXqgacaDQ5ulBn39gkh2dmGJldYHR+jqkkZSF3H9e0gkxKgkoZtEZaSZamrF82xIYVS6jVZ5EIh9XHsnHFEJOzc0w3M+dooxQHgL/atYfIQmgtBSkpC+iJQnrLZQbLZRZ3VlnWUWEojhgolKn4N1VrTZYmCKBTSS4dGuTSoSHmzj+fg9MzPHL4MPft38/26SlGFjTlpZ3ERcOy1UvYvXXE38o5uZbc88hTvOXay8i1plDspNK7mLnJvU6wwPo1OZDlmkrPEEGhm2bqtja7TkxTzwWRUViTO8if9bKQRtNXjBkoFx3TDMl8vX5s69at86fGznP9Bmg1wnMT42PbkjR5QxCU5LLBql3UUxX75jPn79V2DHHKArlVPLnrEPbK88i1oRpHrFvSx0O7jxAXIg+6zBHascRWLepzjF0hmW9Ydu4/DGHgHVAsthCgA0Hz8FE2BgG/ctUWrjt7A+sHe4n9N9pMc3JrCaOAKFTtgN81McXuyRl2jk6yZ3qGw9MzTDQSFjQk0sGjgyh23saF2BPXJaEnzbfkH8GyaKiXZlZvy4JL6ebhsRAM9XYzcXAEpaTfkIeoYonMOtuoBQOjGPakKbbeQI1PEGPpEIKeKGJJtcKqjioburtZ3VFhcalIyZdtaZ5idEYsFZt7u9jc28ON52zhyWMj3P7MdvbOjzGdTVHpqhAVI2Ru0CZFhSFP7NzNTOr0U7UMKfcuZtJAwXq2mM0RYUBqFR0DKxAiRImUJvDk8TFsWMTq3FFAW4hXodCpZtlgB32F2JIb0UxSPXn8+BNTU1Nzz0YD/KyWQAAzswsn8lw3jLXlgWqFNcuWsPuJA4Rh2CbKYwU6N6g4ZtueQzR8UEtgy5qlfO4HP3ZEeHFScjxQiuWLl6Ct3ySPjjM6MUtULGDyhGxijPLyIa5Z2sWLt5zBpSuWMRC4j00yTcMYgiikEAWkwNFmwjNHR3j0wHGeOHqcA9OzzEuBjkuYUCKDANlRQEhB7Hu1FujNeu1Po7W/AWWbQeXpOhir0TiLVun/jkK0Tae1cclhdH6y5xHOhlQKgYwiVCzaWqbzCGatZc98g7un5igdPMxQFLO0XGJddydn9nSxrquT/rhM7G8HrRPKgeLyJYu5fMlijtQWeOLoQbYePcFdMmFscprOrk4KUcSJ0SlGTkywYqAbYwzlzgEMitxoB19BIgMwokDP0Er3iqiAsUbC3ul5RFQiz/M2DdJY4dDBWcaarg6KgBWSRrMxeeTo0R3PZkw+qwmwa9czT1962YUjXdXK2gA4f+MqvvfILoQoo/FED9x2MAoD9o5McXhyjuWdFRJjOXf9GqqFiNyACgMnAWIMHcWYpb0doA3FOGB8fJKJiSnyJKNSUbzhDdfxzl94Dedv3kAByNOcuUZKFIXEoSJBcbjW4LGDR7jv4DG2HhvjxEKdhlAQFwi7elCBq10Vvl2RHp3ZYkK1pCy9CUYgHdbfWIu2GiUkWW6YmJxloLuCzpoIGbSphoqY0akZhzPy5H+DA7adNL5oQaVDrDSnbHGc/VMQFoEiuRAcNZojaZOHjsxTOnyYxaUim7q7ubC3h029PfRHsTccybAWlpTKLF2/iStWbeDsUi+f+sJ3ueexHSQmQMiUiakZ1i4ZJM8alKt9GBkjbOLgKkphjCEo9VDuH3YmJkqya3yCsUaCLHY4GXqnadPWdY10xhlLhtxrqmBhfv7oA/c+vOt5mwC33nHHkdffeON+OyTWIuC8M1ZSENqrIdPmilphCWTE8ck5ntp3lFUXnkGt3mT1kj42rljE4yeclIlznkzpr1boqxQR2hAHkqee2oGcn+Q3fvktvOPNN7Bl/Qo0UG8kLFiIizFhFDCRZDy25xA/2r2fx4+McaKZ04wjR3Ps6SXyXgbWGHSLBSVAiMBBgD0ep63HYzQ605Bpb1NqCeKCK3OwqFCx++ARhvq66O6IyfMMKRVxocjekXGOzcwTRQXHDBOCtN5w2PrABZkMHcUylM7ETrRrTGcsAhnKnhRJkmGIiGIaWHYZw+6xUX5w/DgrCkW2dHdz0dAg67s66RaCTDtLqIIQvOKSc7juknO465En+cxXb+Nb3/kBIyemCLdI8jwg6uhDRhUwuVO5k4JGmlNdsZpidYhGogmVZPuJSfd6tz0UhKfDgs1z+qKQ1f09DvmrLbPT0888+OBdJ56t+v9ZSwAhhBVC8OM77pgYH5t4cvmKVdeJKBDrlg8x1N3B0WaKCtq4XoQ0CAFppnl0+05edeEZGGPoDhSXblrFI4cfQyjZEoOgs1ykHMcoo0mNZd2KIW7/zr9y+bkbaViYmU9BWgrFmFzCkxMz3L71Ge7aeZC9szWaQUBQKRN1lv082ntyWSfH0lrcKCvb2MQssxidIqwmRFOSgo44pqcY0xmX6IoiejurPDYyxs6FOqIQo6wky3MeevxpNq4dpqezSpY3GZk4wcHRKQgjDG58KJoNLurroT+Omag3mM9TarmmlqY0BTQDhVUBNlBI7+wiWlYZ9qS6tLQ5eP81yiWawDNZzq4TY3zv+CgrihEXDQxw0eLFrCwWia1modkELNdesJlrLtjMrTdcTU+1itaGMAjIi10EpSrU5px1qxAkRrFqxSbAwSRqwOOHjpELQZglTluoJYImJXmasLxaZFG1Yo02otlIzPjY2NO7du2af7bq/2f1BmhxA0ZPjDzdbNYXgqBaGerrsmuGB8X+rQcoVYrYPAfhtSNz14w+vH0XC8a98AK4/OyN/OMPHnWnicf/C2sJlLM4yozhJVdfigGmFlKsgFIlIhPwyNETfO2BR7l711FGU01QLqPKHU4CxUKWpm0+r5TCkdpbJt9ak9brSJ3RIWC4o8SygW5WdHWyqr+XZdUii8pl+stlKtI5ogXAveOT/PYtP2K+5PSQgjCgoQ2PPXOQKFTkWLSQBJFzZHSydjkrJPzhhecwHIYkQBOYbSZMNhqMpimHZuc5Uq8x0mgy3qgxY6EhJYQByt8Yjmqae+tVZ6hnhZNVpBQzayyPJU0e37ufbxw8zCW9vVyzcgVndnZQAuqNBkIIrr/ifCxOuVsFEhNG7nbxZR7GIAud9A5vdLyMUHJ0oc6u4xMIEWJT7QIfJ+4llcI2E9avGaIqnWx8o9kcP3jo8Dae5Sd4tr/gM7t2PX3JZZeNdlSrlQg4d+Mqvv/jpxHl2IOptMP4S0scx+w4NM6+kSnOWtJHpg1nrV7KcE8HIx5Zmc3OsGr9gFuSIVD+OjbW0lWJaAAPHDjKV+59mLt2H2ZaKKJKlWI5Ai/udJJVJhHKBYjFkCY5IsspC83yUsS65YNsHOpj4+IhllY76CsXKJ/ysylAGbelFm64w+X9vfzyOZv4+KNPIvv6nCG4UBAGjtqpVFveXQqBNBDOzvKOS85nZRhidE4RQUkK+goxKwtOasQMDpAC01oz2mhwqFZn38wMB+fmOFqrMWGhrgIIQ3dISLx+kpeXdKMpN4FTEWPG8s3Rce4+Mcalg/1ct3I553R1UsGSZokzrwgctTOMSoQdQyxM7KezHJPmTapDy6j0LaWRasI4YMfoBMdm5xGdvVjtnUJFS7jMEmQpm4aXEPi2Kc/S2UNHDow87xPg3jvuOPLaG1+7u2/RotUWOPfMVRQC5xxjtPV+AU6IVUnJxGydh5/axZalfdSaGYvKBc5fvYjP3fMk65b08YG3XsdbX3iBA8d5oFUxckT5+w+N8Pm7HuFHz+xnxkqKnZ0U4xBtDcZkbfVnaa0zgtaWZlJD6ZzeSLGut8r5K1Zy9vAS1g1001csEHlAX6oNeSNjruX/6xvkogqJpaMxSuGAe2/eciYzacpnt+0gr1Sc/IpseYl5Xqy1ZPUGnc067zr3bF66YtipPEvZ5kE0jaHh3VPAqVR0CEFXqczGSgUGB5i3lokkYd/0DM9MTfP09AwHGgkLSiHCwHlw+ex0aFHbJueHlTLzBr47OcWDY+Nc3t/HK1at4JyuTgIgN9orRMdceP272Hl/P6NP30nWXGDN8jMQskSmmygCHt13mEYuKObGu9xrwInjmjynP1SsHeolNxBImJ2Z3ffQvfeOPm8ToKUScccdd0xMTkztWLEqfWkQBGxcOcxQdzcnFjKkEk4Px62I/OhPcvvDT/LWl1zm9CaBF5y9lvnpSf7019/K2oEeDG7sqFRAoGD7xAyfuf1BbnlyN9M2ptDRS0l66ZQ2BNYpSIMlayboZIGuSHLBkh4uXb+aC1YMs76/m16nYktNO3xP0jZ1NlglvZeZw7Y4gF6Osk7Uy3jQXpTn/MaF57BhsJ8vP/4kO+bnWVDOAEMEigCoWsPmaoXXX3Ehlwz2I/OWCJgblWoB9VzTtL4b938usUiTYzOHvIykZG0csWnRENctGmJUa3ZMT/Po6ChbJ6Y42GjSFIogjlBh6IGFxsmyGycBE5VL1ITk+7MLPPzIE1zW38OrVq9iU0fFSyJaosoQZ1/3K4yuPouHf/hVOpee6RShJUzlmkf2HYRAOXW8sOUMKrwCeJM1izpY1lm1WhuRNtP86NEj9999992jz2b9/2zfAG2O8JEjRx7feOaZc9VKpbp0oNOuH14ijjy+k1Kl1DbUtl6QqlQq88hTezk8Mc+Krgrzac5LLj6H11x9ITHQzHKkhEgFHJyv87l7HuUbjz3DSCaIq70UW+bMWD+NEQRBiNaa5lydKE/Y0N/Blees5erN69mwaICqXx5pndPI3MelxlLXbunVNojAOnqi923X1mJyQRhIvIgbWHfCCaN5xfKlXL58KU+PjrF/epapphMC7q+UWd/by4buLirQBsxpPzfXAhrasOANBFuKqi1NUokXCwNC/zWbOkVIxWIpWdzXx6V9fYxpzc6pGR44fITHJqc5Xm9go4i4WGgntfHCwwhLWCowZSO+NT3NIw89zKtXr+T65cvpV9JJIFrL4JrLuWZoHciIZjMjjGOeGhll19gUhWK30z1FO8af8T5xSZMLVp1JFTBSMDs/d2LHjqcf4FQHqedrCQTwwF13PXrxpZfs6axWzysBL77kLH708HZ3YuSmrYagrUaFEaNT0/zgnof4zde8mKSeEwYBSTOlaaFcjEiA/3hwK5++42H213KCjg4KJXVyGYV1J2kQYk1OY3aObmF54YohXn7eGVy4biWDXpk4zRyJXkqfispJmwRSkpiUHOPKFu9ZoNtuiO6fdWNQIidSEbLtICkIhJMx6RSCKwcHuGRwgJrvGzpagwLr8CCBUl4xzc39EyGo5Rlp61YRBmlbmkqt0aIhNIYwchpFWoRtnoVBExjBsJIM9/dyRX8vR5pNHj42wl3HjrNrbp6kEKNKccul3Kk7aAfeU+UyJ4zln/cf5uHj47x23Sou7e8jtpYkTQmLvWS5RmuDkoK7tj1DUwTEgSLPLUL7JaGHQPREiovWrPBLQMHk5OT2r3zhC8/8LGLxWU2AVhn0qU996tBb3vGOh5cMLT7PhgHXXLyZ/s99hznTUkN2fFErhJPfbtRo1Jpenk+QGXfdV4oRW4+P84n/vIM7dh9FdHYRdZXdPD7PWj6TjoaIJpmZYVEsedk5a3j1+Wdx5pIBCkBqjdOs8RtK6a9rL13vRqAKKrHbbmqlkN71sQVwlNbBGpQUNLUhNdYp4HnBKGtPWp+mRlM3mlqunQdCGDoTcXFS1FQg2g7raW5IrXOdzL0ynhBOlU0K4Zhx1lBRikhIcmO87ZTb0iohsNKirUHkhtjCuqjA+tWruGHlCp4YG+PWI0d5bGaOhShCFmLHuzZOwzX3xha2o8IjScrOJ7bxgsEBXr9hPSviiDxtuvfXN/MmSdGNJrJDoFTo5BCFp4omKWf297BpoAeMEc0k0aPHRu7/8Y9/PP7TVoH773ADtMqg5qG9B+7buHHjW4Og2rF26aDdsm65+NH2A5Qrjr2lVEieNlGNOT72O+/gna9+EY00c3DpKKApJX9358P843fvZkwrSp09WAxZkiIDb6caOPmsZH6WwVDw8vPW8pZLz+GMfoc+zLUm9XP+SCl0S+XMW1SJFsVPCISxVKSiFoSMpxmEwrvctLyBTdsh3iCo65yiCt2Uw2+EhRfNbf09Cxgp0TjlbOsT3ArbnuVnKBo68+xnh70x1joUOS0NUEsZ6AhDr7kpXankIRV4opFsuc95LrbILb0Krhsa4vKhIR4dn+A7Bw/x6PQM83EBVShgjSW3jnZncueOM1co8q3jo2wdm+DNZ2zghUsWEaVNMDlZYvmN619ETcA3tu5BdQ84PVRrkcIis4SL16ylR0qrrRX1Wu34rqeffsBNys2zWv//zEoggNtvv+2h8y45f1tnpXp5JOFll5/Njx7f5cqFQJEsLDBUsvzNTb/FSy48i5lmClgqxZj9czU+9Pn/5Ifb9xN0dlMoRuR56koT4fThAhWSLCzQJTJu2LKOt112HpsHelxQ+VGgEC0PEuubY6+G3HJ2b9f6fkxqLL2FiJo2zGYWLQWBsASirfXt/qaEhtbUcklFCYT1ShJtK1TbPjGd1649afnkSf5OhU2S5IbU+tbd6nat76RFXH8jckNXsUjg+aUtlxZxildt69+dmqO7aUyLgqg1BQFX9fdxfn8fj0xM8K29B3hkdoZ6qYgIQnfb6JOEeFkocCDL+NgjW3lqdJy3n30GA0GAbiZ0hvDhV7+ENQMD/N1dj5KUulFRgMXQKQ0Xrx1uVwRTE1Pbv/SlLz39s4pD8TP5os59UX7/hz/8wGWXXn5zR6kgd49OiWt+5UPMqxKNuUnOHKry2Q+9l7NWLmG2kWElVOKQW7ft4o/+5esczRWFzqqTOZEtE2mJigKyNCNM6rxw/TC/cs0lXDq82MN1TdtlHQ+ys8K2x64I70wvWhOdttSnm9Mbd2rXgAMLTRIkQWAJsYTCybIr0fr5DGUM/aEhFJbcxF4V2vnjpiYn13WkVBRVicBDio01CKGRIqeZGSa1oiFC32YbctHWwnBjfa3pD0L6oxBpcjCSvC1ke+przkmslZeWbPk6CmPbpozCgwvngTtHRvjKnn0800ywhZIrB1tueca0zbvzuXm2VDv41QvO4ayOMqLpXEDjKOaWXfv50H/eyYQqQRCypSz513e8kmqgbCNJzAP3PXDTtde88C+EEMZa+6zHovxZJIAxRgBm69at98zMzR4zIFYP9pgXnLuemX17uGTNIr71Vx/grJVLqKUp5TigGIf8zS338Oab/569NUOxu5tMO7iCMM5pXlpDY2yUdaHmf735pXzyF1/LZcOLSTNNmmsMlkAposD9kkogpSSUiqi1jPKa/l6LEGEFAYLASXAihaFTwnA5IiJDYglaSzDhx5IWlBDEdoGxbd9mctftRLLhmlchCYSgRIP5XbeTHX2MsKURKgwCDXqaE9u/w8zBe1EiQ6CdBDqgjCGwltCrPXdJQX8YooxBWj99shZ5imSM8N+TwDWpkZQEUmCkQEtn4xT4hZzx/VBRa16+eDEfvuxS3jo8TNf8HHmt5m5J7RdaRjhVjEoHTzSafOju+7nz+Biy4JCySZpww/pV/O3bbuDMsqQxNsZl61bRFSgjQczNzB177InH7m2VP8/7Jvi/YoP+4K//+pEtmzf/U7F40f/s6+ysvvzSs+zYsaPi8x/7Pfo6ijSSlHIcMZlq/uTfvsm/3/0k0bKVyEiSpYl3cLSoICCr1SnmDd5+5bm862VXs7hUoJnl7UmLVIpYCA7ML/DAzkM8fWyM6XoTJSWLuiqcu3IJF6wepi+QNI1xjaO/raSxfpPpIdjG0BsokkLEaJIgAtfEuiTwphZSIOZOwPijNK2gObCSuPcsr6UpacwcJZ5/Bp12kg2uISwuw1qNlIba2DOI8QegvAg1eAZRYajteqO8Mp02mpI19MUlrx6NB+VBYP1ewmrvo+AIQVZIDjQbbB2bZOfULBPNDCmgvxCxqb+Hcwb76Q9Cz16zYHIGhORdG9dx4aIBvvj0Th4am4RSCSu9Mh0usYJCgRPW8vFHn2Biw1pev3Y1RaNJs5SLFw3w1297JR/9929y1erFFpAz8/Nz27Zt+6c/+Ou/fsQ3v/bnpgQ6mQfCrly5svPvP/3p37nooovfWyyVqgZhCxLRSFJKccTeqVne8/df5oGDk5R7e8mNe3OUcHAJpKQ5PcvZi7r4vTffwFVrHRzX5BoVOKXiOAwYT1I+/b17+O7jOzieWNIwxAjnWyaNpmhzNvR28a4XX8gNZ28Ao51fmC/JrTx5O1ivhZMLyfFmQt1YIqEIhXN/FMISKUU8uY3m9v9A2YzChtdRWXqVsxeSgvlj92AO3kJCQOfG1xL/f9p77zC7qnr//7XW2nufNr1m0nuDJIQaIBB6E0RAUGkiIEUsV1QUEAGxF7CCXUQRQRFBQHq5QOgJEJKQ3pPJJJOpp+yy1vr9sfaZxPuz3XtFvX7ZzxN9eIzDnH1W+Xzen3dpmo0xCVLGFNfcj930n5RVC9nd3kuUH+VmDOnMQQtIkoT6IENBeQjj3DGqKIq1dmf7ktY1243h1teX8eC69WzWlrLnYYTnSpk4Jp9ETCrkOGW3KRw7bjSZJEY5WwJiBMLz6AfuXbGa25cuoxOBl82neb5p76UEUim8vl5OGjmMC2bvQQMWncRILyAC64Eo9g/0v/j8c9+86KKLvr5mzZo+a+2ban/4lx7FP/cRPT09lflPP71g3333C4a1tc3OBX6mHIa2kM2IVzZ3ce7XbuGVbSXyTY0kOh5q5jxPOhld7w7OPGgPvnTBacxobyaMXFqkki6EQXmKlT2DXHD9T/jtghWUCvWIfAHl+2R8H993Ki6TybOlFPHYgsVYYP+Jox1FAkeMqwb3uW9KOi8va8n6Pol2Npa+EgRS4AtBzlOo8nbCrYuRwuA3TyKoH5+qoTRh3zLiba9jBeRap6HyHamFuiXctghR6sT4DWQ79kb6dUMnlRLO1SHvedR4vjMH2CVwROzilu34RbAxSbj6iWf4zYat9NbUYrMZpPTwpHSwr+eR+AFbwpj5q9dSjCJ2HzkCL7WdcdJSS2A1M1tbmDG8g63bd7CpbwCZybiUG+Hcoa0Fm8vz+rYdbNqxgxkdw6j3PHSibSCEKBXLgwsWLPzOBe88/6urt6zu4032/vyX7AF2hUXvuOMOtXr16r75z81/slgu7jBGU8hmeHbdRs75ys0sKxqydXUO5UlPOD8ISCohDbrCV859B1848+20BhmiOCGjpKNUpN/bjijhYzfeyvMbd5BvakZojY0idBgSVsqEUQWTOFvzIJtBNLXy7Yef4yfzX3FNcmoZ6KWQp7UpkcKCtJKM1TQFATnpThNfpDW2M3hJQ63VkP2jRTs2kXWBda5eH2LwA2rIxgUZ4CnH35Hpz5YC8kpS43mugd1FbfZHYRUpPLrDWL7w1LM81jOAqq9zaro4wWpDHIbEYejoCkY73XN9Pbe9vpJfvLaERGVIsAjhegeFj4kTZtYW+NyhB3LGhNHkioOuz5COUSuMQccJtraeh7b18dmnnmd9lKA8x88aLJd2zH9u/pOre1b33XHHHeqfufj/6TfA1VdfLT/4wQ+acy86d8qZp7/3spa29r3ymUDOX7NRnPuVn7HZBAT5nJMGpgskCALCwT5mtRS48YOnc8z0iUSJU5Op1KxWCIFJDIHvcdND87n1P1+l0NREEsdIoQiTMsIX5Jtq8AKfSiV0izVFN2Q+z6srVjJz6nhG1RaGgFJRzWwTO7F/AF+mvCKb0rNxGyYubWfHpjdILKjGcWQaxqSxS1Ds30Jx+3piUSDbPhOVGzZkDDC4fSXFvm3YTDuFYTMxwt85ILOWnKfwd4F3pNiJoFZP7MhYikryi2UruX3VBry6enSVX5RoRJJQm8sQKElYqqSljHDMzWyOFRs3M21YC+NqndLOpqQ9Jd3NmxewX8cwOmoKLNu4mRIgPR+tDdI6sb/MZFjfN8jyTVuYMXK4qPE9K4UqtDS31MZR5dUrrrhi29VXXy2ffPJJ+//cBrDWikMPPdSedf75kz94yQe/OGbchHc01OS9Z1ZvEO/74o/pIks2l3f2iakFeeAHlHu6OXJyB9/74BlMbm4g1Nq5nVF1WnNeoUhJVxTzuZ/fTbfxh+gVYRLSOmYY0w6cTduU0QwfN5KWtib6e3qIK5Gr+z1FfyXCmITZU8eTJAlKKaoZNHKXOYFMU068VDSTaIuSkpySZAq11LePpn7kTPItExGyFik9hPDI13dQ3zGF+pF74teNQ4ggnWMoCq0d1LRNpq5jBjJowQo5lMiYkQJfuJAQscuMYgheFoKi1uyIEzYZy08XvsY2oaia6ek4obkmy57TxjN+VCsjhjXSWl9Hf28/lTBysUxKUooTkrDCgWNH4xs95IQt0om1AKSGaY11TG1vYdXmLWwLI5QfuPdvwGoNvs/GgSLLNm5mxujhojmbUTW1tVMmTJw8oqLjV751ww3b/5mbQP2zFr8Qwp570UVTPnjxxV8cO2HSO2prC/LldZvtOdd9X2yKPDKFPGBT+i34nkd5Wxfv2ncK37rwXTRlA5czJmSK1adZY6ljciZQvLRmCzc//DxkMygrCOOIxtFt7H7YPoSBTa3GNbUNBdpbm+lcuwGRQnxYQWmwlzl7TMeXEm3SMkCqoXJDVucPyBRuhMD3GETw8sr1LN26g42VLJvCLOv7NRt7BtjQ08/KbT2s7i6yoRSwpRKwua/Cpr5BNvQOsq6nn1U7QlaXAtaFPit7B9heiaipKZCTElmlC4p0sptSsY2QhMCOKKYnjjC+z+qBQe5eupLEDxylJI6pyfnMmT2FXEbgjPoiags5Wpoa2dq5DZ3aQwrPo1gc5KCxo2gNAhfOl/Ka0i/RIWPWMCKfY4/hw1jf2cXGgUFkEDgPJWMxSYL0PTb1F1m5cROzxo+2DUEgG2vrp0yaMHGkjnn1G9/4+nZrrbj22mv//WFQa62QUtpTzzpr9IXvv/Bz4ydMfkeuJi8Wb9lmz//898TmELK1OUwUOfcF5XQBYdcWLjpqH655zwlIY4i1RiHToDVny2ds1TbaYearN3dRjC1B1YYdy5hpEyiTkOgETyisgb5iiabaAm0jWtm0bAPZTAFhYVvPINt7B+noaKYcx875TFkCIQiqZRDVuZDFV4qnV23k09/7JSu29qE9hecHLjLV81AqwIgU1VcghfMbUkqm6I37OZGOsUJQU1sAYclgmdiY54qTj2Bmc73bfEO0IUFsLaUkoWQSygYSBL5SbOjupRgZjNIuwDuKGDNxFEJoymEZT/qApRSWyedzjBzRxvK1W/CkQihFT6jZ0tvPbvl8OohLB2BCuCC79PZJEs34bJarDp7DV55+gUe3bkcUapyRcGqH7ufyvNTTz9cfnS+uPOog2xR4cuLkSe+4+CMXI3PqciHE8n80FfqfsQGElNJaazPnnHnmeeMmTDwxW5OTa3sH7EVf/L5Y0x9TqK9PxeKpx4ixlDs7+diph/Opk44k0RadQh16iDrgJlh26FpzS3OgXHY+P7jrWPk+XjYgMcnOGOA03SSymlxdTZo4I4aCrq3WeEIQCUlioWIMBmdJ4qeDL2stQkp2VEI+8Y2fs7CrSENTC0KBVQqjJEYqEt9zsajCc426VMRCEA4d5e6XEliUUsS+h5WWBMn8zTv44QNP8q0zT0Sl/kmJtcTWUtGask3QQzyJtBQqh+jYQGAQ2iCNxfednQmoIUduEMQ6IVfIptYuVc6S/KMWVTj+xRCAXiWOSiRxommRkk8ePAeenM/96zpRtTVoYwiUhzEar7aGpzu38+0nnxMfO+QAaixi4oRJ73j/eeehlPqUEGLFP3oT/CNRIJEiKsGv7rzzvFmz97wwn8/5/bGxH//6z8TrW/rI1taTRHrI91NaQ2XrFj7+ziO47KQjqUSJS4W0O4njYihq00GEXhoqJ4DWxnpX0wuJ9NSQy7OXfrEijTE3RiMsRKUIlIeRYIVHID1qM5khS8Sqykt5EpMOBUQ6j5BCsH7bDjb2FKltbMHgnJIjo4lNQmxiwjikEpUIo0EqlSKVcj+VUj9hqZ+w0k9YHiAKB6lERcphkUpUIUk0BkWhvpH13SV6Yj0UqF2t133fI+P7KUvU+ZMqa2nKZauWRM5BDyiXQ6RMBT3C+Q1VOUFROcZqp9sy6Wetq6lJXbtdaVQFBKor1Aow0s0n4iShzlo+eegBHD2ynbi3D5Wa8YJz21N1tdy3agM/fG4BUcZDeZ6YOnXqO9592mkfAWqklP9QTYD3Dyx9EELYr3zl+oP3n7P/x5sam9u1J+zV37pVPLZoNbUtrcQ6cT44qa9OaXsXH3vnkVx68hEUowRfppFEpnrqil0QGkcJThJLPvCJgCXru1BBFlQ68CFm87K1zBw9jD4SdxNYS9bzYSBi67qt+L77+xpLa77A2MY8GWsZiGOyvsAvdVHcvgaRhJTTFHi8OhpGzsJogxEKKxVhHDF29ymMmzUVKTWBr1C+RKqUhFelWpPmgwhH3bbSYqWDQwu5LK88/zqb1/WQ8RWRFW7IJyoMdC5FigghPIywWOmRqx2OkI1gIJcYJjY3UJCSfrdLUb7P5s1bGTe6DaUkiXYHjecprBZs2LQN4XkuB1tIIqF4aVMns+snoqQaYrJWgzCqG2CIwyMlWhtqrOCThx9I8tBTPLKpk0x9vdtkUhDHCV5jI796fRkddbXijN0mWc/6cvr06afd/MtfPnfO6af/Il0r/z4boHqtfexjH5p+3NuP+2RTc8u4jCfs9Xc9Jm558DkK7cOJ4wQ3tTd4nk9x+xYuOv4gLnvXMVRCF2o3xNuUYHUKSqZBbY6jYskGig29g3z6lru597V1+HWNLphNQJDNs239VlY99xrj9pyK9tMNVAx5/dlFVAYrZHJ5pFDosMisKaMZHQRERuMHHnHfBl594FsEpQ34CpTnYxFEXiv7nfZphFBOx2AMSaIhlyWqLaBkhJcR4FkX5CeVM7YS6WaQEqHchlBCpM21R0N9M4XaPInpwreSJB16RQMb2LLgdmr8iqNnSElogOaZtMw4iVjlqVGSaQ11TGrIs6DkrAs9Ienvr/DaqyuZvts4Mp7bglGUsGjxKvrLIUE+n9oXQpLN8IOFi9m6o5cPzplNm6eIteMzaXbGnVYDr6vTYKMT6o3h8iMPonTfYzy7oxe/phZjNUI5QIH6Rn743EImNNWJeR3ttqmxsXXOfvt94MMf+9grQojX/22sEatN78SJE+tOPOU9Hxg9euzhNRnfPvr6CvHVn/+efFsHBuMgTGvwPcng9i5O3n8WV7/vFKLQuapJr2rH5hRjLlop5TNai8GQ9xWPLF7Fp35yDysGQoL6RnSiHaqRZpFlvQwbXl9N94at1LU2oI2mp7MbE2rytXVDJq21cYmT5sxwi9Jqaj2JramnrrGdUt8alJQok4CAMC4DGovAxDEmiiCKKA8WGegfpFzqpJDTKOVEIVYqlEoFNCK9RZRwsy8hUmFOhkK2njhO3O8UJ1hpUwZohNFFkBWnKzACYXwKuQK1gYdOI7pbpeKEKeN55emFiIZ6TJLgKY8Nm7vY3t1DY2MjWEN37wClWJMtOG8NWbUwFIIwX8vtazaydkcvlx06h2m1NcRxlLJnHWVb7HLLYwVCSWxiafIsnzryID5938MsrlSQmUwqDHJKu4F8gW8/+TwT3nG0GJXN2JEjR+578sknX/jgvfdeAQz+OwRkpHoMq752/fXnTdtt99Nrchmxqb9oP3b9z6nkGsj4fmrPZ/A8Samvh/0ndPCVD52FSDRI0prVTTidbaC7iI21Q7ycrK/43kPz+czN95LUt5CprSeJXc6ysdaVGMIDa8gFBZKiZmv/Zje08jw8z3Mp5V5AaesWzjtkNnPHjiBOktSEyyKDOmYdczFbV+1LuXsd1rifn/UawG/C6G3YJMFGIaZSJhkcJCkWiYolpNH4gUBKF4aRKIunPKyUGC0dbymRqLQ+V8bZFiZJjAlDjLEkXoLRGr/QTuPko8j46QhYSrx8G3Vtk9H4Tl4gXHDd2yaP55Glq3hyaw/Z2hq0jgn8LGFi2Ni53eFnfgbP91LLFjEk5rfGYIxBFQrM7x3k0rsf4ROHH8CBHW2YMEyFR5adxVyqbxDC6W7imLGZgE8dPpcr73uCTW5S6KxojEZlsyzpH+Db//kC1xx1EPkgUNOmTnv3ZZd/+nkhxC9SjtD/3RugWvd/7WtfO2Tvvfe+uLmutjEGe+VNvxJLtpeoH9aBMc65DN+jUioyriHLtz9+Lo2+IowSfF86EwQjUrWVow4I6yxQ8CTGU1z1s7v59m8fx29pJ/Ck+4KU6yeU9KkUS4CgUFNwlGE8MiIYEt+LFIEpbu3kpL0n8/GTDsMksYtpQqaolMVQw7DJh0PqB5HK54HASTHDCCPK2DBM/8SYiia0BmMVnmdxbW2aEp9GM1VtRVESLQxGQqQ1Jo4hSTBSYkjQWuPXttOx2wlDrWjqIkpihmLpHWnOGgpCcdkRc+m56yEW7ughX9/g+gHPwyPjDHbTzF8hJWElwvMVUqih2YaOYlQmw9oo5qr7Huejh+3P8eNHY+IYKdwU3gibUlV2zkQ8zyNJEmY11PPhg/bhs48/R6mubmhqrRONX1fLwxu6mLlkhThj+iTb2NjYcvC8eR+57kvXrRZCzH+zbwH5Ztf951500ZRjTzjxqqbW1kkC7E/+MF/cOX8p9a1t6NhJ/YRQCCupNQnfvez9TGtrIkkSfJXW/VWquDBIYbEKZ6YrJaGAS79xC9+863FybcNQSqCjGHSCtAZlBZXtXRy720jOPmg6hWiAqFgiCiPiSBNHhjCMiAcGaYpKfOKEg/jKeadQq4RDo1KCF7YqIhHESYROLEni8m4rYZKKWUgjVp2zgpQeUvrYRBJXDFHJEFYMccUQVyxJRROHmigyhFFCmGgqOiFOf67R2uHoUqKExEvBAbDE2pJoQUVDOYEwMQiqMao7+wtjNZNqC3zttON4x8SR+AN9hIMlojAmjgxRJSGqhJjBIq2lIke2NjLWGMzgoJuSaoO0FrTG8336cgW+9PBT3LZkBbbq6o1xgRcpE1alljNCSnzPR+uEo0cP54zdJmL7+hFKDW1drROSulpufnExr/UOCAR21OgRex973NsvnTdv3rAUNhf/J2+A6dOn17z3jLMuGDly5NyM57Foczdf/8X9BI3NLto0vW49zyfavpXPvP9EDpo4mkqcEEiFkQKTZuq4FJJUpWWcy0Ms4dIbbubXzyyipq2dJNGOeCZd3KYulxGD/Vx+6uH8x8lHkAXmHzCDxxauYMXGTnoGB5FC0lxbYNaEERyzzwymtjaQpDirVB4WpxpzedgJykvtAInSk18QVDlCWBf5JBVIHxlkkDKDjsFEzqHC1fMeyrP4niBRTqugPIHwSLUHBoROc4el0w3bKuyaBoqrBGGdCKd6lrnoqCpXSaa6ZidyGZ/1+dKxh/Dcpk6eWbOJtT199JXKSClpztcypb2Jg8eNZlpzA+sGS3z76Zd4eN0mVF29uyER6MQdOuWaem548kXi2HDOrCmIJKmKLodISTadaDg4W2DjhPftPZPVO3p5sHMHqlBwUU/G0ak3ofjeMwv43NvmUWux48ZNOPaTV1/94Sffc9jnpZTFN4s16r2Zp/83vnPTwZOmTD49n8uqGOyXb/6d2FIy5BqEcxmTEt8PqOzYwZnzZnPB0QcQJ85ZWJudOt0hfruVxMYZ1sZS8dFv3Mxv5i+mtrWNWDvJlrUGTyiiYpEGIr704dN59/4znBuchbnjRzJ3/EgSoKI1UkBO7nRaNqkOYCjKx4IyzoFZyZjBziVsXf4i8WA31iZoY9F+E7OOef8QV8hWFyOu74grGpvEzkXCdwvd+grrSaQvUUqDUY5MV53/KYFMbz5t3fDNGFBSERU3s3bhvdQEqbWhVMhcHfWjZqNyw9FGUvXxlaIquDdkrebwEcM4fMQwikCoXfmSV2ooJEQbw+SaPNcdczAd81/il68uw9TUpQGCzq1DSIVtaeOmF15FCsu5M6cijHZwrBBD8xVrq+4U7ibPG8uH5u7DqrseYkUUI5RCWIGJElQu4KmNndy9eIU4fbdJNpvP52fOnHneD7/yk1ff/95z73izoFHvzVj8Ukp79ElHdxww98D31dTVDRMC++v/XCDumb+IXHMbSRw78bsSxKUy09pquOp9J2LSMbtJ8XAppbtiU5zZpNetDnw++a1buf2pV6lpbSfWzn4P4XQClf5BxtQobrzsEuZOHEE5ivA9RyvWxsF3Ukpq0gA+W3VaEK5JHeI7pzwHm05mS72dPPObG6iNNpL1PaR0kpEwGAb2dPc7JgloDYnBJLHzxa+EJGEFyKB8gfQMNmPRgYeHwVrpLEuwGOEMeQNPuIzfOIYoxihLrBI0Fl3ZQWXjCwR+GSmd23IlUXR3bmTyAWeivNqhE7hKl5NCpYN1BxsXpKSQfn6DdQdOOhk3OiFnDP9xwN6019Tw3fkLKRfyzsdUuGhXC+imRr77wmtkPI/3Tp+I1Q5xM1VEqApepLyh2FjG53N84OB9uOqBp6jUNqSfG0ys0Zkstzz7KnuOGSEm5zK2ob6hbd4hh19+3de+tuHN6gfkm3QDeBec96EzRo8be5zyfTb2F/naT+7CBjl0kqTpjs7m0Cv2cvW572BYTS499WWaIevUWCqlOcv0Kg8Cny/87G5ueeR5altaMUmcBkwKvCCgUiyxe1uO26/7IAdOHEEpjhzPXVq0SHF45U4zM8SfT8uaXU4YscswsqqVDXK1tI7ZHZNtJQpqCb0aYllDIrOAK1N0HGPCMiYskQwUIQzRpTLJYIWkGJKUElf7lxOiiiaKEqLYDfB0CHFoiMOEMEwIjUWHMaZUQpdKVMpll8qCoBwLKsanJHJEsgYbNFLXMhpU4GjL6e0pdvkkVay+iqq5je9yDKqSymrohycFMo44e+ZUrjhifxqi0DW5nkoT3mOM0ej6Br4zfwH3rd2Ir5QjzVX/PbsoHSyuf0m04bBRw3n75PHE/X3phtMYrZHSY32o+dlTC4iUEok2dvjwkbMOOeig97a3txd2RRb/JW+A6g798pe/fOCs2Xu+v5Ar5H0p7A/vfFAs2biN2rZh6Mg5PPiZgIFtXbz/mH05ds+phHHi2IUpjYFUjeXot47iXJvx+N7Dz/Kd3z5GTUsHWqcIkgE/kFT6e5nZnOfWz1zEmKYGKklMIOUQZcIMcVgEO1kwZqcz9J9AcavVrBUCmW1iz+M/xMC29ZQHe7Emcb+byoFoJI56MGGIpYSNKiTlIrocYUONLccO8kzAJhY0KCtIkBiTOKsRpRz0azVKeESxIa5USIolpO8RezGVKCZTN5Jxc99HNuMhvQxKBshsgUxNO5agmtM05PKwq1jGspMzhTDVt+2CSexOAy9rhcvAjCJOnjiWQhDw5SdfYIfynL26cfYoVliKhTq+9vjztB6fZ7/WJrRJXBlo06WfyjSr71MayxlzZjF/7XpWlCpDECzWEuRzPLRsNYdMG8fRY0cgE8voMWOO+fgnP3m7EOKxdI39620Aiyt92qFwwCGHnN7W1jJZCuzrG7eLW37/FLn6RpIodkvNQDRYZGStx0fOPJ44zYytirqHxB3CORfH2pDxFY8sWs41P7oLv3kYVrr/zViLJ6DUvYPd23L89NMXMKapgShJ8JTEE2rI77IqGWQXANEiMCSuTt5lC1hbrXmroW5OHWZEntq26dS0pdm9QKwTDOn/boxDgrTBJhodJyRRjEkMNjZom7g4Ie1iQo2xSO1hPIPwDEparElQnuPV6zjBRKHz2NROySX9eprHzUll+DuvcZ0GDQqJy+5FYqygWjXYtBRxZY9xHJ90sVe3uxBiyDbF9zNoozE64fjRwwkOncN1jz1LdzaPFdLRna3TP2zP5vniY8/y9bcfzoRsxrnJKW8oFsmhec7dTmvNyEzA2fvP5to/PAUNjc6JLvU5LQcBP3/mZfYa2SGarbUtzU2j9p974Gm0tz//926I/34lUHp6XPq1r80ZM2bMMVI4otaPf/MQXQMRnvLSssfV08lAL5e861jG1NdQiZM/qsVtqupyEL3B9wSrunv56Lduo5StR/oKK1yj6iufSnGAKc0ZfnLNhxnd0kRfWHYokVDc+JsH2DpQcsSxqpCjOksYYunJtNFM//3GkFjt5gypJaERkCApa+iOYrqiiG1RxI44YjBKSHblxdjq4E7uLDW0JokSdBhiKzFRJSQuVUiKFZIBVyLFxTJRqUylWE7pGy5h0iYu6FsKQeB7GKBsLH1asz2O2RaF9MYxsXUlnsEF7ek0lTG1p8WkqJs2LrDOmNQH19U/zj0OgUJSRvDixs0YqdJ8M80RI4dx2bx9qS0OOOgzlUHqRGN8j6XlmC898hQ7rEUqmaJkxjXH1fcjHIcrihKOmzyew8ePJOwfpHpnGW3xggyvbO3hocXL8X2Fr3wxZszYY792+eVzqhLVf7UeID392wsHHnzwqXWNDaOVUPbl5RvFbx5+hnxNPTYxSKGQUlIeHGTOtNGcedw8SlGCkl5K4a2eVCndATf5rVj41HdvY3W/0+0aYxFC4XkZkihhdN7nh1d9gHEtDQyUiwghSZTiyu//hkuuv42v/vz3aZ5Y1ctTuNMxPQurduM7w4rTzABAC0loJb3asrUS0R1HJLaCTHrx4m144VZUuJ0UMHWDJSlB+jh9r1vExBqiEBvF2DCBSowphejBMnrQbQIzUMIUQ5JyhElAWA9tBUZIjJBYofCkQqHx4z4yegdZ048QRUoyojOO6IxiBgwkUqYzWucJJFOGrbFgUhc6KXciNCplkbp3K7hz0TIu+OXv+Nkri1MY1hBGIceNGcFHD9yLTP8ASDDS9UhxGCFyWZ7YsoObnn+FSEhi48oqY93mszIVakuHlgXGcNYBe9EqDCaO3MwhtaQU+Ty/e3kxXVEijDE0NDSM3mPPPY9wqkxp/6VKoCpE9cnrP7nfmDFjjs34AVLAj379B7orhvqcTGVyAiktflzkQ2eeS60vKVVipJfi2CaVf4n0ujSWnO/xpd88wv0LVpFvG0YcxU4CmXoC1ekyN155EXsMb6OvEhEoDy/I8PFv/pKb7nqC+jETufF3TzF76njOOnxfoiRGeSqtj53CqhoSQZr4KAEpPcpAT5LQU4moaEtWaoLuZWxe8iRmoBOpI3QSE6sGDnnPJ/E8DyE9UAHSA4ujYEflEFOuYBMPFFgqCCVRvgKlSDyF9CX4ChV4RFZjonRaLARWKazwQGgCT1Hq38QrD/2AvIoRXhbjZ6G+nez4AynmRxKXKjQGAS1Zn9qq9cvQhrYpWGtSoMumlCrHxVJK8eD6TXzrqQUMNI/gyw/PR1rL2bN3J4kjTBJy2uRxbOsf4MaXl+A3NqFj7XTAUYLM1/CrhW8wbVgbx40bSRRrkDZVr7mDB+NuiDhKmNHcwNtnTeUnLy8h0+B0yyAIshlW9vbx9Io1nLbbJCtVIEaOHDHvvPPOG/fjn/xk5d8LEfL+Tse/bW9vL+w/d+5pTY2NowIh7Cvrtoh7n3yZmrpmN83EIBQM9g9w3F5TOXbf3aiEyVA+l6PYps2Su8PJBz6Pv7GOG371ANmmZuIkHnItVEKR9HTx2Q+9k3mTR1GJNBklyfoeV/70bm767cPkWzoIwxKqUMcnv3ELk0YPY86k0VRM5G4ddvpjijSgWypJDHRFMZuKFYrGaZI95SGKfSx/4Cc0DC4j74s0Acai/TawocszFgotPayIsVgqg2UKNbWobC6tuVIPH89NSlEK4UmsL12QhB/gY1H4UNEoLRGJG4IlSQJIkvIOoq6l5L0SVrhycHC1oXtHL03zzmcAj/5yzNZyhdZMwLB8xmVxAUIbTGojaYwZEgbFJiFQPq/09PPZ3z9Ot8wiEouqa+OL9z9DwfN414ypxDrGmITz957J+h193L1mM15tPUYbF29lYVAFfPex55jynuMZnQmcgVaqeCMd1lkh8RWgNafttTuPL1vD+jhOadeO3qEzAb9/dSlHTZsg6oSgra199tvfccrxP/7xj7/5L3MDVHfif3z843uNHjP2mKzvJNu/vOcJuiuW5gYPnSRo4ZiPGRLOO+1YAiAyxknr0lU9BM9pdz53lUM+/Z1bGbA+WZOWJ1Lg+x6l7i4+cuJczj54LypJMlQfX/+bB/nKz+6j0DqCOImcmZVUdGuf86++kd/ccBmT2hrRxji/n6rETyqMgg2VmFW9RbZHCdbzyHoegQTPajwvQ92EvYk3CCqEqU+QAq8JyCFtMRXzOFq3rYSISkh9XSMZX7p62Ghcyql0qI/nIT3fJfz5HsIPEJ6P8ATlbX3YUoiNEvcjpUlRmgxh0Eopo13onfKxNQG1I6YQWQfHWunRqwXdgxXWFsuMKmQZXciRV8pRG6pJkqmuFylZHydcdeeDrC0leDmD0RFIn7Jf4Oo7H6OpUODI8aOIdUJgNP9xyBxWb7uPVwcGUJkMxkn08LyAFQMD/ODJF7n62IPwojQgQKbUEumGZUKC0YYJ+Swn7zGVbz37KrKuDqMTjDbIbIZXu3p5Ye1Gjh4/2tYWCtmp06eeet4HzrtXCPF3uQX+1xsgrcfk7FmzD6qrqR0prGVtb7+4+4kXyaaDDpRAyoBycZAjZk/mkFmTKcca6ashYE5YOUSVTYyhkPX58s//wAsrNlHb3k4SRwjp4Smf8o4eDp08jMvPOI5Ya1dP+h43//5xLv/6reQ7xhPpJJX0GXSsyeTyLNnaxye++lN+/ZVL8VxxiqcUCYJNYcyr2/pZM1Am9gIyQUCgIbQxOSXddFrlaNr3ZIJZh+PpEF9YMgjqMwVQdaA7XR+gIxSWnrUrKe/oSksrPZTs4tQw0jlEeMpl/qYSSaGUu42kYJV0Qz1PWHRYQQhLHIXUtIxnn1M+SYwhkhJtBZEMKKpaemJn8x6bhNBaYqnojjXrtvXRtKOf8XUFJjXUUitUqtRyncIAkmt+cy8vbe3Br6tBxwlSKCoDAwgEfV6eK3/1B1rOfyd7tDYSRhFtQYaPHz2Pj9z6e3rlLrpma/HyOe5fvJIDJ43m+IljMFGSumE78p8bLLtDURjL8btN4p5FK1kbJ0OZDGApC4+7X17CweNHE1hDS0vbrMPnHXPgj2/88cp/+g1Q3YHnnXfe+PETxh+by2aVENjfPvy8WLO9RL6pOfW8V0g8sibhglOOJi8EpfTFiyoB3c1jnIY08Hh5bSc/+u0j1DY1YbXjmgir0eWERlPkCx/8CHkpqMTOnW3d1m4+8ulvYuqHO/MpkwydcNKTJMZS29LOw4vWc9UPf8NXLjyVGMvWMObFLT282j1Iv/CQmQyBhiCMyUrIeZLEWrLWYPHpxifrt1AIBE2+oimToSYdHudyWUxYQcgAYRNIYkpd/UOcGKQY8hi1UoLwhpLgxR9NeNxJqYIMfjaLn3oXKWXJBD5aBuQbx1OxjjFaSSx92jAYJ5QtlIylog2htoSJJtSG0MC2MGFF3zYWd/exT3szE2pyeMZglOSr9z3KQ6s3k61vdv5JVhKVy9jQeQl5QcCmsuWGX9/P9y8+A6kkURyyV0sjFx6yD19+5FlUvTvwbKodjjI5fvLEi+wzegRtKU1FpQ7ZVDUaUqITzZhclqMnj+F7Ly6B2gJGJwitUbmAZzdt5bXOLrHvsDaby+cKkyZNPHz8+PG/k1L+r53l/h4okDjhpJOOa29v20tI6AkTfvXAfLxCnfuA2mVZVYr9HDxrAkftNR2tLYEnUTIVcFvHYDQyzdoSgq//5Nf0hKmFuVOBoICwp4tPnncy04e3UowSkIpSHNHSVM9ppxxNEheR0jobb1O1lBVYFHESk21q5bt3z+cLv3uS+T1lfvjKGh7tHGSryFASHsVQMxBGDEQJA7GlPzIUY0tZC8IwhjCkRcCEvM+4fIZ6BcpatNZMGTOSI/bbjWjNcqLeHUQD/ZhSBV2uoKMYHSbEoSYJE3QYo8shyWAZXa6QlEvEpRJxeZC4XEaHETqsEPb3U+zaTmXDak4+/ABacgFJkuBpQwFLi5I0K8jpmCiK6A0jeuOE/tjQE8X0RhF9YcxgbChqw6AIeKOYcOeKTTy4vpN+KfnFK0u5/bVVZJpaSQxI4ZFUIuJyhE19Vm2UkIkrzJ01HZnmMQspCaOQk2dN45jJYwkHSq4PAhSWTNZnyfZefvvyIpSnEKnhgBDuFkAIp3FK4eNjdp9Me+CRxDq1fXGuer0aHl68kgSnDekYNfKI//jYx+b+PSDR/80NIIQQdvr06U1jxo49LJvPZyXYJxcsEUvWbyVT3+KCr6UBCX5S5py3zyMQENuqiMIgZFXUYtCJJZ/J8LtnF3H/84sptAxzbmbSMSaLPX0ct+/unH3cQQyGCZ5MrTmkw69vuPIiNvYVefjVNWTqGoljV3tqC0KYdKgW4zc08oWf3c/RvZqJe80krBSxiU4xcbBGIrRGegJpJRUdkYthWH2OqfW1tGWD9MXZnZCttQRS8YMvfYLdJo/lkadewJo0oV3g+PdSucjQlKufxE7JpqRyAyrpmP0mZb0aafG8gMaaHMfMexfnvPMY4kTjVT2BUspDxveo8T3qMzHL+oqsHCgxYAWhVERGEicxRhuXF2ZiJFC2gue7Kzy59jUeeHEhps6hOcJCEjmKtKOjGGdL09PNew6ayVlz90THMUru1ElktOa8Q+awYMPv2BJFKE8NARpBfS13LVjM22ZMZmIhNxT8V73pqkYTsTZMrq9h3sSR3LpkHX4+52gaSYLIZHl8xSbOnBuLdk/Z+saGjn3mzDlt/PjxT0sp+/43vYD3v4U+L7rkkv1bW9v31RYSAfc89jyh9cmneLpQkrBcYvaEkRyxz+5uuFRNNkl3rxICaxVCWYqJ5cZbf4/OFBxikmLHJtG05iWXX3AqvjXEMvWEFe6GwWgKnuI7l7+fE//jSyzvjfGCDDpxGQLWVptdgU4sIsjw4P0PYwLFsInjKVXKjnMvPadoUgKZRDQImNxYYEpjgRGFLNk0db2aV+Yka85OPbFQX5Plcx8+i0996Cyshrq/4Q3r/+K0oNJ7PQEqaY5uAFS0C/gQQjghPTuDPDJYhmd8GtsaGFObY8mOfpb3lwiNQqelZZQYtHa3Y+B7rO8Z4OmXXkErHxE7DN5GmrBYSoUt4Pk+UXGAOSOb+Y+3HwZJnGaZibSIlWijmVKb59y5e/HFh57B1jWk4AJ4mQydvSF3L1jCZQfvg9XGzQOGGFdm6LN71nLkblP43eLVVHSS8oQMUirW7Ojl2VXrOGn6RCSSjhHD573vggtmXPWpTz39zxiEibT5DWbMmn1kbWN9h7HWrtreL55Y8AZBLovVCQiLJxS6PMDx8/agxlMYbVIcmlRAIVBGIDQUPI8Hn3uF55ZvJFtTU52gI5Wi1N/H2W+fx94j2wgTjfSko0OkL095HtpoJrTU88OrLqSJCFENkbMWoVM6ROI8cmySQGJ46K4/sGXlWgI/S2Lc4hBhiea4xIHNGU6ZNIyjRjYzsZAlk0r5TPrV2VQ66CgZbljVHyV0lkPHUK1s49VHfsIrD3yPUs86rLVoHWOtpmvtyzx75xd47eEfQHEr0ji/Tt8aNix6lJd/fz1bXvkDgQ4pR5ruYuQGS1Kl1AWRhjmKIdt0aQx5a5mYy3DMiFZOHtvBrIKiEBeJwjKJMYQ6IVGKbX0DPP3CAiKDM1hKEmwlodI/iEycCEYqgU4SRgeWL5z9dob7XjXIjCFbC1y2cBLFnDBjCgeM6kBHodNwS5dSKWtreWDxatYMllBK7KIhJr0pXFkUacOM4a3MaKknKpZdEqd1g9FEejyxZBUVIE4SamoKw2fvscec/y05Tv5PT39rLe86++xRwzqG7ecpj4yS/OcLi1i3tQelhEtit47O25T3OeaAPf6IXkz19ExVfJ4ShMby83seR2fyWOFEJVYqwjBk4rBGzn3H4ZSME9BXU1wcf8UhIUIotNHsP2UsX/vIu6F3K54wbuZrtcPAkyRNV3TDLqkFj93zIL2bOqmVgnZb4qjhtZwzcwxvG9PO6GyAMtp5lFrSea8Yik6qZgQnEnqThO4wYcAKSki6Nq9m3fw7WPf0Lax97Ql3cmuNEIaNi59m49O/5I3Hfsa29YsdwSx9F+tfeYzNz/6Slc/dwWCph0goYqkYjDRlbdGpD4/YxZtIpItIpbbtOQuTcgEnje3g9CmjmdNYIB8WUcYwOFDmuZdepRSbIe2vjRJKPX3YMHaHg9ZIrSkUe/jCWcczs72ZRGs3txFVd+yqHbu7leqB0w/Yg1qb8oBS6oVQHuvLMfe+tjylo1Rv45S3Z91mjg0UBMybNglbDkG7TS20xQ8CXl23mbU9/UIIYfOFgt8xcuSBU6dObUrLH/EPb4KPOvKw2fX19VOscWmLDz/7Ckb6Lp/Xukii4kAve08dy9SRw4i0GXIWk+lwzKYiFE9Jnnp9Bc8sWUe+psYJp9Ns36RU4py3zWN8bYFE61Qks7P9t2kUkOP0K6I44cwj9+NjZx3DYNdGFx5nNBhXDulEIxLHxhRSkkSGh+/8PeOJuOzgGRw/oYMR2QBrrCvZhNzpi5nW9CYVo1sr0Ah6Y83mUkRZKEoaSgYqOiSXV9TkfVymPEMzDyViamuy1OQDjDMhd4RAIONLGmqyZANFMawwGGuKRjNoLd3lkHCIw5qWRNYMFVI2ZVWatD8JrGVaPsepk0fxof2mM6km4KWXFxAmBs/3XFOqDcW+AYidaEcY5yJnt3XyqXfM45ip44iSGOmJnZvNVo/w6sZTmMRw0KgODhoznLgUOqmrdrJHkctz36JlbKiEaXmVerA6H91UTGNJtGHOpDF0ZGVKnQeMK4W3DpZ4cdVG56xhBS0trXucf/7504YcKf5RG0Aq18OPGDF6t0wm12gRbNg+IBYsXYefzaFNklIfNLoyyOH77Y4PxFpjdoYgDtX/1V/i1vuepmQUGPeCpZDoKGFKRxOnH70/xloy0nO2IVXBRyqUr4bGxcY1kqVKyOXnncI5b5vL4LYteNIpq6rc9MQYR2BD4nkZ4orhpz+6jWUrN6CAKI5SOJKdSRN/pBewVSSAsoVNgxFFqygbTckYSjohMonTykqB8v0hJwtw/B6tBML3kV6wC9XdcfSNiUmShIE4olcnDGhL0UCfhu5S5Owh2XVjVol31VM15eFgiJKYPNDZtZ37HnuGKLF4vufoJxqK/YPYyDXAGIuPQG/bwoeO2o/3zduHcujsV5xvcHr7pvkH1bBrkWaY5YFT99qdhiQCndIsjEF5khV9Re5ftNKJY+LElT+AdQFmTvORaEbXFZgxooOoHLqBmXbD0UT4zH9jNSFgtKWmpjBi8tSpe/5jSyBrhTWWqVOnNrW0te4RZDP4nrKLlq9mw9ZtTtuqHR1YxxEtNTkO3GsGYbpDraiyLh3b02qD73m8um4rD7+wlHyhzkGY1tWWcX8/7zlyP0bU5NHG4Anh4FOqebfuKrZmJ8lZSBd4LeKE6y87nxMPnk2pezue76W2ghItHNEMKTBW4wUBq/piTv/0d3nktZUEfkAlSVKEij9SiUl22mqXEKzsL9GjLbGFSmIpJYZBrakkMaBJkgidpJ2DcP/vbG0L2dp6pB9g7E7HaQAZZNwmIKakYwdrak3ZGCLh8PwdoTPQdSew3JlvIIfGbRirnc7C83lk5Xou/O7tLN4+6BwmtAFtKA8MOjvKNHDbs5ZK12bOP2xPPnbykcRxktrPu9vOWON6L5fO7QCDlOlpBITasPeIVvYZ3kpYKrnPZSw6Ckn8gLtfXsyWOMEK5XTO1VI2DSQ01lIQsN/kCcjU06mqJVDZPMs2b6Ozb0D4nrS5bNZvbWubDuT+p5aK/+0NUH2573vf+6a3tLTOrlKM5y9YTJRU+eXu74WlEjMmjWbCyHbCKL3mh5of0lPFnbJ3PPQU24tROh1OE+HDmDHNBd5z9IFoa1M5nlvw1T3AkHJRpD/fyfuMBW0S6jzJz7/0Md5++H7oxCL9LKjADWCsdZizdfBg4PtsKVre95kb+f1Li8kHGWKdYG2K01gzRHUwBmJg3WCZbZEhRFBJNKG1hNYyGGtMUKAYecR+Mx0TZ2GtSfn4kvbxs1ANI7H5VupaRu7sJ6xg/B4HkWRbKGlJ0QjKxlBKNGWtqVgIhWRLsUxfolPh+y4X1RCnXxAa8IMM9y5ZxUXfu4MtWuL5HkkYQaKJBtzJ70nltrTy0KV+Lj7+QK49/e3YJAaTpD2fi2mt2sFbZNp3WKqdkcvlM2QRHLfndLKVsuMIJQk60Sjp88b2Pp5duxkvUCnI4foYO8QsgEQn7DtxNB05n9hYhHIMUj+TYUesWbmtFw/IKEVTS8uMk08+ue1/OhP4H/cAE6ZOnVxbKAzzcCfegiXrEdk8GOcWhqcwUch+MyZQq0RKdZVY45LRLS6T1/M9ukoxf5i/CD+XdeJxLIHyqPTv4ORDZjOqvkCScnYsFpPeIkN7wBinQtIaicH3PbJBgJ/J0lUOWbFlOwfO3ZdcIZ9G/jCUNWZMSjLTFrTFCwJ6RJYLP/8Tfj3/FXJekCYmkjLl3SszUrKlHLK5FKOVj0aQCEGkLbEVVMKE+vaJNO92NLsfeQH1w3cbMgIwQM2wSXhTj6Njzqnkm0YTpwL9RCe0TNiX8XPPpmn344i8BkINkbUkxjqdsDUUrWLTYEiZXSO6xZCyzVpD3vf5zatL+eiPf0uvzOH5PkInKK2J+osk5cj93ZQEWIljRo0axp5z9uTVnn62GTCZLL6fIfA8vLT5FWn/5kwDrKOXWzPk1hEmmv3GDGdqSyNRuei8n7QFNGUheWzRMiJ2mkjIdOEL4W7vxBjG1ufZbeQwojABz0Mqd3uXpMdr67eg0pK3rqZm9LzDDx//DxuEVbk/TfX1IwLP93wl2dq5QyzfsBU/X0hRslTe5iv2m7Wba/9S+EukZE9T9fLMCJ5auITl6zvx65sxcQhSkkTQnIGTjzggTW+XQ4iBTf13qshJxlNUAxG7KprlazezYMlKXl62nmUbt7O+u49ybJG1NVCsYLXd5TYzrj82Bul7rpwIPAaMzwe+eitd5w1wyfEHDTWWIsW/e7Rm7WCFinJyvmpvUzW78q0hn6tn2tsvQlpBosVQmJ2xhgGbIRpzICaXpYTEx7ldIBWJCZiw/8mMQPF81yBxojHKbT0PA0aipGJHkpAvR4zIBWSq4dcpNKuU4ofzX+Fzv32Mcq4OT0is0VgjiYplbGzS8sKVP0kcU9taT6mlgcvufJgaXzG8Ns+klkZmjmhj9+HtjGmso8VTQ4vGGOuId+y80S2GRCc0ZrIcvvskXnnwGUSmwYnxtcHLBLy4egNr+ktMqslhEjMUSC4AZVyDnBew57hRPL62yw0PrXaUdS9gycat9GlNTkqyuXz7uLFjpwOPv+kboDpxG1NfX9fU0jI7m80oAXbJyvWie7CCrGvEJjFS+SRxxKiWJqZNGIVOrzFjq4nu6eTXWkLg/qcWUtHg4yBKgWGwf4BD957I9NHthLFjburYpFaGikC5Xz0Glm3dwYIVG3lm0VIWrOxkdecOBsLEuRwHGXfyZZ0Vi59xY/6q+MIaOXQqx1ojVdpgewEV5XPZ9+5i9YYtfO79p5DzFLHRVIRk/UCJSjqks0kqNsFZiwujafMVbTkn9K9uHGFEmvcOFW3piyXSg9iCSutgKQVCemiT4EuPMfUFurf3E1rf1dIuHYmMdRuqqxyRVR4tgUQaQyAFEZIv3vskNz3xMqa2yc0ujFtA0WAFYje4E9YNpWITkmuqpaatgbLQiHyOfgQ9xYjX+7Zw76rN1CrBqEKBGR2N7Dt+FFPbWxhem6cmzbiPUpOrap+jtWbu5LHc8sSLdEfusEotgNjQH/HsynVM3XMaidEo4YAPIyxSuCZcAnuMGUatMJRSuFwbjZCKVVu30zlYFhPra2wul/WHDRs2K+0Dyv9dbtD/aBL87osuGlXf0DChCly/tnI9kYackM4sVSricpmp40czqrkek8QotbNpRTh2jqckW3vLPPfqMvxs1tEe0rRzEZU4au6eeNowGGsyeZ9s6gLVHScsWbmBZ5eu5pnXV7B4Qzdb+0NiIVF+QJCrJZNPKcnGSRttGrQnpSTI+egwcbaEPoRhlHLVpPPD1G4RI8CrbeLbd89n2drNfO+K99NeX2DTQJGiddCfTekcTtDjiGDKJowqFMgNdWU7Ic7qBFBbp9rS1SZPVHN+3XGo8LDWMiwT0BJ4rKuWAginMkvpJBGS7eUIhU9LoNgexVz+87u58/V1eI3N6MS5b6AtA70DkEBG+ljjgIIwCSk011Db0UBi4iF8HunS421WglAMGsvr5ZhFyzfx6yXrac96TGrMMXPMSHYb3s7EYa20Bk7LkMSGKIoY21jL9JHtPLqui0wuj7UJwgoS6fHs0lW8a89pKOmAEVWNIJRiyJFmQlMdHbUF3ogTpHKHh5CCrmLMiq5eptTXYJWitqFh2oknnthy9913b/jv+gf9jzbA1N12m1DI14wyFrSAJas7QXlD16/rZCJmTBhORkDk3qhLOBep2DENsH5t+SrWbd2OV1OPMQlCgI4NzTUZ5u47E6Ukhbyks1jmuYVLefjZV3hx6Wo2DoT0G4XMFvCzGfy6HJ7YmRtgq6zEqv5XOeOpREckWmO1RQ+WEUKSrSkQxRprBVI4XxwpHTFIJ4ZcXQMPLlzDyR//Ol/81LmMGTcS0TNIIMBLmz+TusclWtMcCNqyPnKI9lHN8UqpUek4QQmZWr64fzZVH4yhzF2DL2B4TZ6NpT6MStGQFHkJpIMOK4lGBFkWbO7i8h/czktb+8m2tKHDEInCxAnl/gF0pPGDHMILkBYGBnsptNZRGNZEIi3CcyWglYCRDlmzxsnrLQ7hC7JgLJujmHWbenhw9RYKAkbXFdh7wmj2HjeK3UcOoz2fowE4YMpYHlu2DvK5FGyw+H7Aki3b2TRQYkpNjkS7sA5ZFQ4jiBNNazbD2LYmXl+zlSCXTR3qLKXYsnRTJ8dOGkliDLU1teOPPPLYyXffffeGN70HsNaK391333DlqZwQgr5Ys2rjVmSQGaLxWmvxpWG/mdOHpr9VmzVpcUZQOkb48MqydZSThFrpBkpSSgb7yxwxdybZXIZfPrWAJ154nedeWczKLT1EMiBTW0empp68H+xMKrFJagDl/n1KSqwU6BjiuExcriB1TFNNhkljWpg1bgwzx4/mqRde5dePLiSobyGxqVWf1enE1pUkOozI19Tx6oZezvz417nyQ2dxzKF70tdXwklVPLS1+MpJOUcX8gQIB/OZXQKkZZook/IAsp4iUMoFVls3ETLCoqxrJpM0XaUp69PoC3q0wfMlAZa8kggMSkjqmgvct2ApX/zhHWyOJYXmZpJKBSUUcVghLFaQGnylnPtFnBD3bOXsI/eiddQwlm7dwtq+EttKZWLpo4IMnh8gvHTqa7SjcWCxcQLG3ZZBNofNZoi0YUWoeeOV5dz+0hI6Cln2GD2Sg6aNZcL4UYxua2FjOUJ4Ds1RUtFZHGT55i52mzLW9YhCgNVpM+BugywwobUJuXwDIpMB7ZwstDWs6ex28wBjCDKZ+saWpmH/E7uU/84GECncmHnmpZdmZ/OFjAC7aXuv2Li91w1WUgJUnES01dUwfeKYVP8hhlAbqimHSBJg0cp1CD+XWmr7CCvI5nKs3VbiHZd8jlWbuki8LEGuQK51BDnJEG1aVGtO5coupKPQap1QqpSwYUhOwOTmWmbvNZY5M6axx6RRTB493Dam+Nf7j9yHmeP+ID77k3tQ+TpndRIlQ7aCxhNI6ZHoBD+foz+JuewLP+CNFYfz4XNOQmIZLIVI6XQDNbkMrbkAq+2Q/tVgqke8u+HYaSfiyWrQdXryVgMn0sbQGEPOc71A2DNITgXkpcCzhnw+C57ie799iJvvfQaRryWbC4iiCF8qotIglcEQTzqLRmUVphzil/v4wvuO5eJj59oYKILY2DPA0s5tvLZ2I0u39bG6t0T3YEwsJSoI8LwgxfR3uV1T7yZP+FhP4vkBRhvWRTGrX1/N7199g9FtLZRU4MJB1E4xfqJ8lm3qQk8eM+RiZ6shKKmoRgMThjXjxyE2yYJ2m08hWd/ZRX+iRc4YlO/n2jo6pltr1dAp/Df2Ad5/swE2J7/nPaMaGhumW+niSFev38SOvgFUbVPKsXEhESM6GuhoaUynr3Kn4VV113qS7cUSi1duIpOrdbm36e/uexmWbehEeIJc6zAQnos0TZEAm04jJQYpPJesGJaJowo5YWmvL7D71BHMmTyKfXefzJRRHba9kPn/bebq8/HTj2VYWzOXfunHDIgMQRCk/YjAxBKrYlfiaeNUabWt3HT7EyxZvoHr/uNMpo9qY0d/CSugNZ/Bx6YOF4KqvddQIF/6nybFsdWQ/0VVM5uWQGlGmhIO8erI5+ivRITWEKBobahhTXcPX7vtfp5YtJqgvsVN3rXzNo0HSsTlEB8HQXseVPp7afcN377sTN6+9zQA4QMNQENjrd29sZZTp42nD9i0o18s3bqNhes2sXBLN6t39DAYJmjPQ2VyeL6XlrIGYbWDQlN7dt/3CQIfYRVryzEop7wzNp1TGCeSem3jFipCEIh0Qp5Orh2HyBBZy+jGGmo9xY4wRqYsXOUptvQPsqMUMSKjbC6Xkx3Dh8074dRTxwkhVl599dXy2muv/fttgKuvvloKIcwRRxwx/CMf+cgnWtva9qxUQtucy9LVM0gclghq60msRUiFiSLGD2+izhNUYoPn3Fmdx4+SeJ4bdj35zFI2dg8Q1Lokl52DCUGQDUBaJ3msOkUIh1dLqdBJQqVUQYcV6nIBMzpa2G/36Rw4fRx7TpvA6KZ66/2ZBV+pVKJyWOk1Rmvfk1k/m2s484h9RXtTAxd8+ltsGOgnV1dHkiTOqyZ2oLP0PRCKxGoKLa089UYn7/n4DVxx7gmcfvQB2ESTFXaIKiCqlASxswwyWNKMSgIv1QtY6+gASOfekDbEQzxha8kJQ3s2oE8bCjUF7p6/gO/+9lE2lTWZ+kbnzADI2BCWihCb6sQCX/mUtm9ncmuO7195AQdMHE05rBBH0Q4DFLK5Jt/3h95RPVDfVGenN9Vx4rQJdCeJWLWth5fWrOeFtZ0s3bKdroEKkfLwgyy+7yOFk/tbU+UkOTN55ftY4Wp/UaWBYFGBx6LOPp7d0MURo9owQKidQ151whxFMR0NDbTVFtjWU3byUeNu5t5yTE+kmViXo1iJbHvHsL0+9rGPXVbu6bnm2muv3fy3agS8v/XkP+KII4Zf+dnPXj1l2vSzCzW1Ga2xdzw6X9x06z34uTzWxOlV7sxGYutR0pq8r4gSTcZzbpXFRPPogkX85tHneejllchC/dAETyAx0n1pIk2CETKt66VCxzHlUhEbhzQVMuw+YQQHzZ7GgbOmMHPCSNsSBLsu+KEvtBJWkkq50tXfP7Chp2fHoq6ursVr1qx5PQzDsLa2tnmf/fc5b9SYsccduedkeed3r7DnXfFN8cqaLmqaGtCpQB9tSEyC9EH6ijgxZGrq2JbEfPQbt/Pi6yv47PtPpb4u71zRRJXzk34eFCbdCAbnIBcISaBceqNzRUvJgmInycwKgTAGkphRhRw9vf185sZbufeFJajaegI/i65E+EoRlyNHZ8bV6O7A8ejv3MRh00fznSsvsFNaG0V/qWg2rl93//PPPvMj6Qd23NgJe9UWCsMamxqn19TWj83lsm3ZXDZQKXG92fNo7Wi1+3S0cvYBsKl/UCxcu5FnV2/i1XVb2NzbTVl62CBLkMkgpefuOp0SHs3O+7+aPiOFoBefT9z6B46dOpq37TWN6aOGUVCO7hwnCX7gKOqkcVju9nemAiGK7//uIbzjDhLThrdaqb3sbrvtfvaVn/2sDYLgaill59+yCcRfW/xSSnvCu04Y/tEPX3b11Om7n1NfXx+s2dptP3Xdd8R9Ty3E5GrJNTYNDUKwEiE9TGWAfce38K0rLmKP8SNYtWU79zz+Ar95/AUWruokxidfXz9UFAyVCdX/kq62j6KQuOIa2Lb6LLMmjmHevrtz0KzJTBk33NYgd1ozpirjchQSR1H/wODAxoG+gdWbN2165o033nj+pZdeWvfTn/50M1DZ9XNefPHF0y78wIVfHD9x0ttrs3mxpmfAfuja74v7nnmZQlO7S59P7ycrQPo+KvAxacCdlIpKfzczR9Zz3fmncNx+MwEcxi0dv8UgSFKaSGIEvXHMYGLJSGgMFFkl8agyVHfSzo1xi9gCv57/Cp+99T6W95QoNFVLTmeBGw6UCctlN8MQrtHEaMKe7bz3qH348ofOtE3ZQAyUB/Sa1evu+f53b7zypptuWrrLOghOP/30Yfvss8+YyZMnz2xtb53e1t62d31tw+QgE9RlszlhcZNqi7VS+cRAZzkUSzZt5pX1W1mwZjPLt+9gRyyxmSx+JthlUp9qJ8wugiprEUajB4vUkLD78GaOnj2Ng3ebzIi6PC+t7eT6ex7l1W0D4GeHFHgYi7KCuNxPA4bTDt6L9x9zoG1WSpRKxcrT//nUZ09829u+IoTQabiG/Z9sACGEsNba3ENPPHbtHrP3/khdbU2wqrPbvvPiq8XS1VvJtbagrRNvi9RPf+jDSUlpRw9TRrcxZ7fRPPLCUjb3hahcnlw+lzII+SOGpfQ8hFToRFMpDyKTCsOb8uyz20QO3WcGc2ZMZMKINpur8jSNFkiF1oZKpRRWKuXunu4dizd3bnl225atSxcuXLj47rvv3vzGG2907/KhMMYIuEZccw1cc801CCHMxRd/ZNo55737U+MmTHxHY0NLXUljr/vuL8Q3b/0DorYJL5NDJ4616CI/QWUChPIwBrxAEYchWV3mrKP24ROnv40xzfXOK9TaNOIoLYGMszaMdYInJTlP4nzkUr1syqlRwkkOV27v5cu/+gN3PPM6Sb4OP5vBCpeDYMKYysAASRSjUiKW8gNMWCGfFLninBP58ClHWA9Ed8/2rtcXvX7zT2+97Qe3/OAHq6rJK0op+ye4NJlLLrlkzJwD58wc3j586vARw/Zram6dlc3lh9cU8spRWQzGGOsp3wWTA8s6u8TTKzYyf/lalnR2s0NLVDbn2LDSIhLj3KCNS9OUWBQSYTSVcgkqFUbX5Jg1tp0Fa7vYGBqCXH6nWbKtbgLhUkWNIRro54RZE/ncGW+zTYEnurq2rnnwDw9ced4559xhrTV/6RYQf23q+/2f/vTUtx17wrfa25qHlRJt3/mh68TDL62k0NxIFIWpq0Hq/IazP1RBkJ5OHsIYSnGFXC6PHwSuLLAOLVLK3xkaZxLKxRI2jhnRmGXf6WM5du6ezJ09hZHNjTZIGe+xMcKTDh4sFculnoHBtdu6tr64ccP659auXbv8rvvuW/rUgw927rrrpZRorasoFv/1RKh+1pEjRzbdeOO3z51z4NxPNDe1tEmwtz/2vPjE129h86AlW9eYSiy1s1kU4AU+Kggcxq88lKco9fUwoTHg46cfy+nHziUPlJLUpkW6wL2k2vymWmKVioRM6pefk4p+Y/nZA0/xrXueYMOgJVtXn+oALJ4URMWQcGDA8fela0ID3yMcGGByS54bPnYmR86aagGxo6en6+mnnvriiSee+D2g8ifKA7HrBkijiYb+eY899mh93/veN33K7tP3Hzl85NzGpqbd8/lcS01NTcEbIodbqmOsfgOLO7eJp5et4Zk31vD61j76kahMxkVfGTdvsVYjdKoPSMU1UblMXKmQzReQfsapCIUgTmIkLk9WKeWyEQRprG4X5x88gytPOsJ61ohNmza/cdstt1xw5ZVXPvWXSiHxFzaGPfHEd4267kvX/mTK5ClHBBL7g3seFxd+9sfUDhtOJSyD0cRJRK6hgZr2VqQnKG7vobi1Bz/9BX1fDnF2TIraCOU5W0DhQtniSpGmvGLvyaM47pB9OWK/GYxrbbTVqIo49ZGJk4RKuVIaGOhb09W17aXVa9c+/cILL7zwtS9+cZVD8/7rKT/0z/Zv7HUskP3973934f4HzL2iuam5DbBvrO/k8ht+Ie59bhlefRNSChLtQrYtFhn4+Lmc8863oJQkqpQxgz0cusdEPn7WCRy0+0Q0EEUaX6aB2OnCEQ7vxGDI+T4J8IeXF/PN3zzEc6u78Oua8ILAITwpSzYqOT6PShtt5XkkcUzS2827Dp7J5z94uh3XUicqcURvz451zz33wtdPOvHEHwohKp/5zGfktddea/4a7F3dAEopa8zOvz5z//3b3n3SSRPGjR07Zbfdph/f1Nw0KxtkazK5bHtNWioZl3ZjAXq15tUNXeLxpSv5z6VrWL69j8hKgmwGJZWjWOsEW8WB0+BwrXUKJUMsNDWtdeTr6sFoijsGiMsJnvPRRChJdqCbH37gNPYfM8ImWotFixbdfvXll3/ogQce2PbnoFHxlxbDbXf85pwjjzri2/X19TWhxr7t4mvFUyu6yORzaJ0QxyGNE8fSMmUiUcpDzyIZXLWWLYuWEsiAoGoBmDL9lPQw2lAuFwmkYfdxwzhu7myOnbsnMyeOriI3QqeIUKUSMlAqbuvp7n1927bOhdu7upY899zLL335y59bWV301QV/DYhrwP5PHQKqPY+1Nnfbbbe9c+7cAz44rGPEvp5SxAZ7w8/vFl+8+T4GbYZsTYE4SlKxvRvletkMnpdxuL9w/KfyQD+1MubUI/flotPexu7DmtDgrE2EdC7UQNbzscD8Fev47q8f4A8vLyfO1pCvrRtKxvGspFysEJbKKKFQ0sWRSuVTGSzS6idcfsYxXPTOw20AohyFbFi/7rEH7n/gyx/5yEeeEEJEf60m/kvv5k9thr322qvl2GOPHdExalT7pEmT9h81atQBjY1NM7K5bFs+n1dyp9DfJkBnFIvnV6znwZdf55ll69hajPHyPr7vQkaqJSPWOpt3K7GBYuSMCeTa69HClUwyEmx5Yw0DW7oJpIdUklLvDt673zQ+/86jrLVGdO/Y0XXXnXece8mFl9z3524B8Rdqf/XEM898de+99/5oIQjsgjWbxFEXfo5BlQfp2IPZlkaGH7gPg0mE0c77H2NozObZ9tKr9KzaQE0uh0wRkSSJiEr9tNYVOHTf6Zxy9MEcvNdutikXDP0uFihHEcVisaunp2fJ5s2b5698441n7rzzztceeOCBLWkl9Een/J8qa/63di/WWq666qp93vWu0y4fN27c0fl8IQ/YJxe+IT51w60898ZmZ/yVKt+qNF7pKfxcBuX5Q5lZVhvKg72MaqzlnUfsw9nHH8yM9uY/mti8vHELP7znCX775AJ6taRQ15D2G+4g0GFMOFhGpy4JSkpXV1tN2NvDETPH8oVL3sVeE0ZaQPQPDvYuXrb0tjtvu/3Gr3/96687G0j7d/HVt9aKa665RlxzzTX/9bAR8+bNa3/nu989Y/zYsdPbOjr2aGtp3bemtn5svlDIS0VqSSltBVi+dYd45OXXeWjhUt7Y2kPsBXi5bFV64W5Saxk+YwJ144dRTIpIKzFCECiPfCRY9fxSGHSDyCiqMKMx4GcfPtPWCiHCKDLPP/fsZ4474ojPS1kt0//KBqjulLe97W0jvvjV62+ZMGnSYRlP2LueWSTefdk38OuasUaT6Ii2vXbHHzeaMK4gjRtwCWvwpcLv7mfdU8+RVz7KE1idMKopz8lH7MtJx8xjxuh2FNiK1kIpRVSJKJWKXTt6epZs2bJ5/soVbzxz/+/vf+2uu+7aXO1538RF/yevfyGEnTdv3rDPfOYzF++5554XNzQ0tAL0liN73U2/Fjfe/hCxX3AJ6w4dcTJXIZCeh5/PIZQL6FC+u9LDUolRTQXedfg+nHPCIXT393PzH57h3mdfo6sUk62pc32Vdji+iRPCYgkTRoBLZxFK4fseleIgTZ7m0tOP5D/edYzNep7QRrN92/aVL7300vePP/74HwJ9u6Sq/N3f164RplIqa+0fLbKaj37ykxP232fO3hMmTz6mo6P9wFy+0J7LZWWSuI0gFWJbOeSZpav47XMLeX59F2UZAJ67WTOKMQfMIM6n1G0jMNIBLTUqR98bm9mxYiOB7xEnCc22xM8+ciaTmpus1olYuHDBdw/df/9L/9zt92fnAB0dHQU/l202QqQpKNVpnevchXXDDFN1NpJuQIFNfekzvtOpGoOSPv29PXz04+/l3GP2owwUK7FVSoooKpd7e3rXbFq/8cFFry58+OGHH170lxb9mxmavOv36nwurRBCdB5++OFfueXWW5YedMBBHxw2rH3/hlxOfv3SM+wBe08TV15/C8vWbabQ2uZw+0SjrEKHIeU4wstmyeTzrsdTikJjE9uM5Wu/e4rbnl5EmGi2lRKyhXoKDbjMM+0sEqJKhbhUwhqbEuictWAShRS3b+GIvSbzhQ++m32mjrOACMPQbNy84Zl7fvf7z1166aWPCiG0MeZNDZr+rz/7jzeEHLzhy19+9QZ4derUqb+74AMfmD1z5p6HjRk3+tiGhsaJhXyhJqokNErJO/acztv2nM5nfvMAP52/lHxNQJKk2Q3KnfrWOIGwMc5xMBbakQzj2FkyGks5iSlFMVZYlK9oaKqfethhh7U+9thjm/4UU9T7Ex8IIQRPPPFE5/mXXLK4o2P4LF951OV9x7Y0zm3NGkF5ey8Nw4dTMRqBN6TYCvws8WBPakDlpnf5wKe1PkuoNeUwtLlsXvR292x+eeGL33nsgQfuveGGG1ZU8fl/0qL/k1+utVYIKYpnn3H2ry6++OJXzzjrrAunTZvynpqGurZTDt7T7rXbBL51y93iZ3c9Ql8sydc1kMTOxhFjSEqubPHzObwgGNK35uub6Ao10stRqJPO/9SA0JaoVCQcLDsoVDpsRQkX2lHu2kprjeITHziZD5x+gi04LZDo6dmx7Y03lv3y57f//Ps3ffOmpdV3+I9+d38OWRJCdF/64Q8/Ajx58cUf+fnceQfOmTVr9hmjx4yeJz3fi8qhrckGYlJ7G9YuxhmICOI4JiyWUbkCFRKwBqWdUZqymlJPn0vlFBaTaHwlyGUyziHEWsqlcNvavrWDQ9P1v2ESbIUQrFy5sn/79u1Lozi0QaDE6OGttikXiO2VCp4foISkd/VGGkaNoKalnnLo0hqzfgavFLNpxRp8z0f6rgSoK2QZO6IdLZQNMhnRs2PHlmeeeuLzp51yyo+A6F9l0f+ZL7RaEi296aabrvzFL372wsGHzvtkW8ewmaOa67n+o2fbU4+eK6779q088NxiVKGWTDaLsS6w29GRB/ECn0w+i8pmnNepIDXxFUitCUsVooEiJorcgEu66bgnJOX+AWQ0yDsP3ZurPvgeZo7psBZEuVLWnVs6n3v66ae/c/bZZ98LDFZLnn+Bd2iri656M0gp45tu+ubSm2765tL3XnDBsx/6wAevmzJ18km+7yshBGPamsng+inPme7RtWIDY+qnYbwAnVTAJBS8LHFXP4Obt+OnNvdxGDJ8eAPDGhusSWKSJLbbt3ctXv3y6j7pTIjt36QJrmLmi1555enBgf61aJgyvIU5u41BDw4iTAImQUQxG55bgN24lbrEUhuD3dLN+meeR/cP4PkefpAhCsvsvdt4Jo0cZgVGFAf6tz/5n0988bRTTvmxlDK6+uqrZbX3EFWV9b/WY4duAyGKZ5753l/+8ue/umTp60tvLxb7ihoj9p8+3t7x7avs9665gIktOYrbu0GD7/ko6VRmuhJS7h2g3NNPXK6gEotKNGH/AP1dPVR6+iFJUClq5gmBLlcob93I3mMb+fX1n+DWr15qZ4zpQGPFjp7urgULX/7u9ddff/7ZZ5/9K8TQ4v+Xe4fV79ZaK9I/8mc/+MEb3/nG9Vcse2P5XUkSJ1obO3tMB1NbaomjCKSHLxXhtgHWPPc6enMfmQGL6o8ZXNPJpldW4BmbipIkSWmQw/eYSpMv8ZUvBvr71y1+5bWnd13Tf/McoDoFfvzpp7+29577fSCX8+zjC5dxwvs/Iyg0uDpMKSdyUYpMTcFpeQedFYYzrtVkfYXq385vv3MlB8+cTH+5bF55ZeH35x1w4GVCiMG/EZP+l/oupZTWGMP+++/fdvmVl5+x516zL2hpaZ3qeT4Kabf0l/nxr/4gfvzbR1nbUyZbV49QyrnLpWxYLcDzvJ1SP+FyAWQqi0wqIUnfDiaPbOKSs07gzJMPs00uIE9UKiWzefPmZ5997oXvnH3GGb8Him9mo/umnSrWSiGEufDCC6df+onLfjhhwvgDFJhbnl8kL73lPjKtregwdg6iOkHbGBX4Tttc0QSqarUuiSoxIwLLbZ+50I4sFAjLpeSll1/8zjGHHHKlEKL85+Bf9Ze6+2uvvTaeOHly77ix4/bPFWrbJo5otaUoEU888SzZuroqudkt9ijBxjG+VKnK3+J7ioHOdXz6gnfaM485SAyUQr1i+bK7fnHzzZ976aWXOo0x4tBDD7X8H3tS2oA4//zzi7f98rYXGhvaF+TyQSVfyI3NZQs1tRlfHLzXNPv2w/YTnq6wbNla+vpLBNkMnuelgXWkDno4d2gp8aSPThKivn6G13h84r1v41tXXWSP2HsaGSVFrLXo6ele8/rrr/3sB9/72TVXXn7ZE0KI+M1IUP9HPNdee6211soTTjiha4/Zs/vaO4bvk68pNE0e2W43besWLy9dRaFQwMQRSoCPQMQaZQVeyoGS0sckFjHQw2fPPYkDxg0n1rFYsXzZvT/60Y8++9qCBdv+kj7gr5LhhBDy+z/90clHHnbM54d1dEzSStpPfvXH4sZf3Iss1DqehkzH0jgjW7BE5RJJfzeffN9J9rMfPlMMlIpmxfJlv73xe9++4ubv37zi/+qX9mcGZwCFW2+99YS999n7nBEjhh9UyNfkq3/t9dWbxDd/cR+/efRFeksJmZo6pJLOE0kplHQ8oqQ4yMjmAmcetz8XvPMoxg1vSRXUMDg4MLhu3bqHH37o4e9ceumlzwDh/8VT/y+sM/XTW29912GHHn5de3v7uO44tpf96NfinpfeIFtX72g0giGWrVMLWSrFErU25oozjuWsg/YyRiM3bd74xu23/+Liqz5x+RN/bZ39VQ1ZCgfK7//0R6ccedgxnx82vGOS8aT9xd2Pim/+9LcsXbc1TRr10p/mwp4nDW/hQ2edYC849ThRLJX00uVv3PX9b377iptv/vdY/H9uIxxwxBHDP3PZx8+aNWPWOU1NTVOCIBgyQ35+2Xrxwzse4HcPP093zyAiX4s1CUQVxo9s5Yzj53LuyUcytr2p+i1TLBYrGzdt+s/XF71+20++/ZMH7n/y/s5/FsLzJteVWGPUT2+55d0HH374dR3DO8b1xcb++A9Pil8+9gJb+kokUjrCpHWkuhpfMWfKKC4+8XDmTBhpTITctrVzxaOPPnzl+e87+7d/jQj3N22AXXao+P6PfnrKEUcd/vm24cMn5ZUyG/uK8vFnX2LRsrVs6uxGG8vItib23n0Sc/edZUc21tI/WDbLly+/66Zvf+OKm2++eUWq1jH8+z1DwzPAu/baq2YffthRp0yYOPHExsbGKZlMJs2IxL66eov45e8f454H59PSWMvpJ87jpKMOYkRDzZC9XxhW7I4d25cvXvzGrV/96ldvfuihhzbs8l38nz/1/0JvpX50yy3vPuzwI65ra20fp3xpVnf3y2deW8riDVvpGSiTzfhMGNbInGmTmTVhhM0CWiO2bu5c8fBDD1x54fnvu1MIYf6egdoibSLk93/001NXrN+wrJgktqwTa63V1lqTpH+s+6PLWtvugX79wsKFvz7nwgsngVOW8W/+7LJAAfyrrr1qn6eefvpLm7dsXlqqFI2xxqYvzWzqK5u+WFffmbXW2nKlZDZv2bz0qaef+tLnPve5vQG/+nN3HTL9mz7Vz6h+dMstZ6zZuGl1KdS2GFsdWWtCa03RWlO21sTVf4617S5VzNLVa1/9/o9+dOouP0O8GTsUQH7zO987ZuHrS+7f1tcfVayxJZ3YUhzbso5tWSc2tNZuHxxMnn/l5TvOOeecSf91Qvj/wvP/2whXXbvPU08/9aXNnRuXliuloQU/tPDDytDCv+qqq/apLvzqRPr/oVdXXWfqxzf//MzVGzevLBtrK8baYhzZUhKnf7QtxcZ29fSWn39pwW1XXHHF3oBM3/nf/L7+2y82/UI48cQTx553ySVnjBk1am5Nfd24fC7XYQWiUipvGegfWLth/cb5v7rl5lt+8YtfrPl3q/n/u2XRLo2yf+211+5x2GGHnDJh4qQT6+rrx0VhVOzt61u2cePGJx5/9NG7rr766leAeJc6/9+x3Pkblpmw1lrvhm/deOQ+B8x5T0db2965mvwIZ6kkTGmw1Lmte9vL69asfurOe+659/abb94ghpyk/xv/ov/F6WYBOXz41MaLLjp97KgJE8YKIcSa1avX33bLLauXL1/eA+j/hxf/X9oI3uc+97k9Z82atVdvb2/nI4/cu/BnP7t9I5D8P77w/+RhC9R8+tOfnjRh8uTxnuepJLF67arl66699tplOCEa//B19l+u+D/3ywveev6m97bL+3rrnf2X98VfWGf/295I/F1+wZ0/y+76hf6/foL9tRvhv26At97X3/y+nE/4W+/sreet563nreet563nreet563nreet563nreet563nreet563nreet563nreet563nreet563nreet563nreetZ9fn/wMd+zu1+iB0VwAAAABJRU5ErkJggg==" alt="" width="96" height="96"><span>منظف الكمبيوتر</span></div>
  <button class="tab on" data-t="1" onclick="tab('1')"><svg viewBox="0 0 24 24"><path d="M12 3a9 9 0 1 0 9 9"/><path d="M12 12l5-5"/><path d="M17 3v4h4"/></svg>تنظيف وتسريع النظام</button>
  <button class="tab" data-t="2" onclick="tab('2')"><svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/></svg>تجهيز الويندوز بعد الفورمات</button>
  <div class="sfoot">
    <div class="swrow"><span>الوضع الداكن</span><button class="sw" id="thm" role="switch" aria-checked="false" aria-label="الوضع الداكن" onclick="toggleTheme()"><i></i></button></div>
    <div class="user"><div class="av"><svg viewBox="0 0 24 24"><path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z"/><path d="M9 12l2 2 4-4"/></svg></div><div><b id="adm"></b><small id="ver"></small></div></div>
  </div>
</aside>
<main>
<div id="upd"><span id="updt"></span><button onclick="doUpdate()">حدّث الآن</button></div>

<section class="pane on" id="p1">
  <div class="head"><div><h1>تنظيف وتسريع النظام</h1><div class="lbl">الأداة بتفحص بس الأماكن المعروفة إنها كاش وملفات مؤقتة.</div></div><button class="ghost" id="qb" onclick="openQ()">المحذوفات المحفوظة</button></div>
  <div class="hero">
    <i class="orb o1"></i><i class="orb o2"></i><i class="orb o3"></i>
    <div>
      <div class="lbl">المساحة اللي هتتحرر</div><div class="big" id="total">جاري الفحص...</div>
      <button class="ghost" onclick="load()">فحص من جديد</button> <button class="pri" id="go" onclick="confirmClean()" disabled>نظّف الآن</button>
    </div>
    <div class="ringw"><div class="ring" id="ring"></div><div class="ringt"><b id="rpct">0%</b><small>من الإجمالي</small></div></div>
  </div>
  <div class="stats" id="stats"></div>
  <div id="list"></div>
</section>

<section class="pane" id="p2">
  <div class="head"><div><h1>تجهيز الويندوز بعد الفورمات</h1><div class="lbl">اختار البرامج والأداة تثبّتها واحد ورا التاني من غير ما تفتح أي نافذة. بتتنزّل من مستودع winget الرسمي، فلازم يكون فيه إنترنت.</div></div></div>
  <div class="warn" id="wg" style="display:none"></div>
  <div class="bar2">
    <button class="ghost" onclick="appsAll(true)">اختار الكل</button>
    <button class="ghost" onclick="appsAll(false)">إلغاء الكل</button>
    <span class="lbl" id="acount"></span>
    <button class="pri" id="inst" onclick="startInstall()" disabled>ثبّت المختار</button>
  </div>
  <div id="prog" style="display:none"><div class="track"><div class="fill" id="fill"></div></div><div class="lbl" id="ptxt"></div></div>
  <div id="apps"></div>
</section>
</main>
</div>
<div id="ov"><div class="modal" id="m"></div></div>
<script>
const $=id=>document.getElementById(id);
const LV={safe:'آمن',caution:'بحذر',risky:'خطر'};
const esc=t=>String(t).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
let S=[],sel=new Set(),dl=[],dsel=new Set(),keepDl=true,ADMIN=false,QD=14,ql=[],qsel=new Set();
let A=[],asel=new Set(),WG=true,busy=false,appsLoaded=false;
const api=()=>window.pywebview.api;
function fmt(n){const u=['B','KB','MB','GB','TB'];let i=0;while(n>=1024&&i<4){n/=1024;i++}return (i?n.toFixed(1):Math.round(n))+' '+u[i]}
const num=n=>'<span class="num">'+fmt(n)+'</span>';
function showM(h){$('m').innerHTML=h;$('ov').style.display='flex'}
function closeM(){$('ov').style.display='none'}

/* ---- theme + tabs ---- */
function curTheme(){const a=document.documentElement.getAttribute('data-theme');if(a)return a;return matchMedia('(prefers-color-scheme:dark)').matches?'dark':'light'}
function themeLabel(){const d=curTheme()==='dark';$('thm').classList.toggle('on',d);$('thm').setAttribute('aria-checked',d?'true':'false')}
function setTheme(t,save){document.documentElement.setAttribute('data-theme',t);themeLabel();if(save)api().set_theme(t)}
function toggleTheme(){setTheme(curTheme()==='dark'?'light':'dark',true)}
function tab(n){
  document.querySelectorAll('.tab').forEach(b=>b.classList.toggle('on',b.dataset.t===n));
  document.querySelectorAll('.pane').forEach(p=>p.classList.toggle('on',p.id==='p'+n));
  if(n==='2'&&!appsLoaded)loadApps();
}

/* ---- cleaning tab ---- */
async function load(){
  $('total').textContent='جاري الفحص...';$('go').disabled=true;$('list').innerHTML='';$('stats').innerHTML='';
  const st=await api().get_state();S=st.sections;ADMIN=st.admin;QD=st.qdays;
  if(st.theme)setTheme(st.theme,false);else themeLabel();
  $('ver').textContent='الإصدار '+st.version;$('adm').textContent=ADMIN?'صلاحية مدير ✓':'بدون صلاحية مدير';
  $('qb').textContent='المحذوفات المحفوظة ('+st.qcount+')';
  sel=new Set();S.forEach((s,i)=>{if(s.level!=='risky'&&s.kind!=='downloads'&&!s.locked&&(s.size>0||s.kind==='recycle'))sel.add(i)});
  render();
}
function render(){
  const g={safe:[0,0],caution:[0,0],risky:[0,0]};
  S.forEach(s=>{if(s.kind==='downloads')return;g[s.level][0]++;g[s.level][1]+=s.size});
  $('stats').innerHTML=['safe','caution','risky'].map(k=>`<div class="stat st-${k}"><div class="lbl">${LV[k]}</div><b>${num(g[k][1])}</b><div class="lbl">${g[k][0]} أقسام</div></div>`).join('');
  $('list').innerHTML=S.map((s,i)=>`<div class="card ${s.level}">
    ${s.kind==='downloads'?'<span class="sp"></span>':`<input type="checkbox" ${sel.has(i)?'checked':''} ${s.locked?'disabled':''} onchange="tog(${i},this.checked)">`}
    <div class="inf"><b>${esc(s.title)}</b><small>${esc(s.desc)}${s.locked?' — يحتاج صلاحية مدير':''}</small></div>
    <span class="badge ${s.level}">${LV[s.level]}</span>
    <div class="sz">${s.kind==='downloads'?'<button class="ghost" onclick="openDl()">اختر الملفات</button>':(s.approx&&s.size?'~':'')+num(s.size)}</div></div>`).join('');
  total();
}
function tog(i,on){on?sel.add(i):sel.delete(i);total()}
function total(){
  let t=0,all=0;sel.forEach(i=>t+=S[i].size);S.forEach(s=>{if(s.kind!=='downloads')all+=s.size});
  $('total').innerHTML=num(t);$('go').disabled=sel.size===0;
  const p=all?Math.min(100,Math.round(t*100/all)):0;
  $('ring').style.setProperty('--p',p);$('rpct').textContent=p+'%';
}
function confirmClean(){
  const items=[...sel].map(i=>S[i]);if(!items.length)return;
  const risky=items.filter(s=>s.level==='risky');
  let h=`<h3>تأكيد التنظيف</h3><ul>${items.map(s=>`<li><span>${esc(s.title)}</span><b>${num(s.size)}</b></li>`).join('')}</ul>`;
  if(risky.length)h+=`<div class="warn">${risky.map(s=>esc(s.desc)).join('<br>')}</div><label class="chk"><input type="checkbox" onchange="$('yes').disabled=!this.checked"> فاهم إن ده مفيهوش رجوع</label>`;
  h+=`<label class="chk"><input type="checkbox" id="rp" ${ADMIN?'checked':'disabled'}> إنشاء نقطة استعادة ويندوز قبل التنظيف${ADMIN?'':' (يحتاج صلاحية مدير)'}</label>`;
  h+=`<div class="row"><button class="pri" id="yes" ${risky.length?'disabled':''} onclick="doClean()">تأكيد ونظّف</button><button class="ghost" onclick="closeM()">إلغاء</button></div>`;
  showM(h);
}
async function doClean(){
  const rp=!!($('rp')&&$('rp').checked);
  showM('<h3>جاري التنظيف...</h3><p>'+(rp?'بنعمل نقطة استعادة الأول، وده ممكن ياخد دقيقة. ':'')+'استنى لحظات، وممكن تظهر نافذة صغيرة من ويندوز.</p>');
  const r=await api().clean([...sel].map(i=>S[i].name),rp);
  let h=`<h3>تم التنظيف ✅</h3><p>اتحرر حوالي <b>${num(r.freed)}</b>.</p>`;
  if(r.restore==='ok')h+='<p>اتعملت نقطة استعادة في ويندوز.</p>';
  if(r.restore==='failed')h+='<p>⚠️ مقدرتش أعمل نقطة استعادة (ممكن اتعملت واحدة النهاردة، أو حماية النظام مقفولة). التنظيف اتم عادي.</p>';
  if(r.windows_tool)h+='<p>أقسام ويندوز اتنضفت بأداته نفسها ومش محسوبة في الرقم ده.</p>';
  if(r.skipped)h+=`<p>${r.skipped} ملف اتخطّى لأنه مستخدم دلوقتي، وده طبيعي.</p>`;
  showM(h+'<div class="row"><button class="pri" onclick="closeM();load()">تمام</button></div>');
}
async function openDl(){
  showM('<h3>جاري تحميل القايمة...</h3>');
  dl=await api().downloads_list();dsel=new Set();drawDl(false);
}
function drawDl(sure){
  if(!dl.length){showM('<h3>مجلد التنزيلات فاضي</h3><div class="row"><button class="ghost" onclick="closeM()">إغلاق</button></div>');return}
  showM(`<h3>مجلد التنزيلات (أكبر العناصر)</h3><ul>${dl.map((d,i)=>`<li><label class="chk" style="margin:0"><input type="checkbox" ${dsel.has(i)?'checked':''} onchange="dtog(${i},this.checked)"> ${d.isdir?'📁 ':''}${esc(d.name)}</label><b>${num(d.size)}</b></li>`).join('')}</ul>
  <label class="chk"><input type="checkbox" ${keepDl?'checked':''} onchange="keepDl=this.checked;drawDl(false)"> احتفظ بنسخة ${QD} يوم عشان أقدر أرجعها (موصى بيه)</label>
  <div class="row"><button class="pri" ${dsel.size?'':'disabled'} onclick="${sure?'doDl()':'drawDl(true)'}">${sure?(keepDl?'متأكد؟ اضغط تاني للنقل':'متأكد؟ اضغط تاني للحذف النهائي'):(keepDl?'احذف المحدد':'حذف نهائي للمحدد')}</button><button class="ghost" onclick="closeM()">إغلاق</button></div>`);
}
function dtog(i,on){on?dsel.add(i):dsel.delete(i);drawDl(false)}
async function doDl(){
  const r=await api().downloads_delete([...dsel],keepDl);
  let h='<h3>تم</h3>';
  if(r.kept)h+=`<p>اتنقل ${r.kept} عنصر (${num(r.kept_size)}) للمحذوفات المحفوظة لمدة ${QD} يوم. تقدر ترجعهم من زرار "المحذوفات المحفوظة" فوق. المساحة هتتحرر بعد ما تتمسح نهائي.</p>`;
  if(r.freed)h+=`<p>اتحرر <b>${num(r.freed)}</b> نهائي.</p>`;
  if(r.failed)h+=`<p>${r.failed} عنصر معرفتش أتعامل معاه (مستخدم دلوقتي، أو على درايف مختلف). مفيش حاجة اتمسحت منهم.</p>`;
  showM(h+'<div class="row"><button class="pri" onclick="closeM();load()">تمام</button></div>');
}
async function openQ(){showM('<h3>جاري التحميل...</h3>');ql=await api().quarantine_list();qsel=new Set();drawQ()}
function drawQ(){
  if(!ql.length){showM('<h3>المحذوفات المحفوظة</h3><p>مفيش حاجة محفوظة دلوقتي.</p><div class="row"><button class="ghost" onclick="closeM();load()">إغلاق</button></div>');return}
  showM(`<h3>المحذوفات المحفوظة</h3><p class="lbl">بتتمسح لوحدها بعد ${QD} يوم.</p><ul>${ql.map((q,i)=>`<li><label class="chk" style="margin:0"><input type="checkbox" ${qsel.has(i)?'checked':''} onchange="qtog(${i},this.checked)"> ${q.isdir?'📁 ':''}${esc(q.name)}</label><span><b>${num(q.size)}</b> · باقي ${q.days_left} يوم</span></li>`).join('')}</ul>
  <div class="row"><button class="pri" ${qsel.size?'':'disabled'} onclick="qRestore()">استرجاع المحدد</button><button class="ghost" ${qsel.size?'':'disabled'} onclick="qPurge()">مسح نهائي</button><button class="ghost" onclick="closeM();load()">إغلاق</button></div>`);
}
function qtog(i,on){on?qsel.add(i):qsel.delete(i);drawQ()}
async function qRestore(){
  const r=await api().quarantine_restore([...qsel].map(i=>ql[i].id));
  ql=await api().quarantine_list();qsel=new Set();drawQ();
  if(r.failed)alert('مقدرتش أرجع '+r.failed+' عنصر');
}
async function qPurge(){
  if(!confirm('المسح ده نهائي ومفيهوش رجوع. متأكد؟'))return;
  await api().quarantine_purge([...qsel].map(i=>ql[i].id));
  ql=await api().quarantine_list();qsel=new Set();drawQ();
}

/* ---- setup tab (winget) ---- */
const STX={wait:['في الانتظار','mut'],run:['جاري التثبيت...','caution'],ok:['اتثبّت ✅','safe'],had:['موجود قبل كده','safe'],fail:['فشل','risky']};
function setSt(i,s,code){
  const e=$('st'+i);if(!e)return;const [t,c]=STX[s];
  e.className='badge '+c+(s==='run'?' pulse':'');
  e.textContent=t+((s==='fail'&&code)?' ('+code+')':'');
}
async function loadApps(){
  const r=await api().get_apps();A=r.apps;WG=r.winget;appsLoaded=true;asel=new Set();
  const w=$('wg');
  if(WG)w.style.display='none';
  else{w.style.display='block';w.textContent='مش لاقي winget على الجهاز ده. محتاج ويندوز 10 (إصدار حديث) أو ويندوز 11، وبرنامج App Installer متحدّث من متجر مايكروسوفت.'}
  renderApps();
}
function renderApps(){
  const gs=[...new Set(A.map(a=>a.group))];
  $('apps').innerHTML=gs.map(g=>{
    const items=A.map((a,i)=>[a,i]).filter(x=>x[0].group===g);
    return `<h3 class="gh">${esc(g)}</h3>${GN[g]?`<div class="warn" style="background:#d9770622;color:var(--warn)">${esc(GN[g])}</div>`:''}<div class="grid">${items.map(([a,i])=>`<label class="card"><input type="checkbox" id="ck${i}" ${asel.has(i)?'checked':''} ${WG?'':'disabled'} onchange="atog(${i},this.checked)"><div class="inf"><b>${esc(a.name)}</b>${a.note?`<small>${esc(a.note)}</small>`:''}</div><span class="badge" id="st${i}"></span></label>`).join('')}</div>`;
  }).join('');
  upd2();
}
function upd2(){$('acount').textContent=asel.size?('المختار: '+asel.size):'';$('inst').disabled=busy||!WG||!asel.size}
const ONE=new Set(['برامج الحماية']);
const GN={'المحاكيات':'المحاكي بيشغّل ألعابك انت، وبعضها (زي PlayStation) بيحتاج ملف BIOS من جهازك الأصلي. الأداة بتثبّت المحاكي بس.','برامج الحماية':'اختار برنامج حماية واحد بس. تثبيت أكتر من واحد بيعمل تعارض وبطء. وويندوز ديفندر بيقفل نفسه لوحده لما برنامج تاني يشتغل.'};
function atog(i,on){
  if(on&&ONE.has(A[i].group))A.forEach((a,j)=>{if(j!==i&&a.group===A[i].group){asel.delete(j);const c=$('ck'+j);if(c)c.checked=false}});
  on?asel.add(i):asel.delete(i);upd2()}
function appsAll(on){if(busy||!WG)return;asel=new Set(on?A.map((_,i)=>i).filter(i=>!ONE.has(A[i].group)):[]);A.forEach((a,i)=>{const c=$('ck'+i);if(c)c.checked=asel.has(i)});upd2()}
function prog(done,total,txt){$('fill').style.width=(total?Math.round(done*100/total):0)+'%';$('ptxt').textContent=txt}
async function startInstall(){
  if(busy||!WG||!asel.size)return;
  const idx=[...asel].sort((a,b)=>a-b);
  busy=true;$('apps').classList.add('lock');$('prog').style.display='block';upd2();
  A.forEach((_,i)=>{$('st'+i).className='badge';$('st'+i).textContent=''});
  idx.forEach(i=>setSt(i,'wait'));
  let done=0,okc=0,bad=0;
  for(const i of idx){
    setSt(i,'run');prog(done,idx.length,'بنثبّت '+A[i].name+' ('+(done+1)+' من '+idx.length+')... ممكن ياخد دقايق');
    let r;try{r=await api().install_app(A[i].key)}catch(e){r={state:'fail',code:'error'}}
    setSt(i,r.state,r.code);
    r.state==='fail'?bad++:okc++;done++;
  }
  prog(done,idx.length,'خلصنا: '+okc+' تمام'+(bad?'، و'+bad+' فشلوا (جنب كل واحد كود الخطأ). جرّب تثبّتهم تاني، ولو فشلوا ابعت الكود للمطوّر.':'. ممكن تحتاج تعيد تشغيل الجهاز.'));
  busy=false;$('apps').classList.remove('lock');upd2();
}

async function doUpdate(){
  $('updt').textContent='جاري التحميل... الأداة هتتقفل وتفتح تاني لوحدها';
  const ok=await api().install_update();if(!ok)$('updt').textContent='فشل التحديث، هنكمل بالنسخة الحالية';
}
themeLabel();
window.addEventListener('pywebviewready',async()=>{
  load();
  const v=await api().find_update();
  if(v){$('upd').style.display='flex';$('updt').textContent='فيه إصدار جديد: '+v}
});
</script></body></html>
"""


def gui_main():
    import webview
    webview.create_window("منظف الكمبيوتر", html=HTML, js_api=Api(), width=1040, height=740, min_size=(820, 560))
    webview.start()


def entry():
    if "--console" in sys.argv:
        main()
        return
    try:
        gui_main()
    except Exception as e:
        if getattr(sys, "frozen", False):
            ctypes.windll.user32.MessageBoxW(
                0, "Could not open the window.\nPlease install 'Microsoft Edge WebView2 Runtime' from Microsoft.",
                "PC Cleaner", 0x10)
        else:
            print(f"GUI could not start ({e}). Using console mode.")
            main()


if __name__ == "__main__":
    try:
        entry()
    except KeyboardInterrupt:
        print("\nStopped.")
