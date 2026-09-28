"""FTP, SMB and local folders for MerlinEngine, and saving downloads.

Chromium removed FTP and never opened SMB, so these addresses are MerlinEngine's
to handle. A folder becomes an index page to click through; a file that is a
page or text is shown, and anything else is saved to the Downloads folder,
streamed to disk so a large one never sits in memory.

FTP and FTPS use Python's own client, with any username and password in the
address. SMB takes each system's own way in: on Windows the address becomes a
network path, \\\\server\\share, which Windows opens with the network sign-in
already in place; on Linux the desktop's SMB support (GVFS, through gio) when
there is one, and otherwise smbclient.
"""
from __future__ import annotations

import html
import os
import posixpath
import re
import shutil
import subprocess
import time
import urllib.parse

SHOWN = ("text/html", "application/xhtml+xml", "text/plain")
CHUNK = 256 * 1024


# ------------------------------------------------------------------ pages

def _size(n) -> str:
    if n is None or n == "":
        return ""
    n = float(n)
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def folder_page(title: str, entries: list, parent: bool = True) -> str:
    """An index page: entries are (name, is_folder, size, modified)."""
    rows = []
    if parent:
        rows.append('<tr><td><a href="../">\u2191 Parent folder</a></td><td></td><td></td></tr>')
    ordered = sorted(entries, key=lambda e: (not e[1], e[0].lower()))
    for name, is_folder, size, modified in ordered:
        href = urllib.parse.quote(name) + ("/" if is_folder else "")
        label = html.escape(name) + ("/" if is_folder else "")
        icon = "\U0001F4C1 " if is_folder else "\U0001F4C4 "
        rows.append(f'<tr><td><a href="{href}">{icon}{label}</a></td>'
                    f'<td class=n>{"" if is_folder else _size(size)}</td>'
                    f'<td>{html.escape(modified or "")}</td></tr>')
    empty = "" if entries else "<p class=quiet>This folder is empty.</p>"
    return f"""<title>{html.escape(title)}</title>
<style>
 body {{ font-family: sans-serif; margin: 24px; color: #1d2030 }}
 h1 {{ font-size: 20px; font-weight: 600; word-break: break-all }}
 table {{ border-collapse: collapse; width: 100%; max-width: 900px }}
 td {{ padding: 5px 12px 5px 0; border-bottom: 1px solid #e3e5ec; vertical-align: top }}
 td.n {{ text-align: right; white-space: nowrap; color: #555 }}
 a {{ color: #2a55c8; text-decoration: none }}
 .quiet {{ color: #777 }}
</style>
<h1>{html.escape(title)}</h1>{empty}<table>{''.join(rows)}</table>"""


def message_page(title: str, text: str) -> str:
    return (f"<title>{html.escape(title)}</title><body style='font-family:sans-serif;"
            f"margin:40px;color:#1d2030'><h2>{html.escape(title)}</h2>"
            f"<p style='max-width:640px;line-height:1.5'>{text}</p></body>")


def download_page(path: str, size: int) -> str:
    folder = os.path.dirname(path)
    folder_url = "file:///" + folder.replace("\\", "/").lstrip("/")
    return message_page(
        f"Downloaded {os.path.basename(path)}",
        f"Saved to <a href='{html.escape(folder_url)}/'>{html.escape(folder)}</a>, "
        f"{_size(size)}.")


def unique_path(folder: str, name: str) -> str:
    """A path in folder for name that does not overwrite anything there."""
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip(" .") or "download"
    base, ext = os.path.splitext(name)
    candidate = os.path.join(folder, name)
    count = 1
    while os.path.exists(candidate):
        candidate = os.path.join(folder, f"{base} ({count}){ext}")
        count += 1
    return candidate


def shown_type(kind: str, name: str = "") -> str:
    """Whether a file is shown as a page ("html"), as text ("text"), or saved ("")."""
    kind = (kind or "").split(";")[0].strip().lower()
    if not kind and name:
        import mimetypes

        kind = mimetypes.guess_type(name)[0] or ""
    if kind in ("text/html", "application/xhtml+xml"):
        return "html"
    if kind.startswith("text/") or kind in ("application/json", "application/xml"):
        return "text"
    return ""


def text_page(name: str, data: bytes) -> str:
    return (f"<title>{html.escape(name)}</title><pre style='white-space:pre-wrap;"
            f"font-family:monospace;margin:16px'>{html.escape(data.decode('utf-8', 'replace'))}</pre>")


def save_stream(read, folder: str, name: str, progress=None, total: int = 0) -> tuple:
    """Stream from read(n) to a new file in folder: (path, size)."""
    os.makedirs(folder, exist_ok=True)
    path = unique_path(folder, name)
    partial = path + ".part"
    size = 0
    last = 0.0
    with open(partial, "wb") as handle:
        while True:
            block = read(CHUNK)
            if not block:
                break
            handle.write(block)
            size += len(block)
            if progress and total and time.monotonic() - last > 0.25:
                last = time.monotonic()
                progress(min(99, int(size * 100 / total)))
    os.replace(partial, path)
    return path, size


# ------------------------------------------------------------------ local folders

def local_folder(path: str, url_title: str) -> str:
    entries = []
    with os.scandir(path) as listing:
        for entry in listing:
            try:
                is_folder = entry.is_dir()
                info = entry.stat()
                entries.append((entry.name, is_folder, None if is_folder else info.st_size,
                                time.strftime("%Y-%m-%d %H:%M", time.localtime(info.st_mtime))))
            except OSError:
                entries.append((entry.name, False, None, ""))
    return folder_page(url_title, entries)


def _open_local(path: str, url: str, download_dir: str, progress=None) -> tuple:
    """A folder or file reached as a path: (kind, payload, final url)."""
    if os.path.isdir(path):
        final = url if url.endswith("/") else url + "/"
        return "html", local_folder(path, urllib.parse.unquote(final)), final
    name = os.path.basename(path)
    shown = shown_type("", name)
    if shown:
        with open(path, "rb") as handle:
            data = handle.read(8 * 1024 * 1024)
        return "html", (data.decode("utf-8", "replace") if shown == "html"
                        else text_page(name, data)), url
    total = os.path.getsize(path)
    with open(path, "rb") as handle:
        saved, size = save_stream(handle.read, download_dir, name, progress, total)
    return "download", (saved, size), url


# ------------------------------------------------------------------ FTP

def _ftp(url: str, download_dir: str, timeout: int = 20, progress=None) -> tuple:
    import ftplib

    parts = urllib.parse.urlsplit(url)
    secure = parts.scheme.lower() == "ftps"
    client = ftplib.FTP_TLS(timeout=timeout) if secure else ftplib.FTP(timeout=timeout)
    client.connect(parts.hostname or "", parts.port or 21)
    try:
        client.login(urllib.parse.unquote(parts.username or "anonymous"),
                     urllib.parse.unquote(parts.password or "anonymous@"))
        if secure:
            client.prot_p()
        path = urllib.parse.unquote(parts.path) or "/"
        try:
            client.cwd(path)
            is_folder = True
        except ftplib.error_perm:
            is_folder = False
        if is_folder:
            final = url if url.endswith("/") else url + "/"
            return "html", folder_page(f"Index of {path}", _ftp_listing(client),
                                       parent=path not in ("/", "")), final
        name = posixpath.basename(path)
        try:
            total = client.size(path) or 0
        except ftplib.all_errors:
            total = 0
        shown = shown_type("", name)
        if shown:
            chunks = []
            client.retrbinary(f"RETR {path}", chunks.append)
            data = b"".join(chunks)
            return "html", (data.decode("utf-8", "replace") if shown == "html"
                            else text_page(name, data)), url
        # binary mode: transfercmd, unlike retrbinary, leaves the connection in
        # text mode, and the server then rewrites every line ending in the file
        client.voidcmd("TYPE I")
        connection = client.transfercmd(f"RETR {path}")
        with connection:
            reader = connection.makefile("rb")
            saved, size = save_stream(reader.read, download_dir, name, progress, total)
        client.voidresp()
        return "download", (saved, size), url
    finally:
        try:
            client.quit()
        except Exception:                                  # noqa: BLE001
            client.close()


def _ftp_listing(client) -> list:
    entries = []
    try:
        for name, facts in client.mlsd(facts=["type", "size", "modify"]):
            if name in (".", "..") or facts.get("type") in ("cdir", "pdir"):
                continue
            modified = facts.get("modify", "")
            if len(modified) >= 12:
                modified = f"{modified[0:4]}-{modified[4:6]}-{modified[6:8]} {modified[8:10]}:{modified[10:12]}"
            entries.append((name, facts.get("type") == "dir", facts.get("size"), modified))
        return entries
    except Exception:                                      # noqa: BLE001
        pass
    # older servers: the Unix-style LIST
    lines = []
    client.retrlines("LIST", lines.append)
    for line in lines:
        found = re.match(r"^([dl-])\S+\s+\S+\s+\S+\s+\S+\s+(\d+)\s+(\w+\s+\d+\s+[\d:]+)\s+(.+)$", line)
        if found:
            name = found.group(4).split(" -> ")[0]
            if name not in (".", ".."):
                entries.append((name, found.group(1) == "d", found.group(2), found.group(3)))
    return entries


# ------------------------------------------------------------------ SMB

def _smb_parts(url: str):
    parts = urllib.parse.urlsplit(url)
    segments = [urllib.parse.unquote(s) for s in parts.path.split("/") if s]
    share = segments[0] if segments else ""
    rest = segments[1:]
    return parts, share, rest


def unc_path(url: str) -> str:
    """smb://server/share/a/b as the Windows network path \\\\server\\share\\a\\b."""
    parts, share, rest = _smb_parts(url)
    return "\\\\" + "\\".join([parts.hostname or ""] + ([share] if share else []) + rest)


def _run(command: list, timeout: int = 25, stdin: str | None = None) -> tuple:
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000         # no console window
    if stdin is None:
        # never Merlin's own input: a command asking for a password would
        # otherwise wait on it, and from a terminal wait for ever
        kwargs["stdin"] = subprocess.DEVNULL
    done = subprocess.run(command, input=stdin, capture_output=True, text=True,
                          timeout=timeout, **kwargs)
    return done.returncode, done.stdout, done.stderr


def _smb(url: str, download_dir: str, progress=None) -> tuple:
    parts, share, rest = _smb_parts(url)
    server = parts.hostname or ""
    if not server:
        return "html", message_page("No server", "An smb:// address names a server: "
                                    "smb://server/share/folder."), url
    if os.name == "nt":
        if not share:
            code, out, err = _run(["net", "view", f"\\\\{server}"])
            if code != 0:
                raise OSError((err or out).strip() or f"Could not list the shares on {server}")
            shares = [line.split()[0] for line in out.splitlines()
                      if re.match(r"^\S+\s+(Disk|Disque|Datenträger|Disco)\b", line)]
            return "html", folder_page(f"Shares on {server}", [(s, True, None, "") for s in shares],
                                       parent=False), url if url.endswith("/") else url + "/"
        return _open_local(unc_path(url), url, download_dir, progress)
    # Linux and the like: a share the desktop has mounted, or mounts now
    mounted = _gvfs_path(server, share, rest)
    if share and mounted is None and shutil.which("gio"):
        target = f"smb://{server}{f':{parts.port}' if parts.port else ''}/{share}"
        user = urllib.parse.unquote(parts.username or "")
        if user:
            _run(["gio", "mount", target], stdin=f"{user}\n\n{urllib.parse.unquote(parts.password or '')}\n")
        else:
            _run(["gio", "mount", "-a", target])
        mounted = _gvfs_path(server, share, rest)
    if mounted is not None:
        return _open_local(mounted, url, download_dir, progress)
    if shutil.which("smbclient"):
        return _smbclient(parts, server, share, rest, url, download_dir)
    raise OSError("Opening smb:// addresses on Linux needs the desktop's SMB support "
                  "(the gvfs-backends package, which provides gio) or smbclient; "
                  "neither was found.")


def _gvfs_path(server: str, share: str, rest: list):
    if not share or not hasattr(os, "getuid"):
        return None
    base = f"/run/user/{os.getuid()}/gvfs"
    if not os.path.isdir(base):
        return None
    for name in os.listdir(base):
        if name.lower() == f"smb-share:server={server.lower()},share={share.lower()}":
            path = os.path.join(base, name, *rest)
            return path if os.path.exists(path) else None
    return None


def _smbclient(parts, server, share, rest, url, download_dir) -> tuple:
    who = ["-N"]
    if parts.username:
        who = ["-U", f"{urllib.parse.unquote(parts.username)}%{urllib.parse.unquote(parts.password or '')}"]
    port = ["-p", str(parts.port)] if parts.port else []
    if not share:
        code, out, err = _run(["smbclient", "-L", f"//{server}", "-g"] + port + who)
        shares = [line.split("|")[1] for line in out.splitlines()
                  if line.startswith("Disk|")]
        if code != 0 and not shares:
            raise OSError((err or out).strip()[-300:] or f"Could not list the shares on {server}")
        return "html", folder_page(f"Shares on {server}", [(s, True, None, "") for s in shares],
                                   parent=False), url if url.endswith("/") else url + "/"
    remote = "\\".join(rest)
    base = ["smbclient", f"//{server}/{share}"] + port + who
    code, out, err = _run(base + ["-c", f'ls "{remote}\\*"' if remote else "ls"])
    listing = _smbclient_listing(out)
    if code == 0 and (listing or "blocks of size" in out) and not _smbclient_is_file(out, rest):
        final = url if url.endswith("/") else url + "/"
        return "html", folder_page(f"{share}/{'/'.join(rest)}", listing), final
    # not a folder: fetch it as a file
    name = rest[-1] if rest else share
    os.makedirs(download_dir, exist_ok=True)
    target = unique_path(download_dir, name)
    code, out, err = _run(base + ["-c", f'get "{remote}" "{target}"'], timeout=3600)
    if code != 0 or not os.path.exists(target):
        raise OSError((err or out).strip()[-300:] or f"Could not open {url}")
    shown = shown_type("", name)
    if shown:
        with open(target, "rb") as handle:
            data = handle.read(8 * 1024 * 1024)
        os.remove(target)
        return "html", (data.decode("utf-8", "replace") if shown == "html"
                        else text_page(name, data)), url
    return "download", (target, os.path.getsize(target)), url


def _smbclient_listing(out: str) -> list:
    entries = []
    for line in out.splitlines():
        found = re.match(r"^\s{2}(.+?)\s+([A-Z]*)\s+(\d+)\s+(\w{3}\s+\w{3}\s+\d+\s+[\d:]+\s+\d{4})\s*$", line)
        if found and found.group(1) not in (".", ".."):
            entries.append((found.group(1), "D" in found.group(2), found.group(3), found.group(4)))
    return entries


def _smbclient_is_file(out: str, rest: list) -> bool:
    """smbclient lists a file itself, not its contents, when asked for name\\*."""
    entries = _smbclient_listing(out)
    return bool(rest) and len(entries) == 1 and entries[0][0] == rest[-1] and not entries[0][1]


# ------------------------------------------------------------------ the way in

def open_remote(url: str, download_dir: str, progress=None) -> tuple:
    """(ok, final url, page html, downloaded path or "") for ftp, ftps, smb and file."""
    scheme = urllib.parse.urlsplit(url).scheme.lower()
    try:
        if scheme in ("ftp", "ftps"):
            kind, payload, final = _ftp(url, download_dir, progress=progress)
        elif scheme == "smb":
            kind, payload, final = _smb(url, download_dir, progress)
        elif scheme == "file":
            from PyQt6.QtCore import QUrl

            kind, payload, final = _open_local(QUrl(url).toLocalFile(), url, download_dir, progress)
        else:
            return False, url, f"MerlinEngine cannot open {scheme}: addresses yet.", ""
    except Exception as exc:                               # noqa: BLE001
        return False, url, str(exc) or type(exc).__name__, ""
    if kind == "download":
        path, size = payload
        return True, final, download_page(path, size), path
    return True, final, payload, ""
