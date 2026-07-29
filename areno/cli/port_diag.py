"""Port ownership diagnostics for AReno serve/proxy startup.

Checks whether a target (host, port) is available for binding.
If occupied, identifies the owning process and distinguishes
AReno child processes from unrelated external services.
Never terminates any process.
"""

from __future__ import annotations

import socket
from dataclasses import asdict, dataclass
from typing import Any

import psutil


@dataclass
class PortDiagnosis:
    """Result of a port ownership diagnostic check."""

    host: str
    port: int
    available: bool
    pid: int | None = None
    process_name: str | None = None
    cmdline: list[str] | None = None
    is_areno_child: bool = False
    bind_error: str | None = None
    suggestion: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Structured output for CLI / machine consumption."""
        return asdict(self)


def diagnose_port(host: str, port: int) -> PortDiagnosis:
    """Diagnose whether ``(host, port)`` is available for binding.

    Steps:
    1. Resolve the address family (IPv4 / IPv6).
    2. Try to bind a socket on ``(host, port)``.
    3. If bind succeeds, the port is available.
    4. If bind fails, look up the owning process via ``psutil``.
    5. Classify the process as an AReno child or an unrelated service.
    6. Return a structured result with a safe, actionable suggestion.

    The function never terminates any process.  If the owning process
    cannot be determined (e.g. permission denied), the original bind
    error is preserved so the caller can surface it.
    """
    # Step 1: Determine address family (IPv4 / IPv6).
    try:
        addr_info = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        return PortDiagnosis(
            host=host,
            port=port,
            available=False,
            bind_error=str(exc),
            suggestion=f"Cannot resolve host '{host}': {exc}",
        )

    # Step 2: Attempt to bind.
    family = addr_info[0][0]
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
        sock.close()
        return PortDiagnosis(
            host=host,
            port=port,
            available=True,
            suggestion="Port is available for binding.",
        )
    except OSError as exc:
        sock.close()
        bind_error = str(exc)

    # Step 3: Look up the owning process.
    owner = _lookup_port_owner(port)
    if owner is not None:
        pid, proc_name, cmdline, is_areno = owner
        if is_areno:
            suggestion = (
                f"Port {port} is held by an AReno process (PID {pid}). "
                "Consider reusing it or stopping the existing AReno run."
            )
        else:
            suggestion = (
                f"Port {port} is held by '{proc_name}' (PID {pid}). "
                "Change the port or stop that process manually."
            )
        return PortDiagnosis(
            host=host,
            port=port,
            available=False,
            pid=pid,
            process_name=proc_name,
            cmdline=cmdline,
            is_areno_child=is_areno,
            bind_error=bind_error,
            suggestion=suggestion,
        )

    # Step 4: Cannot determine owner (permission denied or process gone).
    return PortDiagnosis(
        host=host,
        port=port,
        available=False,
        bind_error=bind_error,
        suggestion=(
            f"Port {port} is occupied but the owning process could not be identified "
            f"(possible permission restriction). Original error: {bind_error}"
        ),
    )


def _lookup_port_owner(port: int) -> tuple[int, str, list[str], bool] | None:
    """Return ``(pid, name, cmdline, is_areno)`` for the process holding *port*.

    Returns ``None`` when the owner cannot be determined.
    """
    try:
        connections = psutil.net_connections(kind="inet")
    except psutil.AccessDenied:
        return None

    for conn in connections:
        if conn.laddr is None or conn.laddr.port != port:
            continue
        if conn.pid is None:
            continue
        try:
            proc = psutil.Process(conn.pid)
            cmdline = proc.cmdline()
            proc_name = proc.name()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        return (conn.pid, proc_name, cmdline, _is_areno_process(cmdline, proc_name))

    return None


def _is_areno_process(cmdline: list[str], proc_name: str) -> bool:
    """Heuristic: check whether a process belongs to AReno."""
    cmdline_str = " ".join(cmdline).lower()
    return "areno" in cmdline_str or "areno" in proc_name.lower()


def format_diagnosis(diag: PortDiagnosis) -> str:
    """Human-readable multi-line output for the terminal."""
    lines = [
        f"Port diagnostic: {diag.host}:{diag.port}",
        f"  Status: {'available' if diag.available else 'occupied'}",
    ]
    if diag.pid is not None:
        lines.append(f"  PID: {diag.pid}")
        lines.append(f"  Process: {diag.process_name}")
        lines.append(f"  AReno child: {'yes' if diag.is_areno_child else 'no'}")
        if diag.cmdline:
            lines.append(f"  Command: {' '.join(diag.cmdline[:5])}")
    if diag.bind_error:
        lines.append(f"  Bind error: {diag.bind_error}")
    if diag.suggestion:
        lines.append(f"  Suggestion: {diag.suggestion}")
    return "\n".join(lines)
