"""Saved logins.

Credentials are never written in the clear. The encryption key is held by the
operating system, not by Merlin:

  Windows   DPAPI, through CryptProtectData. The blob can only be decrypted by
            the same Windows user account on the same machine.
  elsewhere the `keyring` package, which talks to the login keyring or Secret
            Service. If it is not installed, nothing is saved and the reason is
            reported, because a key sitting in a file beside the database is
            not protection, it is decoration.

Merlin still does not read another browser's password store. That store is
encrypted the same way, and prising it open means implementing exactly what a
credential stealer does. Export from the other browser, where it can ask you to
confirm, and import that file here.
"""
from __future__ import annotations

import base64
import json
import os

from . import settings as cfg

STORE_PATH = os.path.join(cfg.CONFIG_DIR, "logins.dat")
KEYRING_SERVICE = "merlin-browser"
KEYRING_USER = "login-store"


# ----------------------------------------------------------------- Windows
def _dpapi(data: bytes, encrypt: bool) -> bytes | None:
    """CryptProtectData / CryptUnprotectData, tied to this Windows account."""
    try:
        import ctypes
        from ctypes import wintypes

        class Blob(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD),
                        ("pbData", ctypes.POINTER(ctypes.c_char))]

        crypt32 = ctypes.windll.crypt32
        source = Blob(len(data), ctypes.cast(ctypes.create_string_buffer(data),
                                             ctypes.POINTER(ctypes.c_char)))
        result = Blob()
        function = (crypt32.CryptProtectData if encrypt
                    else crypt32.CryptUnprotectData)
        # description, entropy, reserved, prompt, flags, out
        if not function(ctypes.byref(source), None, None, None, None, 0,
                        ctypes.byref(result)):
            return None
        try:
            return ctypes.string_at(result.pbData, result.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(result.pbData)
    except Exception:                                    # noqa: BLE001
        return None


# ----------------------------------------------------------------- keyring
def _keyring():
    try:
        import keyring

        return keyring
    except Exception:                                    # noqa: BLE001
        return None


def _fernet_key() -> bytes | None:
    """A key from the system keyring, created on first use."""
    ring = _keyring()
    if ring is None:
        return None
    try:
        from cryptography.fernet import Fernet
    except Exception:                                    # noqa: BLE001
        return None
    existing = ring.get_password(KEYRING_SERVICE, KEYRING_USER)
    if existing:
        return existing.encode("ascii")
    key = Fernet.generate_key()
    ring.set_password(KEYRING_SERVICE, KEYRING_USER, key.decode("ascii"))
    return key


def backend() -> str:
    """Which protection is available: 'dpapi', 'keyring' or ''."""
    if os.name == "nt":
        return "dpapi" if _dpapi(b"probe", True) is not None else ""
    return "keyring" if _fernet_key() else ""


def backend_note() -> str:
    kind = backend()
    if kind == "dpapi":
        return ("Protected by Windows DPAPI: only this Windows account on this "
                "machine can read them.")
    if kind == "keyring":
        return ("Protected by a key held in your system keyring.")
    if os.name == "nt":
        return "Windows DPAPI is not answering, so nothing can be saved."
    return ("No system keyring is available. Install the 'keyring' and "
            "'cryptography' packages to enable saved logins; Merlin will not "
            "keep passwords in a file it can read on its own.")


# ------------------------------------------------------------------- store
def _encrypt(plain: bytes) -> bytes | None:
    if os.name == "nt":
        return _dpapi(plain, True)
    key = _fernet_key()
    if not key:
        return None
    from cryptography.fernet import Fernet

    return Fernet(key).encrypt(plain)


def _decrypt(blob: bytes) -> bytes | None:
    if os.name == "nt":
        return _dpapi(blob, False)
    key = _fernet_key()
    if not key:
        return None
    from cryptography.fernet import Fernet

    try:
        return Fernet(key).decrypt(blob)
    except Exception:                                    # noqa: BLE001
        return None


def load() -> list[dict]:
    if not os.path.isfile(STORE_PATH):
        return []
    try:
        with open(STORE_PATH, "rb") as handle:
            blob = base64.b64decode(handle.read())
    except (OSError, ValueError):
        return []
    plain = _decrypt(blob)
    if plain is None:
        return []
    try:
        entries = json.loads(plain.decode("utf-8"))
    except ValueError:
        return []
    return entries if isinstance(entries, list) else []


def save(entries: list[dict]) -> tuple[bool, str]:
    blob = _encrypt(json.dumps(entries).encode("utf-8"))
    if blob is None:
        return False, backend_note()
    try:
        os.makedirs(os.path.dirname(STORE_PATH), exist_ok=True)
        with open(STORE_PATH, "wb") as handle:
            handle.write(base64.b64encode(blob))
        if os.name != "nt":
            os.chmod(STORE_PATH, 0o600)
    except OSError as exc:
        return False, f"Could not write the store: {exc}"
    return True, ""


def host_of(url: str) -> str:
    from urllib.parse import urlparse

    try:
        host = urlparse(url if "//" in url else "//" + url).hostname or ""
    except ValueError:
        return ""
    return host.lower().removeprefix("www.")


def add_many(new_entries: list[dict]) -> tuple[int, str]:
    """Merge in imported logins, replacing any with the same host and user."""
    existing = load()
    index = {(host_of(e.get("url", "")), e.get("username", "")): i
             for i, e in enumerate(existing)}
    added = 0
    for entry in new_entries:
        url = (entry.get("url") or "").strip()
        if not url or not entry.get("password"):
            continue
        key = (host_of(url), entry.get("username", ""))
        record = {"url": url, "host": host_of(url),
                  "username": entry.get("username", ""),
                  "password": entry["password"]}
        if key in index:
            existing[index[key]] = record
        else:
            existing.append(record)
            added += 1
    ok, problem = save(existing)
    if not ok:
        return 0, problem
    return added, ""


def for_host(url: str) -> list[dict]:
    host = host_of(url)
    if not host:
        return []
    return [e for e in load() if e.get("host") == host]


def forget(url: str, username: str) -> bool:
    entries = load()
    kept = [e for e in entries
            if not (e.get("host") == host_of(url)
                    and e.get("username", "") == username)]
    if len(kept) == len(entries):
        return False
    return save(kept)[0]


FILL_SCRIPT = """
(function (user, secret) {
  function visible(el) {
    return el.offsetParent !== null && !el.disabled && !el.readOnly;
  }
  var pass = Array.from(document.querySelectorAll('input[type=password]'))
                  .filter(visible)[0];
  if (!pass) { return 'no password field on this page'; }
  var form = pass.form || document;
  var names = 'input[type=text], input[type=email], input:not([type]), '
            + 'input[type=tel], input[autocomplete=username]';
  var users = Array.from(form.querySelectorAll(names)).filter(visible);
  var field = users[users.length - 1];
  function put(el, value) {
    if (!el) { return; }
    el.focus();
    el.value = value;
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
  }
  put(field, user);
  put(pass, secret);
  return 'filled';
})(%s, %s);
"""


def fill_script(username: str, password: str) -> str:
    """The script that fills a login form, with both values JSON-escaped."""
    return FILL_SCRIPT % (json.dumps(username), json.dumps(password))
