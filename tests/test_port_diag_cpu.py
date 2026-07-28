"""Tests for port ownership diagnostics (Issue #228).

All tests are CPU-only, no GPU or network services required.
"""

from __future__ import annotations

import json
import socket
import unittest
from unittest.mock import patch

import psutil
from click.testing import CliRunner

from areno.cli.diagnostics import port_check_command
from areno.cli.port_diag import (
    PortDiagnosis,
    diagnose_port,
    format_diagnosis,
)


def _free_port(family: int = socket.AF_INET) -> tuple[int, str]:
    """Allocate and immediately release a port so it is free for the test."""
    host = "127.0.0.1" if family == socket.AF_INET else "::1"
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.bind((host, 0))
    port = sock.getsockname()[1]
    sock.close()
    return port, host


class TestPortAvailable(unittest.TestCase):
    """Port is free and available for binding."""

    def test_available_port_ipv4(self):
        port, host = _free_port(socket.AF_INET)
        diag = diagnose_port(host, port)
        self.assertTrue(diag.available)
        self.assertIsNone(diag.pid)
        self.assertIsNone(diag.bind_error)

    def test_available_port_ipv6(self):
        port, host = _free_port(socket.AF_INET6)
        diag = diagnose_port(host, port)
        self.assertTrue(diag.available)

    def test_suggestion_set_when_available(self):
        port, host = _free_port(socket.AF_INET)
        diag = diagnose_port(host, port)
        self.assertIn("available", diag.suggestion.lower())


class TestPortOccupied(unittest.TestCase):
    """Port is occupied by a process."""

    def test_occupied_by_unrelated_process(self):
        # Use a real occupied port + a mocked psutil lookup so the test
        # is deterministic across macOS / Linux permission differences.
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        try:
            fake_conn = type(
                "FakeConn",
                (),
                {"laddr": type("Addr", (), {"port": port})(), "pid": 4321},
            )()
            fake_proc = type(
                "FakeProc",
                (),
                {
                    "name": lambda self: "nginx",
                    "cmdline": lambda self: ["nginx", "-g", "daemon off"],
                },
            )()
            with patch(
                "areno.cli.port_diag.psutil.net_connections",
                return_value=[fake_conn],
            ):
                with patch(
                    "areno.cli.port_diag.psutil.Process",
                    return_value=fake_proc,
                ):
                    diag = diagnose_port("127.0.0.1", port)
            self.assertFalse(diag.available)
            self.assertEqual(diag.pid, 4321)
            self.assertEqual(diag.process_name, "nginx")
            self.assertFalse(diag.is_areno_child)
            self.assertTrue(
                "change" in diag.suggestion.lower() or "stop" in diag.suggestion.lower()
            )
        finally:
            sock.close()

    def test_occupied_preserves_bind_error(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        try:
            diag = diagnose_port("127.0.0.1", port)
            self.assertIsNotNone(diag.bind_error)
            self.assertGreater(len(diag.bind_error), 0)
        finally:
            sock.close()


class TestEdgeCases(unittest.TestCase):
    """Boundary and failure paths."""

    def test_permission_denied_inspection(self):
        """When process info can't be read, original bind error is preserved."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        try:
            with patch(
                "areno.cli.port_diag.psutil.net_connections",
                side_effect=psutil.AccessDenied,
            ):
                diag = diagnose_port("127.0.0.1", port)
            self.assertFalse(diag.available)
            self.assertIsNotNone(diag.bind_error)
            self.assertIsNone(diag.pid)
        finally:
            sock.close()

    def test_unresolvable_host(self):
        diag = diagnose_port("nonexistent.invalid.host", 8080)
        self.assertFalse(diag.available)
        self.assertIsNotNone(diag.bind_error)

    def test_stale_metadata(self):
        """Port in TIME_WAIT after close – diagnostic should not crash."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()

        diag = diagnose_port("127.0.0.1", port)
        self.assertIsNotNone(diag)

    def test_process_gone_after_lookup(self):
        """Process exits between net_connections and Process() – should skip gracefully."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        try:
            fake_conn = type(
                "FakeConn",
                (),
                {
                    "laddr": type("Addr", (), {"port": port})(),
                    "pid": 999999,  # almost certainly non-existent
                },
            )()
            with patch(
                "areno.cli.port_diag.psutil.net_connections",
                return_value=[fake_conn],
            ):
                with patch(
                    "areno.cli.port_diag.psutil.Process",
                    side_effect=psutil.NoSuchProcess(999999),
                ):
                    diag = diagnose_port("127.0.0.1", port)
            # Should not crash; port is occupied but owner unknown.
            self.assertFalse(diag.available)
            self.assertIsNone(diag.pid)
        finally:
            sock.close()


class TestBackwardCompat(unittest.TestCase):
    """Default behavior unchanged when feature not enabled."""

    def test_diagnosis_does_not_affect_normal_serve(self):
        diag = diagnose_port("127.0.0.1", 0)
        self.assertIsNotNone(diag)


class TestOutput(unittest.TestCase):
    """Output formatting tests."""

    def test_to_dict_has_required_fields(self):
        diag = PortDiagnosis(host="0.0.0.0", port=8080, available=True)
        d = diag.to_dict()
        for key in ("host", "port", "available", "pid", "is_areno_child", "suggestion"):
            self.assertIn(key, d)

    def test_format_diagnosis_human_readable(self):
        diag = PortDiagnosis(
            host="0.0.0.0",
            port=8080,
            available=False,
            pid=12345,
            process_name="python",
            is_areno_child=True,
            suggestion="Reuse it",
        )
        text = format_diagnosis(diag)
        self.assertIn("8080", text)
        self.assertIn("12345", text)
        self.assertIn("occupied", text)

    def test_format_diagnosis_available(self):
        diag = PortDiagnosis(
            host="0.0.0.0",
            port=8080,
            available=True,
            suggestion="Port is available for binding.",
        )
        text = format_diagnosis(diag)
        self.assertIn("available", text)
        self.assertNotIn("PID", text)


class TestCliCommand(unittest.TestCase):
    """``areno port-check`` CLI command tests."""

    def test_port_check_available_port(self):
        port, host = _free_port(socket.AF_INET)
        result = CliRunner().invoke(port_check_command, ["--host", host, "--port", str(port)])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("available", result.output)

    def test_port_check_occupied_port(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        try:
            result = CliRunner().invoke(port_check_command, ["--host", "127.0.0.1", "--port", str(port)])
            self.assertEqual(result.exit_code, 1)
            self.assertIn("occupied", result.output)
        finally:
            sock.close()

    def test_port_check_json_output(self):
        port, host = _free_port(socket.AF_INET)
        result = CliRunner().invoke(
            port_check_command, ["--host", host, "--port", str(port), "--json"]
        )
        self.assertEqual(result.exit_code, 0)
        parsed = json.loads(result.output)
        self.assertTrue(parsed["available"])
        self.assertEqual(parsed["port"], port)

    def test_port_check_lists_in_main_help(self):
        from areno.cli.main import main

        result = CliRunner().invoke(main, ["--help"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("port-check", result.output)


if __name__ == "__main__":
    unittest.main()
