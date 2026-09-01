"""Write an icon into a Windows executable's resources.

Merlin runs from a copy of the interpreter renamed to Merlin.exe. That gives it
its own process identity, but the copy still carries Python's icon in its
resources, and that icon is what Windows falls back to whenever it cannot get
one from the window or the shortcut.

This replaces it outright using UpdateResource, the documented API for editing
an executable's resources, so the fallback is Merlin's icon too. It runs once,
at install time.

The .ico layout and the RT_GROUP_ICON directory that has to be built from it are
plain binary structures, so they are unit tested on any platform; only the three
API calls at the end are Windows-only.
"""
from __future__ import annotations

import os
import struct

RT_ICON = 3
RT_GROUP_ICON = 14
LANG_NEUTRAL = 0


def parse_ico(data: bytes) -> list[tuple[dict, bytes]]:
    """Split an .ico into its directory entries and their image payloads."""
    if len(data) < 6:
        raise ValueError("not an icon file")
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    if reserved != 0 or kind != 1 or count == 0:
        raise ValueError("not an icon file")

    images = []
    offset = 6
    for _ in range(count):
        if offset + 16 > len(data):
            raise ValueError("icon directory is truncated")
        (width, height, colours, entry_reserved, planes, bits,
         size, data_offset) = struct.unpack("<BBBBHHII", data[offset:offset + 16])
        offset += 16
        payload = data[data_offset:data_offset + size]
        if len(payload) != size:
            raise ValueError("icon image is truncated")
        images.append((
            {"width": width, "height": height, "colours": colours,
             "reserved": entry_reserved, "planes": planes, "bits": bits,
             "size": size},
            payload,
        ))
    return images


def build_group(images: list[tuple[dict, bytes]], first_id: int = 1) -> bytes:
    """Build the RT_GROUP_ICON directory that refers to the RT_ICON entries.

    Same layout as the file header, except each entry ends with a 2-byte
    resource id instead of a 4-byte file offset.
    """
    out = struct.pack("<HHH", 0, 1, len(images))
    for index, (entry, _payload) in enumerate(images):
        out += struct.pack(
            "<BBBBHHIH",
            entry["width"], entry["height"], entry["colours"], entry["reserved"],
            entry["planes"], entry["bits"], entry["size"], first_id + index,
        )
    return out


def set_exe_icon(exe_path: str, ico_path: str) -> tuple[bool, str]:
    """Replace the icon resources of exe_path with the contents of ico_path."""
    if os.name != "nt":
        return False, "only meaningful on Windows"
    if not os.path.isfile(exe_path):
        return False, f"no such executable: {exe_path}"
    if not os.path.isfile(ico_path):
        return False, f"no such icon: {ico_path}"

    try:
        with open(ico_path, "rb") as handle:
            images = parse_ico(handle.read())
    except (OSError, ValueError) as exc:
        return False, f"could not read the icon: {exc}"

    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        kernel32.BeginUpdateResourceW.argtypes = [wintypes.LPCWSTR, wintypes.BOOL]
        kernel32.BeginUpdateResourceW.restype = wintypes.HANDLE
        kernel32.UpdateResourceW.argtypes = [
            wintypes.HANDLE, wintypes.LPCWSTR, wintypes.LPCWSTR,
            wintypes.WORD, wintypes.LPVOID, wintypes.DWORD,
        ]
        kernel32.UpdateResourceW.restype = wintypes.BOOL
        kernel32.EndUpdateResourceW.argtypes = [wintypes.HANDLE, wintypes.BOOL]
        kernel32.EndUpdateResourceW.restype = wintypes.BOOL

        handle = kernel32.BeginUpdateResourceW(exe_path, False)
        if not handle:
            return False, f"BeginUpdateResource failed ({ctypes.GetLastError()})"

        # Resource type and name are passed as integers cast into a pointer,
        # which is how MAKEINTRESOURCE works.
        def as_id(value):
            return ctypes.cast(value, wintypes.LPCWSTR)

        for index, (_entry, payload) in enumerate(images):
            buffer = ctypes.create_string_buffer(payload, len(payload))
            if not kernel32.UpdateResourceW(
                    handle, as_id(RT_ICON), as_id(1 + index), LANG_NEUTRAL,
                    buffer, len(payload)):
                kernel32.EndUpdateResourceW(handle, True)
                return False, f"UpdateResource failed on image {index + 1}"

        group = build_group(images)
        group_buffer = ctypes.create_string_buffer(group, len(group))
        if not kernel32.UpdateResourceW(
                handle, as_id(RT_GROUP_ICON), as_id(1), LANG_NEUTRAL,
                group_buffer, len(group)):
            kernel32.EndUpdateResourceW(handle, True)
            return False, "UpdateResource failed on the icon directory"

        if not kernel32.EndUpdateResourceW(handle, False):
            return False, f"EndUpdateResource failed ({ctypes.GetLastError()})"
    except Exception as exc:                             # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"

    ok, detail = verify_exe_icon(exe_path, len(group))
    name = os.path.basename(exe_path)
    if not ok:
        return False, f"wrote the icon into {name} but {detail}"
    return True, f"embedded {len(images)} icon images into {name}; {detail}"


def group_size_in_exe(exe_path: str) -> int:
    """Size of the icon group inside an executable, or 0 if it has none."""
    if os.name != "nt" or not os.path.isfile(exe_path):
        return 0
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        kernel32.LoadLibraryExW.argtypes = [wintypes.LPCWSTR, wintypes.HANDLE,
                                            wintypes.DWORD]
        kernel32.LoadLibraryExW.restype = wintypes.HMODULE
        kernel32.FindResourceW.argtypes = [wintypes.HMODULE, wintypes.LPCWSTR,
                                           wintypes.LPCWSTR]
        kernel32.FindResourceW.restype = wintypes.HANDLE
        kernel32.SizeofResource.argtypes = [wintypes.HMODULE, wintypes.HANDLE]
        kernel32.SizeofResource.restype = wintypes.DWORD
        module = kernel32.LoadLibraryExW(exe_path, None, 0x00000002)
        if not module:
            return 0
        try:
            found = kernel32.FindResourceW(
                module, ctypes.cast(1, wintypes.LPCWSTR),
                ctypes.cast(RT_GROUP_ICON, wintypes.LPCWSTR))
            return int(kernel32.SizeofResource(module, found)) if found else 0
        finally:
            kernel32.FreeLibrary(module)
    except Exception:                                    # noqa: BLE001
        return 0


def verify_exe_icon(exe_path: str, expected_group_size: int) -> tuple[bool, str]:
    """Read the icon group back out of the executable.

    Writing resources can report success and still leave nothing usable, so the
    installer says whether the icon is really there rather than assuming.
    """
    if os.name != "nt":
        return True, "not verified off Windows"
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        LOAD_LIBRARY_AS_DATAFILE = 0x00000002

        kernel32.LoadLibraryExW.argtypes = [wintypes.LPCWSTR, wintypes.HANDLE,
                                            wintypes.DWORD]
        kernel32.LoadLibraryExW.restype = wintypes.HMODULE
        kernel32.FindResourceW.argtypes = [wintypes.HMODULE, wintypes.LPCWSTR,
                                           wintypes.LPCWSTR]
        kernel32.FindResourceW.restype = wintypes.HANDLE
        kernel32.SizeofResource.argtypes = [wintypes.HMODULE, wintypes.HANDLE]
        kernel32.SizeofResource.restype = wintypes.DWORD
        kernel32.FreeLibrary.argtypes = [wintypes.HMODULE]

        module = kernel32.LoadLibraryExW(exe_path, None, LOAD_LIBRARY_AS_DATAFILE)
        if not module:
            return False, "the file could not be reopened to check it"
        try:
            found = kernel32.FindResourceW(
                module,
                ctypes.cast(1, wintypes.LPCWSTR),
                ctypes.cast(RT_GROUP_ICON, wintypes.LPCWSTR),
            )
            if not found:
                return False, "no icon group is present afterwards"
            size = kernel32.SizeofResource(module, found)
        finally:
            kernel32.FreeLibrary(module)
        if size != expected_group_size:
            return False, (f"the icon group is {size} bytes, expected "
                           f"{expected_group_size}")
        return True, f"verified, icon group is {size} bytes"
    except Exception as exc:                             # noqa: BLE001
        return False, f"could not be checked: {exc}"
