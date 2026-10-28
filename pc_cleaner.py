#!/usr/bin/env python3
"""
PC Cache Cleaner - Windows only - Console version & Modern 3D Webview GUI
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
VERSION = "2.2"
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
                skipped += 1
        for d in sorted(dirs, key=len, reverse=True):
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
    print("  Windows Disk Cleanup is running...")
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
    try:
        with urllib.request.urlopen(RAW_BASE + name, timeout=5) as r:
            return json.loads(r.read(200_000).decode("utf-8"))
    except Exception:
        return None


def ver_tuple(v: str):
    return tuple(int(x) for x in v.split("."))


def find_update():
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
        print(f"Download failed ({e}). Continuing with current version.")
        return False
    if h.hexdigest() != sha:
        print("Fingerprint mismatch. Cancelled.")
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
    subprocess.Popen(["cmd", "/c", bat], creationflags=0x08000000)
    return True


def check_self_update() -> bool:
    found = find_update()
    if not found:
        return False
    new, sha = found
    print(f"\nNew version available: {new} (you have {VERSION}).")
    if input("Download and install it now? (y/n): ").strip().lower() != "y":
        return False
    if install_update(new, sha):
        print("Updating...")
        return True
    return False


def path_is_allowed_for_remote(path: str) -> bool:
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
            return depth >= 2
    return False


def load_remote_sections() -> List[Section]:
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


# --------------------------------------------------------------------- UI Console
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
    print("Scanning...")
    return [(s, *scan(s)) for s in SECTIONS]


def preview(s: Section):
    if s.kind == "recycle":
        print("  Recycle Bin contents managed by Windows.")
        return
    if s.kind == "downloads":
        print(f"  Folder: {s.targets()[0]}")
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


def clean(selected, admin):
    print("\nYou are about to clean:")
    for s, size, _ in selected:
        col, label = LEVEL_LABEL[s.level]
        print(f"  - {s.name} ({human(size)}) [{col}{label}{C_RESET}]")
    if input("\nType YES to confirm: ").strip() != "YES":
        print("Cancelled.")
        return
    total = 0
    cm_keys = []
    for s, _, _ in selected:
        print(f"\n[{s.name}]")
        if s.kind == "files":
            freed, deleted, skipped = delete_files_section(s)
            total += freed
            print(f"  Deleted {deleted} files, freed {human(freed)}, skipped {skipped}.")
        elif s.kind == "recycle":
            print("  Recycle Bin emptied." if empty_recycle() else "  Could not empty.")
        elif s.kind == "cleanmgr":
            if admin:
                cm_keys.append(s.reg_key)
            else:
                print("  Needs Administrator.")
        elif s.kind == "downloads":
            downloads_flow(s)
    if cm_keys:
        print("\n[Windows tools]")
        run_cleanmgr(cm_keys)
    print(f"\nDone. Freed: {human(total)}.")


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
        print("Admin rights required for some features.")
        if input("Restart as Administrator? (y/n): ").strip().lower() == "y":
            args = " ".join(f'"{a}"' for a in sys.argv)
            if ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, args, None, 1) > 32:
                return
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
        elif cmd == "a":
            sel = [r for r in results if r[0].level != "risky" and (r[1] > 0 or r[0].kind == "recycle")]
            if sel:
                clean(sel, admin)
                results = scan_all()
        else:
            nums = parse_numbers(cmd, len(results))
            if nums:
                clean([results[i - 1] for i in nums], admin)
                results = scan_all()


# ------------------------------------------------ restore point & quarantine
QUARANTINE_DAYS = 14
QROOT = os.path.join(env("LOCALAPPDATA"), "PCCleaner", "Quarantine")
QMANIFEST = os.path.join(QROOT, "manifest.json")


def create_restore_point() -> bool:
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
    drive = lambda x: os.path.splitdrive(os.path.abspath(x))[0].lower()
    if drive(path) != drive(QROOT):
        return False
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
APPS = [
    ("chrome", "المتصفحات", "Google Chrome", "Google.Chrome", ""),
    ("firefox", "المتصفحات", "Mozilla Firefox", "Mozilla.Firefox", ""),
    ("brave", "المتصفحات", "Brave", "Brave.Brave", ""),
    ("vlc", "الميديا", "VLC", "VideoLAN.VLC", ""),
    ("klite", "الميديا", "K-Lite Codec Pack (Full)", "CodecGuide.K-LiteCodecPack.Full", ""),
    ("7zip", "الضغط", "7-Zip", "7zip.7zip", ""),
    ("winrar", "الضغط", "WinRAR", "RARLab.WinRAR", "برنامج مدفوع وبيشتغل بفترة تجربة"),
    ("telegram", "التواصل", "Telegram", "Telegram.TelegramDesktop", ""),
    ("whatsapp", "التواصل", "WhatsApp", "WhatsApp.WhatsApp", "من متجر مايكروسوفت"),
    ("discord", "التواصل", "Discord", "Discord.Discord", ""),
    ("foxit", "قارئ PDF", "Foxit PDF Reader", "Foxit.FoxitReader", ""),
    ("adobe", "قارئ PDF", "Adobe Acrobat Reader (64-bit)", "Adobe.Acrobat.Reader.64-bit", ""),
    ("vcredist", "حزم الألعاب", "Visual C++ 2015-2022 (x64)", "Microsoft.VCRedist.2015+.x64", ""),
    ("directx", "حزم الألعاب", "DirectX End-User Runtime", "Microsoft.DirectX", ""),
    ("anydesk", "أدوات", "AnyDesk", "AnyDesk.AnyDesk", ""),
    ("idm", "أدوات", "Internet Download Manager", "Tonec.InternetDownloadManager", "برنامج مدفوع وبيشتغل بفترة تجربة"),
]
APP_BY_KEY = {a[0]: a for a in APPS}
NO_SOURCE_FLAG = {"whatsapp"}
WINGET_ALREADY = {0x8A15002B, 0x8A150061}
WINGET_TIMEOUT = 1800

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
                continue
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


# ------------------------------------------------ Modern 3D & Collapsible UI
HTML = r"""<!doctype html>
<html dir="rtl" lang="ar"><head><meta charset="utf-8">
<style>
:root {
  --bg: #f0f4f9;
  --side: #0f172a;
  --stx: #94a3b8;
  --card: #ffffff;
  --tx: #1e293b;
  --mut: #64748b;
  --line: #e2e8f0;
  --ac: #0d9488;
  --ac-grad: linear-gradient(135deg, #0d9488, #14b8a6);
  --acx: #ffffff;
  --ok: #16a34a;
  --warn: #d97706;
  --bad: #dc2626;
  --sh-3d: 0 10px 25px -5px rgba(15, 23, 42, 0.08), 0 8px 10px -6px rgba(15, 23, 42, 0.04);
  --sh-hover: 0 20px 30px -10px rgba(13, 148, 136, 0.25), 0 10px 15px -5px rgba(0, 0, 0, 0.04);
}

:root[data-theme=dark] {
  --bg: #0b0f17;
  --side: #030712;
  --stx: #64748b;
  --card: #111827;
  --tx: #f3f4f6;
  --mut: #9ca3af;
  --line: #1f2937;
  --ac: #2dd4bf;
  --ac-grad: linear-gradient(135deg, #0d9488, #2dd4bf);
  --acx: #030712;
  --ok: #4ade80;
  --warn: #fbbf24;
  --bad: #f87171;
  --sh-3d: 0 10px 25px -5px rgba(0, 0, 0, 0.5), 0 8px 10px -6px rgba(0, 0, 0, 0.3);
  --sh-hover: 0 20px 30px -10px rgba(45, 212, 191, 0.25), 0 10px 15px -5px rgba(0, 0, 0, 0.5);
}

@media(prefers-color-scheme:dark){
  :root:not([data-theme]){
    --bg: #0b0f17; --side: #030712; --stx: #64748b; --card: #111827;
    --tx: #f3f4f6; --mut: #9ca3af; --line: #1f2937; --ac: #2dd4bf;
    --ac-grad: linear-gradient(135deg, #0d9488, #2dd4bf); --acx: #030712;
    --ok: #4ade80; --warn: #fbbf24; --bad: #f87171;
    --sh-3d: 0 10px 25px -5px rgba(0,0,0,0.5);
    --sh-hover: 0 20px 30px -10px rgba(45, 212, 191, 0.25);
  }
}

* { box-sizing: border-box; }
html, body { height: 100%; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--tx);
  font-family: "Segoe UI", Tahoma, sans-serif;
  font-size: 15px;
  display: flex;
  overflow: hidden;
}

/* ---- Collapsible Sidebar ---- */
aside {
  width: 230px;
  flex: none;
  height: 100vh;
  background: var(--side);
  color: var(--stx);
  display: flex;
  flex-direction: column;
  padding: 18px 12px;
  gap: 8px;
  transition: width 0.3s cubic-bezier(0.4, 0, 0.2, 1);
  position: relative;
  box-shadow: 4px 0 15px rgba(0,0,0,0.15);
  z-index: 10;
}

aside.collapsed {
  width: 68px;
}

.brand-wrapper {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 4px 6px 16px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
  margin-bottom: 8px;
}

.brand {
  font-size: 18px;
  font-weight: 800;
  color: #ffffff;
  white-space: nowrap;
  opacity: 1;
  transition: opacity 0.2s;
  background: linear-gradient(90deg, #38bdf8, #2dd4bf);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
}

aside.collapsed .brand {
  display: none;
}

.toggle-btn {
  background: rgba(255, 255, 255, 0.05);
  border: 1px solid rgba(255, 255, 255, 0.1);
  color: #fff;
  border-radius: 8px;
  width: 34px;
  height: 34px;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  transition: all 0.2s;
}

.toggle-btn:hover {
  background: var(--ac);
  color: var(--acx);
}

.tab {
  display: flex;
  align-items: center;
  gap: 12px;
  width: 100%;
  text-align: right;
  background: transparent;
  color: var(--stx);
  padding: 12px 14px;
  border-radius: 12px;
  font-weight: 500;
  white-space: nowrap;
  position: relative;
  transition: all 0.25s ease;
}

.tab .ico {
  font-size: 18px;
  flex: none;
  width: 24px;
  text-align: center;
}

.tab span.txt {
  transition: opacity 0.2s;
}

aside.collapsed .tab span.txt {
  display: none;
}

aside.collapsed .tab {
  justify-content: center;
  padding: 12px;
}

.tab:hover {
  background: rgba(255, 255, 255, 0.08);
  color: #fff;
  transform: translateY(-2px);
}

.tab.on {
  background: var(--ac-grad);
  color: var(--acx);
  font-weight: 700;
  box-shadow: 0 4px 15px rgba(13, 148, 136, 0.4);
}

.sfoot {
  margin-top: auto;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.sfoot button {
  background: rgba(255, 255, 255, 0.06);
  color: var(--stx);
  text-align: right;
  border: 1px solid rgba(255,255,255,0.05);
}

aside.collapsed .sfoot button {
  text-align: center;
  padding: 10px 0;
}

aside.collapsed .sfoot button span.txt {
  display: none;
}

.sfoot button:hover {
  background: rgba(255, 255, 255, 0.12);
  color: #fff;
}

.sfoot small {
  padding: 0 8px;
  opacity: 0.6;
  font-size: 11px;
  white-space: nowrap;
}

aside.collapsed .sfoot small {
  display: none;
}

/* ---- Main Content Layout ---- */
main {
  flex: 1;
  min-width: 0;
  height: 100vh;
  overflow-y: auto;
  padding: 24px 32px;
}

.pane { display: none; }
.pane.on { display: block; animation: floatUp 0.3s cubic-bezier(0.16, 1, 0.3, 1); }

@keyframes floatUp {
  from { opacity: 0; transform: translateY(12px) scale(0.99); }
  to { opacity: 1; transform: translateY(0) scale(1); }
}

.head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
  margin-bottom: 20px;
}

.head h1 { margin: 0 0 6px; font-size: 24px; font-weight: 800; }
.lbl { color: var(--mut); font-size: 13px; }

/* ---- Hero 3D Card ---- */
.hero {
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: 20px;
  padding: 24px 28px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 16px;
  margin-bottom: 20px;
  box-shadow: var(--sh-3d);
  position: relative;
  overflow: hidden;
  transition: transform 0.3s, box-shadow 0.3s;
}

.hero::before {
  content: "";
  position: absolute;
  top: 0; right: 0; width: 6px; height: 100%;
  background: var(--ac-grad);
}

.big {
  font-size: 36px;
  font-weight: 800;
  background: var(--ac-grad);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  margin-top: 4px;
}

/* ---- Buttons ---- */
button {
  font: inherit;
  border: 0;
  border-radius: 12px;
  padding: 11px 20px;
  cursor: pointer;
  font-weight: 600;
  transition: all 0.2s cubic-bezier(0.34, 1.56, 0.64, 1);
  display: inline-flex;
  align-items: center;
  gap: 8px;
}

button:not(:disabled):hover {
  transform: translateY(-3px) scale(1.02);
}

button:not(:disabled):active {
  transform: translateY(1px) scale(0.98);
}

.pri {
  background: var(--ac-grad);
  color: var(--acx);
  box-shadow: 0 4px 12px rgba(13, 148, 136, 0.3);
}

.pri:not(:disabled):hover {
  box-shadow: var(--sh-hover);
}

.pri:disabled { opacity: 0.5; cursor: not-allowed; transform: none !important; }

.ghost {
  background: var(--card);
  color: var(--tx);
  border: 1px solid var(--line);
  box-shadow: 0 2px 5px rgba(0,0,0,0.03);
}

.ghost:not(:disabled):hover {
  border-color: var(--ac);
  color: var(--ac);
}

.ghost:disabled { opacity: 0.5; cursor: not-allowed; transform: none !important; }

/* ---- 3D Cards Grid ---- */
.card {
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: 16px;
  padding: 16px 20px;
  display: flex;
  align-items: center;
  gap: 16px;
  margin-bottom: 12px;
  box-shadow: var(--sh-3d);
  transition: all 0.25s cubic-bezier(0.16, 1, 0.3, 1);
  position: relative;
}

.card:hover {
  transform: translateY(-4px) scale(1.005);
  box-shadow: var(--sh-hover);
  border-color: var(--ac);
}

.card input[type=checkbox] {
  width: 20px;
  height: 20px;
  accent-color: var(--ac);
  cursor: pointer;
  flex: none;
}

.inf { flex: 1; min-width: 0; }
.inf b { display: block; font-size: 15px; margin-bottom: 2px; }
.inf small { color: var(--mut); font-size: 12px; }

.badge {
  font-size: 12px;
  font-weight: 700;
  padding: 4px 12px;
  border-radius: 30px;
  white-space: nowrap;
}

.badge:empty { display: none; }
.badge.safe { background: rgba(22, 163, 74, 0.12); color: var(--ok); }
.badge.caution { background: rgba(217, 119, 6, 0.12); color: var(--warn); }
.badge.risky { background: rgba(220, 38, 38, 0.12); color: var(--bad); }
.badge.mut { background: var(--line); color: var(--mut); }

.sz { min-width: 100px; text-align: left; font-weight: 700; font-size: 15px; }
.num { direction: ltr; unicode-bidi: isolate; display: inline-block; }
.sp { width: 20px; }

/* ---- Modals & Alerts ---- */
#upd {
  display: none;
  background: var(--ac-grad);
  color: var(--acx);
  border-radius: 16px;
  padding: 14px 20px;
  margin-bottom: 16px;
  justify-content: space-between;
  align-items: center;
  box-shadow: var(--sh-hover);
}

#ov {
  display: none;
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.55);
  backdrop-filter: blur(6px);
  align-items: center;
  justify-content: center;
  padding: 20px;
  z-index: 100;
}

.modal {
  background: var(--card);
  border-radius: 24px;
  padding: 28px;
  max-width: 540px;
  width: 100%;
  max-height: 85vh;
  overflow-y: auto;
  box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.35);
  border: 1px solid var(--line);
  animation: popUp 0.25s cubic-bezier(0.16, 1, 0.3, 1);
}

@keyframes popUp {
  from { opacity: 0; transform: scale(0.95) translateY(10px); }
  to { opacity: 1; transform: scale(1) translateY(0); }
}

.modal h3 { margin-top: 0; font-size: 20px; font-weight: 800; }
.modal ul { list-style: none; padding: 0; margin: 0 0 16px; }
.modal li { display: flex; justify-content: space-between; gap: 10px; padding: 10px 0; border-bottom: 1px solid var(--line); }

.warn {
  background: rgba(220, 38, 38, 0.08);
  color: var(--bad);
  border: 1px solid rgba(220, 38, 38, 0.2);
  border-radius: 12px;
  padding: 12px 16px;
  margin-bottom: 14px;
  font-size: 13px;
}

.chk { display: flex; gap: 10px; align-items: center; margin-bottom: 14px; cursor: pointer; }
.row { display: flex; gap: 10px; justify-content: flex-start; margin-top: 10px; }

.bar2 {
  display: flex; gap: 10px; align-items: center; position: sticky; top: -24px; z-index: 2;
  background: var(--bg); padding: 14px 0; border-bottom: 1px solid var(--line); margin-bottom: 16px;
}

.bar2 .lbl { flex: 1; }
.gh { margin: 20px 0 10px; font-size: 16px; color: var(--mut); font-weight: 700; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 12px; }
.grid .card { margin: 0; cursor: pointer; }

.track { height: 10px; background: var(--line); border-radius: 8px; overflow: hidden; margin: 8px 0; }
.fill { height: 100%; width: 0; background: var(--ac-grad); transition: width 0.3s ease; }
</style></head><body>

<aside id="sidebar">
  <div class="brand-wrapper">
    <div class="brand">منظف الكمبيوتر</div>
    <button class="toggle-btn" onclick="toggleSidebar()" title="طي / فتح القائمة">☰</button>
  </div>
  
  <button class="tab on" data-t="1" onclick="tab('1')">
    <span class="ico">🧹</span><span class="txt">تنظيف النظام</span>
  </button>
  <button class="tab" data-t="2" onclick="tab('2')">
    <span class="ico">💻</span><span class="txt">تجهيز الويندوز</span>
  </button>
  
  <div class="sfoot">
    <button id="thm" onclick="toggleTheme()">
      <span class="ico">🌙</span><span class="txt">الوضع الداكن</span>
    </button>
    <small id="ver"></small>
  </div>
</aside>

<main>
<div id="upd"><span id="updt"></span><button onclick="doUpdate()">حدّث الآن</button></div>

<section class="pane on" id="p1">
  <div class="head">
    <div>
      <h1>تنظيف وتسريع النظام</h1>
      <div class="lbl">تفحص الأداة الأماكن الآمنة للتنظيف والكاش لزيادة سرعة الجهاز.</div>
    </div>
    <button class="ghost" id="qb" onclick="openQ()">📦 المحذوفات المحفوظة</button>
  </div>
  
  <div class="hero">
    <div>
      <div class="lbl">إجمالي المساحة القابلة للتحرير</div>
      <div class="big" id="total">جاري الفحص...</div>
    </div>
    <div>
      <button class="ghost" onclick="load()">🔄 إعادة الفحص</button> 
      <button class="pri" id="go" onclick="confirmClean()" disabled>✨ نظّف الآن</button>
    </div>
  </div>
  
  <div id="list"></div>
</section>

<section class="pane" id="p2">
  <div class="head">
    <div>
      <h1>تجهيز الويندوز بعد الفورمات</h1>
      <div class="lbl">تثبيت البرامج الأساسية تلقائياً وبصمت من مستودع winget الرسمي.</div>
    </div>
  </div>
  <div class="warn" id="wg" style="display:none"></div>
  <div class="bar2">
    <button class="ghost" onclick="appsAll(true)">اختيار الكل</button>
    <button class="ghost" onclick="appsAll(false)">إلغاء الكل</button>
    <span class="lbl" id="acount"></span>
    <button class="pri" id="inst" onclick="startInstall()" disabled>⚡ ثبّت البرامج المختارة</button>
  </div>
  <div id="prog" style="display:none">
    <div class="track"><div class="fill" id="fill"></div></div>
    <div class="lbl" id="ptxt"></div>
  </div>
  <div id="apps"></div>
</section>
</main>

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

/* ---- Collapsible Sidebar ---- */
function toggleSidebar(){
  const sb = $('sidebar');
  sb.classList.toggle('collapsed');
}

/* ---- Theme + Tabs ---- */
function curTheme(){const a=document.documentElement.getAttribute('data-theme');if(a)return a;return matchMedia('(prefers-color-scheme:dark)').matches?'dark':'light'}
function themeLabel(){
  const dark = curTheme()==='dark';
  $('thm').innerHTML = dark ? '<span class="ico">☀️</span><span class="txt">الوضع الفاتح</span>' : '<span class="ico">🌙</span><span class="txt">الوضع الداكن</span>';
}
function setTheme(t,save){document.documentElement.setAttribute('data-theme',t);themeLabel();if(save)api().set_theme(t)}
function toggleTheme(){setTheme(curTheme()==='dark'?'light':'dark',true)}

function tab(n){
  document.querySelectorAll('.tab').forEach(b=>b.classList.toggle('on',b.dataset.t===n));
  document.querySelectorAll('.pane').forEach(p=>p.classList.toggle('on',p.id==='p'+n));
  if(n==='2'&&!appsLoaded)loadApps();
}

/* ---- Cleaning Logic ---- */
async function load(){
  $('total').textContent='جاري الفحص...';$('go').disabled=true;$('list').innerHTML='';
  const st=await api().get_state();S=st.sections;ADMIN=st.admin;QD=st.qdays;
  if(st.theme)setTheme(st.theme,false);else themeLabel();
  $('ver').textContent='الإصدار '+st.version;$('qb').textContent='📦 المحذوفات المحفوظة ('+st.qcount+')';
  sel=new Set();S.forEach((s,i)=>{if(s.level!=='risky'&&s.kind!=='downloads'&&!s.locked&&(s.size>0||s.kind==='recycle'))sel.add(i)});
  render();
}

function render(){
  $('list').innerHTML=S.map((s,i)=>`<div class="card">
    ${s.kind==='downloads'?'<span class="sp"></span>':`<input type="checkbox" ${sel.has(i)?'checked':''} ${s.locked?'disabled':''} onchange="tog(${i},this.checked)">`}
    <div class="inf"><b>${esc(s.title)}</b><small>${esc(s.desc)}${s.locked?' — يحتاج صلاحية مدير':''}</small></div>
    <span class="badge ${s.level}">${LV[s.level]}</span>
    <div class="sz">${s.kind==='downloads'?'<button class="ghost" onclick="openDl()">اختر الملفات</button>':(s.approx&&s.size?'~':'')+num(s.size)}</div></div>`).join('');
  total();
}

function tog(i,on){on?sel.add(i):sel.delete(i);total()}
function total(){
  let t=0;sel.forEach(i=>t+=S[i].size);
  $('total').innerHTML=num(t);$('go').disabled=sel.size===0;
}

function confirmClean(){
  const items=[...sel].map(i=>S[i]);if(!items.length)return;
  const risky=items.filter(s=>s.level==='risky');
  let h=`<h3>تأكيد عملية التنظيف</h3><ul>${items.map(s=>`<li><span>${esc(s.title)}</span><b>${num(s.size)}</b></li>`).join('')}</ul>`;
  if(risky.length)h+=`<div class="warn">${risky.map(s=>esc(s.desc)).join('<br>')}</div><label class="chk"><input type="checkbox" onchange="$('yes').disabled=!this.checked"> فاهم إن العملية دي ملهاش رجوع</label>`;
  h+=`<label class="chk"><input type="checkbox" id="rp" ${ADMIN?'checked':'disabled'}> إنشاء نقطة استعادة للويندوز قبل التنظيف${ADMIN?'':' (يتطلب صلاحية مدير)'}</label>`;
  h+=`<div class="row"><button class="pri" id="yes" ${risky.length?'disabled':''} onclick="doClean()">تأكيد وبدء التنظيف</button><button class="ghost" onclick="closeM()">إلغاء</button></div>`;
  showM(h);
}

async function doClean(){
  const rp=!!($('rp')&&$('rp').checked);
  showM('<h3>جاري التنظيف...</h3><p>'+(rp?'جاري إنشاء نقطة الاستعادة أولاً... ':'')+'يرجى الانتظار لحظات.</p>');
  const r=await api().clean([...sel].map(i=>S[i].name),rp);
  let h=`<h3>تم التنظيف بنجاح ✅</h3><p>تم إفراغ مساحة <b>${num(r.freed)}</b>.</p>`;
  if(r.restore==='ok')h+='<p>تم إنشاء نقطة استعادة النظام.</p>';
  if(r.restore==='failed')h+='<p>⚠️ لم نتمكن من إنشاء نقطة الاستعادة. تمت عملية التنظيف بشكل عادي.</p>';
  if(r.windows_tool)h+='<p>تم تنظيف أقسام النظام بواسطة أداة Windows الرسمية.</p>';
  if(r.skipped)h+=`<p>تم تخطي ${r.skipped} ملف لأنها قيد الاستخدام حالياً.</p>`;
  showM(h+'<div class="row"><button class="pri" onclick="closeM();load()">رائع</button></div>');
}

async function openDl(){
  showM('<h3>جاري التحميل...</h3>');
  dl=await api().downloads_list();dsel=new Set();drawDl(false);
}

function drawDl(sure){
  if(!dl.length){showM('<h3>مجلد التنزيلات فارغ</h3><div class="row"><button class="ghost" onclick="closeM()">إغلاق</button></div>');return}
  showM(`<h3>مجلد التنزيلات (الأكبر حجماً)</h3><ul>${dl.map((d,i)=>`<li><label class="chk" style="margin:0"><input type="checkbox" ${dsel.has(i)?'checked':''} onchange="dtog(${i},this.checked)"> ${d.isdir?'📁 ':''}${esc(d.name)}</label><b>${num(d.size)}</b></li>`).join('')}</ul>
  <label class="chk"><input type="checkbox" ${keepDl?'checked':''} onchange="keepDl=this.checked;drawDl(false)"> الاحتفاظ بنسخة حجر لمدة ${QD} يوم لاسترجاعها لاحقاً (موصى به)</label>
  <div class="row"><button class="pri" ${dsel.size?'':'disabled'} onclick="${sure?'doDl()':'drawDl(true)'}">${sure?(keepDl?'تأكيد النقل للحجر':'تأكيد الحذف النهائي'):(keepDl?'نقل المحدد للحجر':'حذف نهائي للمحدد')}</button><button class="ghost" onclick="closeM()">إغلاق</button></div>`);
}

function dtog(i,on){on?dsel.add(i):dsel.delete(i);drawDl(false)}
async function doDl(){
  const r=await api().downloads_delete([...dsel],keepDl);
  let h='<h3>اكتملت العملية</h3>';
  if(r.kept)h+=`<p>تم نقل ${r.kept} عنصر (${num(r.kept_size)}) إلى المحذوفات المحفوظة.</p>`;
  if(r.freed)h+=`<p>تم حذف <b>${num(r.freed)}</b> نهائياً.</p>`;
  showM(h+'<div class="row"><button class="pri" onclick="closeM();load()">حسناً</button></div>');
}

async function openQ(){showM('<h3>جاري تحميل القائمة...</h3>');ql=await api().quarantine_list();qsel=new Set();drawQ()}
function drawQ(){
  if(!ql.length){showM('<h3>المحذوفات المحفوظة</h3><p>لا توجد عناصر محفوظة حالياً.</p><div class="row"><button class="ghost" onclick="closeM();load()">إغلاق</button></div>');return}
  showM(`<h3>المحذوفات المحفوظة</h3><p class="lbl">تُحذف تلقائياً بعد ${QD} يوم.</p><ul>${ql.map((q,i)=>`<li><label class="chk" style="margin:0"><input type="checkbox" ${qsel.has(i)?'checked':''} onchange="qtog(${i},this.checked)"> ${q.isdir?'📁 ':''}${esc(q.name)}</label><span><b>${num(q.size)}</b> · باقي ${q.days_left} يوم</span></li>`).join('')}</ul>
  <div class="row"><button class="pri" ${qsel.size?'':'disabled'} onclick="qRestore()">استرجاع المحدد</button><button class="ghost" ${qsel.size?'':'disabled'} onclick="qPurge()">مسح نهائي</button><button class="ghost" onclick="closeM();load()">إغلاق</button></div>`);
}

function qtog(i,on){on?qsel.add(i):qsel.delete(i);drawQ()}
async function qRestore(){
  const r=await api().quarantine_restore([...qsel].map(i=>ql[i].id));
  ql=await api().quarantine_list();qsel=new Set();drawQ();
}

async function qPurge(){
  if(!confirm('سيتم حذف العناصر نهائياً. هل أنت متأكد؟'))return;
  await api().quarantine_purge([...qsel].map(i=>ql[i].id));
  ql=await api().quarantine_list();qsel=new Set();drawQ();
}

/* ---- Winget App Installer Logic ---- */
const STX={wait:['في الانتظار','mut'],run:['جاري التثبيت...','caution'],ok:['تم التثبيت ✅','safe'],had:['مثبت سابقاً','safe'],fail:['فشل','risky']};
function setSt(i,s,code){
  const e=$('st'+i);if(!e)return;const [t,c]=STX[s];
  e.className='badge '+c;
  e.textContent=t+((s==='fail'&&code)?' ('+code+')':'');
}

async function loadApps(){
  const r=await api().get_apps();A=r.apps;WG=r.winget;appsLoaded=true;asel=new Set();
  const w=$('wg');
  if(WG)w.style.display='none';
  else{w.style.display='block';w.textContent='أداة winget غير متاحة على نظامك.'}
  renderApps();
}

function renderApps(){
  const gs=[...new Set(A.map(a=>a.group))];
  $('apps').innerHTML=gs.map(g=>{
    const items=A.map((a,i)=>[a,i]).filter(x=>x[0].group===g);
    return `<h3 class="gh">${esc(g)}</h3><div class="grid">${items.map(([a,i])=>`<label class="card"><input type="checkbox" ${asel.has(i)?'checked':''} ${WG?'':'disabled'} onchange="atog(${i},this.checked)"><div class="inf"><b>${esc(a.name)}</b>${a.note?`<small>${esc(a.note)}</small>`:''}</div><span class="badge" id="st${i}"></span></label>`).join('')}</div>`;
  }).join('');
  upd2();
}

function upd2(){$('acount').textContent=asel.size?('المحدد: '+asel.size):'';$('inst').disabled=busy||!WG||!asel.size}
function atog(i,on){on?asel.add(i):asel.delete(i);upd2()}
function appsAll(on){if(busy||!WG)return;asel=new Set(on?A.map((_,i)=>i):[]);document.querySelectorAll('#apps input').forEach(c=>c.checked=on);upd2()}
function prog(done,total,txt){$('fill').style.width=(total?Math.round(done*100/total):0)+'%';$('ptxt').textContent=txt}

async function startInstall(){
  if(busy||!WG||!asel.size)return;
  const idx=[...asel].sort((a,b)=>a-b);
  busy=true;$('apps').classList.add('lock');$('prog').style.display='block';upd2();
  A.forEach((_,i)=>{$('st'+i).className='badge';$('st'+i).textContent=''});
  idx.forEach(i=>setSt(i,'wait'));
  let done=0,okc=0,bad=0;
  for(const i of idx){
    setSt(i,'run');prog(done,idx.length,'جاري تثبيت '+A[i].name+' ('+(done+1)+'/'+idx.length+')...');
    let r;try{r=await api().install_app(A[i].key)}catch(e){r={state:'fail',code:'error'}}
    setSt(i,r.state,r.code);
    r.state==='fail'?bad++:okc++;done++;
  }
  prog(done,idx.length,'اكتمل التثبيت: '+okc+' تم بنجاح'+(bad?'، و '+bad+' فشلوا.':''));
  busy=false;$('apps').classList.remove('lock');upd2();
}

async function doUpdate(){
  $('updt').textContent='جاري التحديث...';
  const ok=await api().install_update();if(!ok)$('updt').textContent='فشل التحديث.';
}

themeLabel();
window.addEventListener('pywebviewready',async()=>{
  load();
  const v=await api().find_update();
  if(v){$('upd').style.display='flex';$('updt').textContent='إصدار جديد متاح: '+v}
});
</script></body></html>
"""


def gui_main():
    import webview
    webview.create_window("منظف الكمبيوتر", html=HTML, js_api=Api(), width=1100, height=760, min_size=(860, 580))
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
                0, "Could not open window.\nPlease install 'Microsoft Edge WebView2 Runtime'.",
                "PC Cleaner", 0x10)
        else:
            print(f"GUI Error ({e}). Starting console mode.")
            main()


if __name__ == "__main__":
    try:
        entry()
    except KeyboardInterrupt:
        print("\nStopped.")