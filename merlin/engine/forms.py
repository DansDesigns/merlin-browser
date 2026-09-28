"""Forms: which controls belong to a form, and what sending it sends.

Follows the HTML standard's rules for the common cases: the form a control
belongs to (its enclosing <form>, or the one its form attribute names), which
controls are sent (named, not disabled; boxes only when ticked; buttons only
the one pressed), and how: GET as a query string, POST as
application/x-www-form-urlencoded or multipart/form-data. File uploads are not
sent yet.

Pure Python, with nothing from Qt: the view supplies the current values.
"""
from __future__ import annotations

import mimetypes
import os
import urllib.parse
import uuid

from .dom import Element

class FileValue:
    """A file chosen in a file field, sent by its contents."""

    def __init__(self, path: str | None):
        self.path = path

    @property
    def name(self) -> str:
        return os.path.basename(self.path) if self.path else ""


TEXT_KINDS = {"text", "search", "email", "url", "tel", "password", "number",
              "date", "time", "datetime-local", "month", "week", "color", "range"}
BUTTON_KINDS = {"submit", "reset", "button", "image"}


def kind_of(element: Element) -> str:
    """What sort of control this is: "text", "password", "checkbox", "select"..."""
    if element.tag == "input":
        kind = element.attrs.get("type", "text").strip().lower() or "text"
        if kind in TEXT_KINDS or kind in BUTTON_KINDS or kind in (
                "checkbox", "radio", "hidden", "file"):
            return kind
        return "text"                    # an unknown type is a text field
    if element.tag == "button":
        kind = element.attrs.get("type", "submit").strip().lower()
        return kind if kind in ("submit", "reset", "button") else "submit"
    if element.tag in ("select", "textarea"):
        return element.tag
    return ""


def _root(element: Element) -> Element:
    node = element
    while node.parent is not None:
        node = node.parent
    return node


def form_of(element: Element) -> Element | None:
    """The form a control belongs to: named by its form attribute, or enclosing it."""
    named = element.attrs.get("form")
    if named:
        for candidate in _root(element).elements():
            if candidate.tag == "form" and candidate.attrs.get("id") == named:
                return candidate
        return None
    node = element.parent
    while isinstance(node, Element):
        if node.tag == "form":
            return node
        node = node.parent
    return None


def controls_of(form: Element) -> list:
    """Every control belonging to the form, in document order."""
    found = []
    form_id = form.attrs.get("id")
    for element in _root(form).elements():
        if not kind_of(element):
            continue
        if form_of(element) is form or (form_id and element.attrs.get("form") == form_id):
            if element not in found:
                found.append(element)
    return found


def _disabled(element: Element) -> bool:
    if "disabled" in element.attrs:
        return True
    node = element.parent
    while isinstance(node, Element):
        if node.tag == "fieldset" and "disabled" in node.attrs:
            return True
        node = node.parent
    return False


def default_value(element: Element):
    """A control's value as the page wrote it: text, True/False, or an option."""
    kind = kind_of(element)
    if kind in ("checkbox", "radio"):
        return "checked" in element.attrs
    if kind == "textarea":
        text = element.text()
        return text[1:] if text.startswith("\n") else text     # as browsers do
    if kind == "select":
        options = [o for o in element.elements() if o.tag == "option"]
        chosen = [o for o in options if "selected" in o.attrs]
        return option_value(chosen[0] if chosen else options[0]) if options else ""
    return element.attrs.get("value", "")


def option_value(option: Element) -> str:
    return option.attrs["value"] if "value" in option.attrs else " ".join(option.text().split())


def form_data(form: Element, values: dict, submitter: Element | None = None) -> list:
    """The (name, value) pairs sending the form sends, in order.

    values maps a control to its current value, as default_value gives it;
    a control missing from it has its default.
    """
    pairs = []
    for control in controls_of(form):
        name = control.attrs.get("name", "")
        if _disabled(control):
            continue
        kind = kind_of(control)
        value = values.get(control, default_value(control))
        if kind in BUTTON_KINDS:
            if control is not submitter or kind not in ("submit", "image"):
                continue
            if kind == "image":
                prefix = f"{name}." if name else ""
                pairs.extend([(prefix + "x", "0"), (prefix + "y", "0")])
                continue
            if name:
                pairs.append((name, control.attrs.get("value", "")))
            continue
        if not name:
            continue
        if kind == "file":
            # each chosen file; none chosen still sends an empty part, as
            # browsers do
            chosen = [p for p in (value or []) if p] if isinstance(value, (list, tuple)) else []
            for path in chosen or [None]:
                pairs.append((name, FileValue(path)))
            continue
        if kind in ("checkbox", "radio"):
            if value:
                pairs.append((name, control.attrs.get("value", "on")))
            continue
        if kind == "textarea":
            # line breaks are sent as CR LF, as the standard requires
            value = str(value).replace("\r\n", "\n").replace("\n", "\r\n")
        pairs.append((name, str(value)))
    return pairs


def submission(form: Element, pairs: list, page_url: str,
               submitter: Element | None = None) -> dict | None:
    """Where and how sending the form goes: url, method, body, content type, target.

    None when it cannot go anywhere MerlinEngine can follow, such as a
    javascript: or mailto: action.
    """
    def attr(name: str, fallback: str = "") -> str:
        if submitter is not None and f"form{name}" in submitter.attrs:
            return submitter.attrs[f"form{name}"]
        return form.attrs.get(name, fallback)

    action = attr("action").strip()
    url = urllib.parse.urljoin(page_url, action) if action else page_url
    scheme = urllib.parse.urlsplit(url).scheme.lower()
    if scheme not in ("http", "https", "file", ""):
        return None
    method = attr("method", "get").strip().lower()
    target = attr("target").strip().lower()
    if method == "dialog":
        return None
    def plain(pairs):
        # anywhere but a multipart body, a file is sent as just its name
        return [(n, v.name if isinstance(v, FileValue) else v) for n, v in pairs]

    if method != "post":
        parts = urllib.parse.urlsplit(url)
        query = urllib.parse.urlencode(plain(pairs))
        # the query replaces any the action had, and the fragment is kept
        url = urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, query,
                                       parts.fragment))
        return {"url": url, "method": "GET", "body": None, "type": "", "target": target}
    enctype = attr("enctype", "application/x-www-form-urlencoded").strip().lower()
    if enctype == "multipart/form-data":
        boundary = "----MerlinForm" + uuid.uuid4().hex
        body = bytearray()

        def quoted(text: str) -> str:
            return text.replace("\\", "\\\\").replace('"', "%22").replace("\r", "%0D") \
                .replace("\n", "%0A")

        for name, value in pairs:
            body += f"--{boundary}\r\n".encode()
            if isinstance(value, FileValue):
                kind = (mimetypes.guess_type(value.name)[0] if value.name else None) \
                    or "application/octet-stream"
                body += (f'Content-Disposition: form-data; name="{quoted(name)}"; '
                         f'filename="{quoted(value.name)}"\r\n'
                         f"Content-Type: {kind}\r\n\r\n").encode("utf-8")
                if value.path:
                    with open(value.path, "rb") as handle:
                        body += handle.read()
                body += b"\r\n"
            else:
                body += (f'Content-Disposition: form-data; name="{quoted(name)}"\r\n\r\n'
                         f"{value}\r\n").encode("utf-8")
        body += f"--{boundary}--\r\n".encode()
        return {"url": url, "method": "POST", "body": bytes(body),
                "type": f"multipart/form-data; boundary={boundary}", "target": target}
    if enctype == "text/plain":
        body = "".join(f"{n}={v}\r\n" for n, v in plain(pairs)).encode("utf-8")
        return {"url": url, "method": "POST", "body": body, "type": "text/plain",
                "target": target}
    return {"url": url, "method": "POST", "body": urllib.parse.urlencode(plain(pairs)).encode("utf-8"),
            "type": "application/x-www-form-urlencoded", "target": target}


def dialog_filter(accept: str) -> str:
    """A file dialog's filter for an accept attribute: "image/*,.pdf"..."""
    patterns = []
    for item in (a.strip().lower() for a in (accept or "").split(",")):
        if not item:
            continue
        if item.startswith("."):
            patterns.append("*" + item)
        elif item.endswith("/*"):
            family = item[:-1]
            patterns.extend("*" + ext for ext, kind in mimetypes.types_map.items()
                            if kind.startswith(family))
        else:
            patterns.extend("*" + ext for ext in mimetypes.guess_all_extensions(item))
    if not patterns:
        return "All files (*)"
    unique = sorted(set(patterns))
    return f"Accepted files ({' '.join(unique)});;All files (*)"
