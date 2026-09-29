"""Is this computer's clock right? Asked of a server, for certificate problems.

A clock that is behind makes new certificates look "not yet valid", and one
that is ahead makes them look expired, for every secure site at once, in
Chromium and Merlin Engine alike. Plain Python, with no Qt, so both use it.
"""
from __future__ import annotations

import time

DATE_PROBLEM_WORDS = ("not yet valid", "expired", "date")


def is_date_problem(text: str) -> bool:
    """Whether a certificate problem is about its dates, which the clock decides."""
    text = (text or "").lower()
    return "not yet valid" in text or "has expired" in text or "expired" in text \
        or "dateinvalid" in text.replace(" ", "")


def clock_skew(host: str, port: int = 443, timeout: float = 5.0):
    """How far the server's clock is ahead of this computer's, in seconds.

    Only the Date header of a HEAD request is read. The certificate cannot be
    checked here, as it is the one in question; nothing is sent but the
    request itself, to a server the page was already using.
    """
    import email.utils
    import http.client
    import ssl

    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    connection = http.client.HTTPSConnection(host, port, timeout=timeout, context=context)
    try:
        sent = time.time()
        connection.request("HEAD", "/", headers={"User-Agent": "Mozilla/5.0"})
        response = connection.getresponse()
        date = response.getheader("Date")
        received = time.time()
    finally:
        connection.close()
    if not date:
        return None
    server = email.utils.parsedate_to_datetime(date).timestamp()
    return server - (sent + received) / 2


def describe_skew(skew) -> str:
    if skew is None:
        return "Could not ask the server for its time."
    if abs(skew) < 120:
        return ("Your computer's clock agrees with the server's, so the problem is "
                "with the certificate itself.")
    minutes = int(abs(skew) // 60)
    span = (f"{minutes // 60} hour{'s' if minutes // 60 != 1 else ''} "
            f"{minutes % 60} minute{'s' if minutes % 60 != 1 else ''}"
            if minutes >= 60 else f"{minutes} minute{'s' if minutes != 1 else ''}")
    if skew > 0:
        return (f"Your computer's clock is {span} behind the server's. Certificates newer "
                f"than that look as if they are not valid yet. Setting the clock right fixes "
                f"this for every site; on Linux, 'timedatectl set-ntp true' keeps it right, and "
                f"a computer that also runs Windows may need 'timedatectl set-local-rtc 1'.")
    return (f"Your computer's clock is {span} ahead of the server's, so certificates can look "
            f"expired. Setting the clock right fixes this for every site.")
