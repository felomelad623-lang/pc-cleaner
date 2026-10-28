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
if sys.stdout:
    os.system("")  # enables ANSI colors in the Windows console

REPARSE_POINT = 0x400  # junctions / symlinks: we never enter them
FORBIDDEN = ("\\windows\\prefetch", "\\windows\\winsxs", "pagefile.sys", "hiberfil.sys")

# ---- Update settings: put YOUR GitHub username and repository name here ----
VERSION = "2.0"
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
        return {"version": VERSION, "sections": out}

    def clean(self, names):
        names = set(n for n in names if isinstance(n, str))
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
        return {"freed": freed, "skipped": skipped, "windows_tool": bool(cm)}

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

    def downloads_delete(self, idxs):
        freed = failed = 0
        for i in idxs:
            if not isinstance(i, int) or not (0 <= i < len(self.dl_items)):
                continue
            p, sz, d = self.dl_items[i]
            try:
                shutil.rmtree(p) if d else os.remove(p)
                freed += sz
            except OSError:
                failed += 1
        self.dl_items = []
        return {"freed": freed, "failed": failed}

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
:root{--bg:#f4f6f9;--card:#fff;--tx:#1b2430;--mut:#6b7686;--line:#e4e8ee;--ac:#2563eb;--ok:#16a34a;--warn:#d97706;--bad:#dc2626}
@media(prefers-color-scheme:dark){:root{--bg:#10151c;--card:#18202a;--tx:#e8edf3;--mut:#93a0b1;--line:#263140;--ac:#4f8cff}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);font-family:"Segoe UI",Tahoma,sans-serif;padding:22px;font-size:15px}
.top{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px}
.top h1{margin:0;font-size:22px}.top small{color:var(--mut)}
.hero{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:20px;display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:14px}
.lbl{color:var(--mut);font-size:13px}.big{font-size:32px;font-weight:700;color:var(--ac)}
button{font:inherit;border:0;border-radius:10px;padding:10px 18px;cursor:pointer}
.pri{background:var(--ac);color:#fff}.pri:disabled{opacity:.45;cursor:default}
.ghost{background:transparent;color:var(--tx);border:1px solid var(--line)}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px;display:flex;align-items:center;gap:12px;margin-bottom:8px}
.card input{width:18px;height:18px;accent-color:var(--ac)}
.inf{flex:1;min-width:0}.inf b{display:block}.inf small{color:var(--mut)}
.badge{font-size:12px;padding:3px 10px;border-radius:20px;white-space:nowrap}
.badge.safe{background:#16a34a22;color:var(--ok)}.badge.caution{background:#d9770622;color:var(--warn)}.badge.risky{background:#dc262622;color:var(--bad)}
.sz{min-width:92px;text-align:left;font-weight:600}.num{direction:ltr;unicode-bidi:isolate;display:inline-block}
.sp{width:18px}
#upd{display:none;background:var(--ac);color:#fff;border-radius:12px;padding:10px 14px;margin-bottom:12px;justify-content:space-between;align-items:center}
#upd button{background:#fff;color:var(--ac)}
#ov{display:none;position:fixed;inset:0;background:#0007;align-items:center;justify-content:center;padding:20px}
.modal{background:var(--card);border-radius:16px;padding:20px;max-width:520px;width:100%;max-height:85vh;overflow:auto}
.modal h3{margin-top:0}.modal ul{list-style:none;padding:0;margin:0 0 12px}
.modal li{display:flex;justify-content:space-between;gap:10px;padding:7px 0;border-bottom:1px solid var(--line)}
.warn{background:#dc262615;color:var(--bad);border-radius:10px;padding:10px;margin-bottom:10px;font-size:14px}
.chk{display:flex;gap:8px;align-items:center;margin-bottom:12px}
.row{display:flex;gap:8px;justify-content:flex-start}
</style></head><body>
<div class="top"><h1>منظف الكاش</h1><small id="ver"></small></div>
<div id="upd"><span id="updt"></span><button onclick="doUpdate()">حدّث الآن</button></div>
<div class="hero">
  <div><div class="lbl">المساحة اللي هتتحرر</div><div class="big" id="total">جاري الفحص...</div></div>
  <div><button class="ghost" onclick="load()">فحص من جديد</button> <button class="pri" id="go" onclick="confirmClean()" disabled>نظّف الآن</button></div>
</div>
<div id="list"></div>
<div id="ov"><div class="modal" id="m"></div></div>
<script>
const $=id=>document.getElementById(id);
const LV={safe:'آمن',caution:'بحذر',risky:'خطر'};
let S=[],sel=new Set(),dl=[],dsel=new Set();
const api=()=>window.pywebview.api;
function fmt(n){const u=['B','KB','MB','GB','TB'];let i=0;while(n>=1024&&i<4){n/=1024;i++}return (i?n.toFixed(1):Math.round(n))+' '+u[i]}
const num=n=>'<span class="num">'+fmt(n)+'</span>';
function showM(h){$('m').innerHTML=h;$('ov').style.display='flex'}
function closeM(){$('ov').style.display='none'}
async function load(){
  $('total').textContent='جاري الفحص...';$('go').disabled=true;$('list').innerHTML='';
  const st=await api().get_state();S=st.sections;$('ver').textContent='الإصدار '+st.version;
  sel=new Set();S.forEach((s,i)=>{if(s.level!=='risky'&&s.kind!=='downloads'&&!s.locked&&(s.size>0||s.kind==='recycle'))sel.add(i)});
  render();
}
function render(){
  $('list').innerHTML=S.map((s,i)=>`<div class="card">
    ${s.kind==='downloads'?'<span class="sp"></span>':`<input type="checkbox" ${sel.has(i)?'checked':''} ${s.locked?'disabled':''} onchange="tog(${i},this.checked)">`}
    <div class="inf"><b>${s.title}</b><small>${s.desc}${s.locked?' — يحتاج صلاحية مدير':''}</small></div>
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
  let h=`<h3>تأكيد التنظيف</h3><ul>${items.map(s=>`<li><span>${s.title}</span><b>${num(s.size)}</b></li>`).join('')}</ul>`;
  if(risky.length)h+=`<div class="warn">${risky.map(s=>s.desc).join('<br>')}</div><label class="chk"><input type="checkbox" onchange="$('yes').disabled=!this.checked"> فاهم إن ده مفيهوش رجوع</label>`;
  h+=`<div class="row"><button class="pri" id="yes" ${risky.length?'disabled':''} onclick="doClean()">تأكيد ونظّف</button><button class="ghost" onclick="closeM()">إلغاء</button></div>`;
  showM(h);
}
async function doClean(){
  showM('<h3>جاري التنظيف...</h3><p>استنى لحظات، وممكن تظهر نافذة صغيرة من ويندوز.</p>');
  const r=await api().clean([...sel].map(i=>S[i].name));
  let h=`<h3>تم التنظيف ✅</h3><p>اتحرر حوالي <b>${num(r.freed)}</b>.</p>`;
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
  showM(`<h3>مجلد التنزيلات (أكبر العناصر)</h3><ul>${dl.map((d,i)=>`<li><label class="chk" style="margin:0"><input type="checkbox" ${dsel.has(i)?'checked':''} onchange="dtog(${i},this.checked)"> ${d.isdir?'📁 ':''}${d.name}</label><b>${num(d.size)}</b></li>`).join('')}</ul>
  <div class="row"><button class="pri" ${dsel.size?'':'disabled'} onclick="${sure?'doDl()':'drawDl(true)'}">${sure?'متأكد؟ اضغط تاني للحذف':'حذف المحدد'}</button><button class="ghost" onclick="closeM()">إغلاق</button></div>`);
}
function dtog(i,on){on?dsel.add(i):dsel.delete(i);drawDl(false)}
async function doDl(){
  const r=await api().downloads_delete([...dsel]);
  showM(`<h3>تم</h3><p>اتحرر <b>${num(r.freed)}</b>${r.failed?` (${r.failed} عنصر مقدرتش أمسحه)`:''}.</p><div class="row"><button class="pri" onclick="closeM();load()">تمام</button></div>`);
}
async function doUpdate(){
  $('updt').textContent='جاري التحميل... الأداة هتتقفل وتفتح تاني لوحدها';
  const ok=await api().install_update();if(!ok)$('updt').textContent='فشل التحديث، هنكمل بالنسخة الحالية';
}
window.addEventListener('pywebviewready',async()=>{
  load();
  const v=await api().find_update();
  if(v){$('upd').style.display='flex';$('updt').textContent='فيه إصدار جديد: '+v}
});
</script></body></html>
"""


def gui_main():
    import webview
    webview.create_window("منظف الكاش", html=HTML, js_api=Api(), width=940, height=700, min_size=(720, 540))
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
