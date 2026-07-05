"""VPN integration.

When the user passes ``--ovpn <config>`` the CLI re-execs itself under
``vpn-run.sh`` (which sets up an isolated network namespace + OpenVPN tunnel).
Only this process's traffic flows through the VPN; the rest of the system is
untouched. ``vpn-run.sh`` drops privileges back to the original user, so the
actual download still runs unprivileged.

The re-exec sets ``OSU_VPN_ACTIVE=1`` so the child knows it is already inside
the namespace and should not re-exec again — instead it prints a banner showing
the VPN exit IP and waits 5s before continuing.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import NoReturn

import httpx

from .richio import console

_VPN_RUN_SH = "vpn-run.sh"


def inside_vpn() -> bool:
    return os.environ.get("OSU_VPN_ACTIVE") == "1"


def _resolve_vpn_run_sh() -> Path:
    here = Path(__file__).resolve().parent
    for cand in (here.parent.parent / _VPN_RUN_SH, Path.cwd() / _VPN_RUN_SH):
        if cand.is_file():
            return cand
    found = shutil.which(_VPN_RUN_SH)
    if found:
        return Path(found)
    raise FileNotFoundError(
        f"could not find {_VPN_RUN_SH} next to the project or on PATH"
    )


def _build_creds_file(creds_path: Path) -> tuple[str, str]:
    """Parse a username=/password= .env (or plain user\\npass) file into a
    temporary OpenVPN auth-user-pass file. Returns (path, kind) where kind is
    'reuse' (the file already had the right format) or 'temp'."""
    text = creds_path.read_text().strip()
    if "username=" in text or "password=" in text:
        user = ""
        pw = ""
        for line in text.splitlines():
            if line.startswith("username="):
                user = line.split("=", 1)[1].strip()
            elif line.startswith("password="):
                pw = line.split("=", 1)[1].strip()
        if not user or not pw:
            raise ValueError(
                f"{creds_path}: found username=/password= keys but values empty"
            )
        import tempfile

        fd, tmp = tempfile.mkstemp(prefix="osu-vpn-cred-", suffix=".txt")
        with os.fdopen(fd, "w") as fh:
            fh.write(f"{user}\n{pw}\n")
        os.chmod(tmp, 0o600)
        return tmp, "temp"
    # Assume it's already in OpenVPN format (user\npass).
    return str(creds_path), "reuse"


def re_exec_under_vpn(ovpn: Path, creds: Path, argv: list[str]) -> NoReturn:
    """Re-exec the current command under the VPN namespace wrapper.

    ``argv`` is the full sys.argv-style list (including the program name). The
    ``--ovpn``/``--vpn-creds`` flags are stripped so the child doesn't loop.
    """
    script = _resolve_vpn_run_sh()
    creds_file, _ = _build_creds_file(creds)

    console.print(f"[cyan]VPN[/] starting OpenVPN tunnel via [bold]{script.name}[/] ...")
    console.print(f"  config: {ovpn}")
    console.print(f"  creds:  {creds}")

    # Pass PATH through to sudo via SUDO_PATH (vpn-run.sh restores it) so
    # nix-shell-provided tools (openvpn, ip, iptables) are found as root.
    env = dict(os.environ)
    env["SUDO_PATH"] = os.environ.get("PATH", "")
    cmd = ["sudo", "-E", str(script), str(ovpn), creds_file, "--", *argv]
    try:
        result = subprocess.run(cmd, check=False, env=env)
    except KeyboardInterrupt:
        console.print("[yellow]interrupted[/]")
        sys.exit(130)
    finally:
        if creds_file != str(creds):
            try:
                os.unlink(creds_file)
            except OSError:
                pass
    sys.exit(result.returncode)


def _fetch_exit_ip() -> str:
    """Fetch the public IP as seen by this process (i.e. the VPN exit IP when
    inside the namespace)."""
    for url in ("https://api.ipify.org?format=text", "https://ifconfig.me/ip"):
        try:
            resp = httpx.get(url, timeout=10.0, headers={"User-Agent": "osu-downloader"})
            if resp.status_code == 200:
                return resp.text.strip()
        except httpx.HTTPError:
            continue
    return "(unknown)"


def print_vpn_banner() -> None:
    """Print a banner with the VPN exit IP, then wait 5 seconds."""
    console.print()
    console.print("[bold cyan]══════════════════════════════════════════════════[/]")
    console.print("[bold cyan] VPN active[/] — traffic routed through tunnel")
    ip = _fetch_exit_ip()
    console.print(f"[bold cyan] Exit IP:[/] [bold yellow]{ip}[/]")
    console.print("[bold cyan]══════════════════════════════════════════════════[/]")
    console.print()
    console.print("[dim]Starting in 5 seconds (Ctrl-C to abort)...[/]")
    for i in range(5, 0, -1):
        console.print(f"[dim]\r  {i}...  \r[/]", end="")
        time.sleep(1)
    console.print("[dim]\r          \r[/]", end="")
