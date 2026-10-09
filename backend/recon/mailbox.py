"""Signup mailbox access (operator-owned inbox for trial registrations).

The operator creates a DEDICATED throwaway mailbox and puts it in `.env`
(SIGNUP_IMAP_* — see .env.example). The trial_signup worker registers
USER+tag@domain on targets, then polls here for the verification mail.

Security rules:
- IMAP over SSL only, short timeouts. The password never appears in any
  returned string, log line, or AgentAction output.
- Mail bodies are NEVER stored: only the extracted verification link's host
  + fetch status leave this module. Bodies contain session tokens.
"""

import imaplib
import os
import re
import socket

IMAP_TIMEOUT_SECONDS = 20

_LINK_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)


def status():
    """Configured flag for the UI (host only — never user/pass)."""
    host = (os.environ.get("SIGNUP_IMAP_HOST") or "").strip()
    user = (os.environ.get("SIGNUP_IMAP_USER") or "").strip()
    password = os.environ.get("SIGNUP_IMAP_PASS") or ""
    return {
        "configured": bool(host and user and password),
        "host": host or "",
    }


def _connect():
    st = status()
    if not st["configured"]:
        raise MailboxError(
            "Signup mailbox not configured — set SIGNUP_IMAP_HOST/USER/PASS in .env."
        )
    port = int(os.environ.get("SIGNUP_IMAP_PORT") or 993)
    try:
        box = imaplib.IMAP4_SSL(st["host"], port, timeout=IMAP_TIMEOUT_SECONDS)
        box.login(os.environ["SIGNUP_IMAP_USER"], os.environ["SIGNUP_IMAP_PASS"])
        box.select("INBOX")
        return box
    except (imaplib.IMAP4.error, OSError, socket.timeout, ValueError) as exc:
        raise MailboxError(f"Mailbox login/select failed ({exc}).")


class MailboxError(Exception):
    """Mailbox unreachable, misconfigured, or verification mail absent."""


def _decode_header(raw):
    import email.header

    parts = email.header.decode_header(raw or "")
    out = []
    for text, charset in parts:
        if isinstance(text, bytes):
            out.append(text.decode(charset or "utf-8", "replace"))
        else:
            out.append(text)
    return "".join(out)


def poll_verification_link(to_addr, sender_hint="", timeout=90):
    """Wait up to `timeout` seconds for an UNSEEN mail to `to_addr`
    (exact To match — safe with plus-addressed tags under concurrency).
    Returns (verification_url, from_header). Raises MailboxError."""
    import email
    import time

    box = _connect()
    try:
        deadline = time.monotonic() + timeout
        while True:
            box.noop()
            typ, data = box.uid("search", None, '(UNSEEN TO "%s")' % to_addr.replace('"', ""))
            if typ != "OK":
                raise MailboxError("Mailbox search failed.")
            uids = (data[0] or b"").split()
            if sender_hint:
                hint = sender_hint.lower()
            else:
                hint = ""
            for uid in uids:
                typ, msg_data = box.uid("fetch", uid, "(BODY.PEEK[])")
                if typ != "OK" or not msg_data or not msg_data[0]:
                    continue
                msg = email.message_from_bytes(msg_data[0][1])
                sender = _decode_header(msg.get("From", ""))
                if hint and hint not in sender.lower() and hint not in (msg.get("Subject", "") or "").lower():
                    continue
                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() in ("text/plain", "text/html"):
                            try:
                                body += part.get_payload(decode=True).decode("utf-8", "replace")
                            except (LookupError, UnicodeError):
                                continue
                else:
                    try:
                        body = msg.get_payload(decode=True).decode("utf-8", "replace")
                    except (LookupError, UnicodeError, AttributeError):
                        body = ""
                links = [l.rstrip(".,);") for l in _LINK_RE.findall(body or "")]
                # Prefer verify/confirm/activate links; else first link.
                verify = [l for l in links if re.search(r"verif|confirm|activat|validate|token|code", l, re.IGNORECASE)]
                if links:
                    box.uid("store", uid, "+FLAGS", "(\\Seen)")
                    return (verify or links)[0], sender
            if time.monotonic() >= deadline:
                raise MailboxError(
                    f"No verification mail for {to_addr} within {timeout}s "
                    f"(checked UNSEEN)."
                )
            time.sleep(5)
    finally:
        try:
            box.logout()
        except (imaplib.IMAP4.error, OSError):
            pass
