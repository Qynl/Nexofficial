"""Structured logging (server.py): level filtering and format.

server.py used to write its [http]/[chat]/[api] diagnostics straight to
stderr with sys.stderr.write(). It now goes through the stdlib `logging`
module instead, configured via logging.basicConfig() exactly once at
import time -- which means testing level filtering honestly requires a
fresh process per scenario (a second basicConfig() call in the same
process is a no-op). These tests boot the real server.py as a subprocess,
the same way an operator actually runs it.
"""
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
NEX = os.path.dirname(HERE)

_FAILED = []


def expect(cond, msg):
    if not cond:
        _FAILED.append(msg)
    print(("ok   - " if cond else "FAIL - ") + msg)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run_server(extra_env, requests=1, wait_s=4.0):
    """Boot the real server.py as a subprocess, hit it `requests` times,
    then kill it and return its combined stdout+stderr text."""
    port = _free_port()
    env = dict(os.environ)
    env["NEX_HOME"] = tempfile.mkdtemp(prefix="nex-logtest-")
    env["NEX_PORT"] = str(port)
    env["NEX_HOST"] = "127.0.0.1"
    env.update(extra_env)
    proc = subprocess.Popen(
        [sys.executable, "-u", "server.py"], cwd=NEX, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        deadline = time.time() + wait_s
        connected = False
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port),
                                              timeout=0.2):
                    connected = True
                    break
            except OSError:
                time.sleep(0.1)
        if connected:
            for _ in range(requests):
                try:
                    urllib.request.urlopen(
                        "http://127.0.0.1:%d/" % port, timeout=2).read()
                except Exception:  # noqa: BLE001
                    pass  # a 401/303 is still a logged request
            time.sleep(0.3)  # give the log line time to flush
        proc.terminate()
        try:
            out, _ = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate(timeout=5)
        return out, connected
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=5)


class LoggingTests(unittest.TestCase):
    def test_default_level_logs_http_access_with_a_structured_format(self):
        out, connected = _run_server({})
        self.assertTrue(connected, "server did not come up:\n" + out)
        expect("server.http" in out,
              "default (INFO) level logs HTTP access lines")
        expect(" INFO " in out, "the log line carries a level name")
        # A line must look like "YYYY-MM-DD HH:MM:SS INFO    server.http: ..."
        lines = [l for l in out.splitlines() if "server.http" in l]
        expect(bool(lines), "found at least one server.http log line")
        if lines:
            parts = lines[0].split()
            expect(len(parts) >= 4 and "-" in parts[0] and ":" in parts[1],
                  "log line starts with a date and a time: %r" % lines[0])

    def test_warning_level_suppresses_routine_http_access_logs(self):
        out, connected = _run_server({"NEX_LOG_LEVEL": "WARNING"})
        self.assertTrue(connected, "server did not come up:\n" + out)
        expect("server.http" not in out,
              "NEX_LOG_LEVEL=WARNING must suppress INFO-level access logs")
        # The startup banner (print(), not logging) must still show up --
        # NEX_LOG_LEVEL only governs the `logging` module, not the
        # deliberately-always-visible startup prints.
        expect("Nex 2.0 serving on" in out,
              "the startup banner is unaffected by NEX_LOG_LEVEL")

    def test_invalid_log_level_falls_back_to_info_without_crashing(self):
        out, connected = _run_server({"NEX_LOG_LEVEL": "not-a-real-level"})
        self.assertTrue(connected,
                        "an invalid NEX_LOG_LEVEL must not prevent startup:"
                        "\n" + out)
        expect("server.http" in out,
              "an invalid NEX_LOG_LEVEL falls back to INFO, not silence")


if __name__ == "__main__":
    unittest.main(verbosity=2, exit=False)
    if _FAILED:
        sys.exit(1)
    print("\nAll logging tests passed.")
