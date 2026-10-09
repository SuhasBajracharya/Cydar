"""Local recon runner: nmap + gobuster via subprocess.

Safety rules (this runs shell-free on the box hosting Django):
- No `shell=True` anywhere; argv lists only, so a malicious domain can't
  inject extra flags/commands.
- The target host is strictly validated (hostname or IPv4 only) and the
  scheme/path a user might paste ("https://example.com/foo") is stripped
  before use — the raw input never reaches argv.
- Flag sets are fixed per profile, gated by the target's automation level:
    read-only -> nmap fast ports only, gobuster common list only.
    standard  -> + nmap service versions.
    aggressive-> + nmap default scripts (-sC).
  Gobuster does `dir` or `vhost` mode against https://<host> with a
  server-side wordlist; the client can't pick arbitrary binaries, URLs,
  or wordlists.
- Hard timeouts + output truncation so one scan can't hang the worker or
  blow up the DB row.
"""

import ipaddress
import re
import shlex
import shutil
import subprocess

# Hostname like "admin.shopify.com" (also accepts a pasted URL — the
# scheme/path is stripped before this regex sees it).
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
    r"(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$"
)

NMAP_TIMEOUT_SECONDS = 180
GOBUSTER_TIMEOUT_SECONDS = 180
MAX_OUTPUT_CHARS = 30_000

# First existing file wins; dirb/common.txt (4614 entries) is the fallback.
GOBUSTER_WORDLISTS = {
    "common": [
        "/usr/share/wordlists/seclists/Discovery/Web-Content/common.txt",
        "/usr/share/wordlists/dirb/common.txt",
        "/usr/share/wordlists/wfuzz/general/common.txt",
    ],
}

# Subdomain candidates for vhost fuzzing / DNS brute force. First existing
# file wins; fierce-hostlist.txt is small (~1800 entries) so it doubles as
# the fallback when seclists DNS lists are absent.
SUBDOMAIN_WORDLISTS = [
    "/usr/share/wordlists/seclists/Discovery/DNS/subdomains-top1million-5000.txt",
    "/usr/share/wordlists/seclists/Discovery/DNS/fierce-hostlist.txt",
    "/usr/share/wordlists/seclists/Discovery/DNS/namelist.txt",
]

# profile -> (argv flags, min automation level). Levels rank:
# read-only < standard < aggressive.
NMAP_PROFILES = {
    "fast": (["-T4", "-F"], "read-only"),
    "services": (["-T4", "-sV", "--top-ports", "1000"], "standard"),
    "scripts": (["-T4", "-sV", "-sC", "--top-ports", "1000"], "aggressive"),
}

GOBUSTER_PROFILES = {
    "common": ("common", "read-only"),
    "vhost": ("vhost", "read-only"),
}

_LEVEL_RANK = {"read-only": 0, "standard": 1, "aggressive": 2}


class ReconError(Exception):
    """Bad input or tool failure (surfaced as HTTP 400/502)."""


def clean_host(raw):
    """Strip scheme/path/port clutter, return a validated hostname or IPv4.

    Private RFC1918 ranges (10/8, 172.16/12, 192.168/16 — e.g. THM/HTB
    VPN boxes) are allowed. Loopback/link-local/multicast/unspecified
    still refuse since those target the scanner box itself.
    Raises ReconError on anything that isn't a plain target.
    """
    text = (raw or "").strip().lower()
    # Accept pasted URLs: "https://admin.shopify.com/shop?q=1" -> host part.
    text = re.sub(r"^[a-z][a-z0-9+.-]*://", "", text)
    text = text.split("/")[0].split("?")[0].split("#")[0]
    # Strip trailing port (but not IPv6 — rejected below anyway).
    if text.count(":") == 1:
        text, _, port = text.partition(":")
        if not port.isdigit():
            raise ReconError(f"Invalid target {raw!r}: bad port.")
    if not text:
        raise ReconError("Empty scan target.")
    try:
        ip = ipaddress.ip_address(text)
        if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
            raise ReconError(
                f"Refusing to scan {text}: loopback/link-local addresses are out of scope."
            )
        if isinstance(ip, ipaddress.IPv6Address):
            raise ReconError(f"IPv6 targets not supported: {text}.")
        return text
    except ValueError:
        pass  # not an IP — validate as hostname below
    if not _HOSTNAME_RE.match(text):
        raise ReconError(f"Invalid target host {raw!r}.")
    if text in ("localhost",) or text.endswith(".local"):
        raise ReconError(f"Refusing to scan local host {text!r}.")
    return text


def _check_binary(name):
    if shutil.which(name) is None:
        raise ReconError(f"{name} is not installed on the server (missing binary).")
    return name


def _level_ok(required, actual):
    return _LEVEL_RANK.get(actual, 1) >= _LEVEL_RANK.get(required, 0)


def _run(argv, timeout):
    """Run argv (no shell), return (output, timed_out)."""
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError as exc:
        raise ReconError(f"Scanner binary missing: {exc}.")
    except subprocess.TimeoutExpired as exc:
        partial = (exc.stdout or "") + (exc.stderr or "")
        return partial, True
    out = proc.stdout or ""
    if proc.returncode != 0 and not out:
        out = proc.stderr or f"{argv[0]} exited with code {proc.returncode}."
    elif proc.stderr and not out:
        out = proc.stderr
    return out, False


def _truncate(output):
    if len(output) > MAX_OUTPUT_CHARS:
        return output[:MAX_OUTPUT_CHARS] + "\n…[truncated]", True
    return output, False


def allowed_presets(automation_level):
    """Preset ids the UI may offer for a target, e.g. ["nmap-fast", ...]."""
    out = []
    for name, (_, minimum) in NMAP_PROFILES.items():
        if _level_ok(minimum, automation_level):
            out.append(f"nmap-{name}")
    for name, (_, minimum) in GOBUSTER_PROFILES.items():
        if _level_ok(minimum, automation_level):
            out.append(f"gobuster-{name}")
    return out


def run_preset(preset, raw_target, automation_level="standard", insecure=False):
    """Run one scan preset. Returns (command_str, output, truncated).

    Raises ReconError on bad input, missing tool, or timeout with no output.
    """
    host = clean_host(raw_target)
    if preset.startswith("nmap-"):
        profile = preset[len("nmap-"):]
        spec = NMAP_PROFILES.get(profile)
        if spec is None:
            raise ReconError(f"Unknown nmap preset {preset!r}.")
        flags, minimum = spec
        if not _level_ok(minimum, automation_level):
            raise ReconError(
                f"Preset {preset!r} needs automation level "
                f"'{minimum}' (target is '{automation_level}')."
            )
        _check_binary("nmap")
        argv = ["nmap", *flags, host]
        output, timed_out = _run(argv, NMAP_TIMEOUT_SECONDS)
    elif preset.startswith("gobuster-"):
        profile = preset[len("gobuster-"):]
        spec = GOBUSTER_PROFILES.get(profile)
        if spec is None:
            raise ReconError(f"Unknown gobuster preset {preset!r}.")
        _, minimum = spec
        if not _level_ok(minimum, automation_level):
            raise ReconError(
                f"Preset {preset!r} needs automation level "
                f"'{minimum}' (target is '{automation_level}')."
            )
        _check_binary("gobuster")
        url = f"https://{host}"
        if profile == "vhost":
            # Vhost fuzzing: Host-header candidates <word>.<domain>; only
            # subdomains of the target can match, so findings stay in scope.
            wordlist = next(
                (p for p in SUBDOMAIN_WORDLISTS if shutil.os.path.exists(p)),
                None,
            )
            if wordlist is None:
                raise ReconError("No subdomain wordlist found on the server.")
            argv = [
                "gobuster", "vhost",
                "-u", url,
                "-w", wordlist,
                "--append-domain",
                "-q", "-t", "20", "--timeout", "10s",
            ]
        else:
            wordlist = next(
                (p for p in GOBUSTER_WORDLISTS["common"] if shutil.os.path.exists(p)),
                None,
            )
            if wordlist is None:
                raise ReconError("No gobuster wordlist found on the server.")
            argv = [
                "gobuster", "dir",
                "-u", url,
                "-w", wordlist,
                "-q", "-t", "20", "--timeout", "10s",
            ]
        if insecure:
            argv.append("-k")
        output, timed_out = _run(argv, GOBUSTER_TIMEOUT_SECONDS)
    else:
        raise ReconError(
            f"Unknown preset {preset!r}. Valid: {sorted(allowed_presets(automation_level))}."
        )

    command = shlex.join(argv)
    if timed_out and not (output or "").strip():
        raise ReconError(f"Scan timed out with no output ({command}).")
    output = output or "(no output)"
    if timed_out:
        output += f"\n[scan hit the {NMAP_TIMEOUT_SECONDS}s timeout — output is partial]"
    output, truncated = _truncate(output)
    return command, output, truncated
