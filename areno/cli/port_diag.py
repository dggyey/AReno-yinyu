"""Port ownership diagnostics for AReno serve/proxy startup.

This module checks whether a target ``(host, port)`` is available for binding.
If the port is occupied, it uses ``psutil`` to identify the owning process,
classifies it as an AReno child process or an unrelated external service,
and returns a safe, actionable suggestion.

Key design constraints:
- **Never terminates any process** — only inspects and reports.
- **Preserves the original bind error** when the owner cannot be determined
  (e.g. insufficient OS permissions).
- **No external dependencies** beyond ``socket`` (stdlib) and ``psutil`` (already
  an AReno dependency).

Typical usage::

    from areno.cli.port_diag import diagnose_port, format_diagnosis

    diag = diagnose_port("0.0.0.0", 8000)
    print(format_diagnosis(diag))
    if not diag.available:
        # Port is occupied — check diag.is_areno_child for guidance.
        ...
"""

from __future__ import annotations

import socket
from dataclasses import asdict, dataclass
from typing import Any

import psutil


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class PortDiagnosis:
    """Structured result of a port ownership diagnostic check.

    A ``PortDiagnosis`` is always returned — whether the port is available or
    occupied.  The caller inspects ``available`` to decide whether to proceed.

    Attributes:
        host: The bind host that was checked (e.g. ``"0.0.0.0"`` or ``"127.0.0.1"``).
        port: The port number that was checked.
        available: ``True`` if the port is free and can be bound; ``False`` if occupied.
        pid: PID of the process holding the port, or ``None`` if unknown.
        process_name: Name of the owning process (e.g. ``"nginx"``, ``"python"``),
            or ``None`` if unknown.
        cmdline: Full command-line argument list of the owning process,
            or ``None`` if unknown.
        is_areno_child: ``True`` if the owning process appears to be an AReno process.
        bind_error: Original OS error message from the failed ``socket.bind`` call
            (e.g. ``"[Errno 98] Address already in use"``), or ``None`` if bind succeeded.
        suggestion: Human-readable actionable suggestion for the user.
    """

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
        """Convert the diagnosis to a dict for JSON output or programmatic use."""
        return asdict(self)


# ---------------------------------------------------------------------------
# Core diagnostic logic
# ---------------------------------------------------------------------------


def diagnose_port(host: str, port: int) -> PortDiagnosis:
    """Diagnose whether ``(host, port)`` is available for binding.

    The function executes the following steps:

    1. **Resolve address family** — ``socket.getaddrinfo`` auto-detects IPv4 / IPv6.
    2. **Attempt to bind** — a temporary socket is opened and ``bind()`` is called.
       If it succeeds, the port is free.
    3. **Look up the owner** — if bind fails, ``psutil.net_connections`` finds the
       process holding the port.
    4. **Classify the process** — the process command line is checked to determine
       whether it belongs to AReno.
    5. **Return a structured result** with a safe, actionable suggestion.

    .. note::
        This function **never terminates any process**.  If the owning process
        cannot be determined (e.g. permission denied), the original bind error
        is preserved so the caller can surface it.

    Args:
        host: Target bind address (e.g. ``"0.0.0.0"``, ``"127.0.0.1"``, ``"::1"``).
        port: Target port number (e.g. ``8000``).

    Returns:
        A :class:`PortDiagnosis` instance containing the port status, owning
        process details (if any), and an actionable suggestion.
    """

    # --- Step 1: Resolve the address family (IPv4 or IPv6) ---
    # socket.getaddrinfo auto-detects the address family, supporting both
    # "127.0.0.1" (IPv4) and "::1" (IPv6).
    try:
        addr_info = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        # Hostname could not be resolved (e.g. user typed a non-existent host).
        return PortDiagnosis(
            host=host,
            port=port,
            available=False,
            bind_error=str(exc),
            suggestion=f"Cannot resolve host '{host}': {exc}",
        )

    # --- Step 2: Attempt to bind a temporary socket ---
    # Extract the address family (AF_INET for IPv4, AF_INET6 for IPv6)
    # and try to bind.  If bind succeeds, the port is free.
    family = addr_info[0][0]
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
        sock.close()
        # Bind succeeded — port is available for use.
        return PortDiagnosis(
            host=host,
            port=port,
            available=True,
            suggestion="Port is available for binding.",
        )
    except OSError as exc:
        # Bind failed — port is occupied or the user lacks permission.
        # Close the socket and preserve the original error message.
        sock.close()
        bind_error = str(exc)

    # --- Step 3: Port is occupied — identify the owning process ---
    # Use psutil to look up which process is holding the port.
    owner = _lookup_port_owner(port)
    if owner is not None:
        pid, proc_name, cmdline, is_areno = owner
        # Provide a tailored suggestion based on whether the owner is AReno
        # or an unrelated external service.
        if is_areno:
            # The port is held by another AReno run — user may want to reuse it.
            suggestion = (
                f"Port {port} is held by an AReno process (PID {pid}). "
                "Consider reusing it or stopping the existing AReno run."
            )
        else:
            # The port is held by an unrelated service (e.g. nginx, another app).
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

    # --- Step 4: Owner cannot be determined ---
    # This happens when psutil lacks permission to inspect the owning process
    # (common for non-root users on Linux) or the process has already exited.
    # Preserve the original bind error so the caller can surface it.
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


# ---------------------------------------------------------------------------
# Process lookup helpers
# ---------------------------------------------------------------------------


def _lookup_port_owner(port: int) -> tuple[int, str, list[str], bool] | None:
    """Find the process that is holding the given *port*.

    Iterates over all IPv4/IPv6 network connections via ``psutil.net_connections``
    and returns the PID, name, command line, and AReno classification for the
    first connection whose local port matches.

    Args:
        port: The port number to look up.

    Returns:
        A tuple ``(pid, process_name, cmdline, is_areno)`` if the owning process
        is found, or ``None`` if the owner cannot be determined (e.g. permission
        denied, or no matching connection exists).
    """
    # Retrieve all inet (IPv4 + IPv6) connections on this host.
    try:
        connections = psutil.net_connections(kind="inet")
    except psutil.AccessDenied:
        # Non-root users may not have permission to enumerate all connections.
        return None

    # Scan connections for one bound to the target port.
    for conn in connections:
        # Skip connections with no local address or a non-matching port.
        if conn.laddr is None or conn.laddr.port != port:
            continue
        # Skip connections without an associated PID (e.g. TIME_WAIT state).
        if conn.pid is None:
            continue
        try:
            # Fetch process details: PID, name, and full command line.
            proc = psutil.Process(conn.pid)
            cmdline = proc.cmdline()
            proc_name = proc.name()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            # The process exited between the net_connections call and now,
            # or we lack permission to inspect it — skip and keep scanning.
            continue
        # Found the owner — classify and return.
        return (conn.pid, proc_name, cmdline, _is_areno_process(cmdline, proc_name))

    return None


def _is_areno_process(cmdline: list[str], proc_name: str) -> bool:
    """Heuristically determine whether a process belongs to AReno.

    Checks whether the string ``"areno"`` appears in the process's command-line
    arguments or process name (case-insensitive).  This is a simple heuristic
    that works for typical AReno invocations such as::

        python -m areno.cli.main serve --model-path ...

    Args:
        cmdline: The process's command-line argument list.
        proc_name: The process name (e.g. ``"python"``).

    Returns:
        ``True`` if ``"areno"`` is found in the command line or process name.
    """
    cmdline_str = " ".join(cmdline).lower()
    return "areno" in cmdline_str or "areno" in proc_name.lower()


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def format_diagnosis(diag: PortDiagnosis) -> str:
    """Format a :class:`PortDiagnosis` as human-readable multi-line text.

    Example output (port available)::

        Port diagnostic: 0.0.0.0:8000
          Status: available
          Suggestion: Port is available for binding.

    Example output (port occupied)::

        Port diagnostic: 0.0.0.0:8000
          Status: occupied
          PID: 4321
          Process: nginx
          AReno child: no
          Command: nginx -g daemon off
          Bind error: [Errno 98] Address already in use
          Suggestion: Port 8000 is held by 'nginx' (PID 4321). ...

    Args:
        diag: A :class:`PortDiagnosis` instance.

    Returns:
        A formatted multi-line string suitable for terminal output.
    """
    lines = [
        f"Port diagnostic: {diag.host}:{diag.port}",
        f"  Status: {'available' if diag.available else 'occupied'}",
    ]

    # If we identified the owning process, show its PID, name, and command line.
    if diag.pid is not None:
        lines.append(f"  PID: {diag.pid}")
        lines.append(f"  Process: {diag.process_name}")
        lines.append(f"  AReno child: {'yes' if diag.is_areno_child else 'no'}")
        if diag.cmdline:
            # Show at most the first 5 arguments to avoid overly long output.
            lines.append(f"  Command: {' '.join(diag.cmdline[:5])}")

    # Show the original bind error (if any) so the user sees the OS-level cause.
    if diag.bind_error:
        lines.append(f"  Bind error: {diag.bind_error}")

    # Show the actionable suggestion.
    if diag.suggestion:
        lines.append(f"  Suggestion: {diag.suggestion}")

    return "\n".join(lines)