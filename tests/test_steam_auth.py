import contextlib
import io
import json
import os
from pathlib import Path
import pty
import select
import socket
import subprocess
import sys
import tempfile
import termios
import threading
import time
import unittest

sys.path.insert(0, str(Path('charts/arma3/files').resolve()))
import steam_auth

FIXTURE = Path('tests/fixtures/steamcmd.py').resolve()
EXPECTED = ["Success! App '233780' fully installed.", 'Success. Downloaded item 3020755032']


class SteamAuthTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.socket = self.root / 'socket/control.sock'
        self.errors = []
        self.log = io.StringIO()
        self.thread = None
        self.connections = []
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for connection in self.connections:
            connection.close()
        if self.thread is not None and self.thread.is_alive():
            try:
                with socket.socket(socket.AF_UNIX) as connection:
                    connection.connect(str(self.socket))
                    connection.sendall(b'{"type":"cancel"}\n')
            except OSError:
                pass
            self.thread.join(6)

    def start(self, mode='code', auth_timeout=5, download_timeout=5):
        env = os.environ.copy()
        env.update(HOME=str(self.root), FAKE_STEAM_MODE=mode)
        self.errors = []

        def worker():
            try:
                with contextlib.redirect_stdout(self.log):
                    steam_auth.run_session(
                        [sys.executable, str(FIXTURE)], EXPECTED,
                        'test-secret-password', ['test-secret-password'], env,
                        auth_timeout, download_timeout, self.socket)
            except Exception as error:
                self.errors.append(error)
        self.thread = threading.Thread(target=worker, daemon=True)
        self.thread.start()

    def connect(self):
        connection = socket.socket(socket.AF_UNIX)
        deadline = time.monotonic() + 5
        while True:
            try:
                connection.connect(str(self.socket))
                break
            except (FileNotFoundError, ConnectionRefusedError):
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.01)
        connection.settimeout(5)
        self.connections.append(connection)
        return connection

    def event(self, connection, state):
        # Read one byte at a time to avoid discarding subsequent protocol frames.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            line = b''
            while not line.endswith(b'\n'):
                data = connection.recv(1)
                self.assertTrue(data, self.errors)
                line += data
            event = json.loads(line)
            if event['state'] == state:
                return event
        self.fail('Expected auth state ' + state)

    def finish(self, error=None):
        self.thread.join(6)
        self.assertFalse(self.thread.is_alive())
        if error:
            self.assertEqual(len(self.errors), 1)
            self.assertIsInstance(self.errors[0], error)
        else:
            self.assertEqual(self.errors, [])
        self.assertFalse(self.socket.exists())

    def test_code_rejection_disconnect_and_cached_login(self):
        self.start()
        connection = self.connect()
        self.event(connection, 'code')
        self.assertEqual(self.socket.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.socket.parent.stat().st_mode & 0o777, 0o700)
        connection.sendall(b'{"type":"code","value":"WRONG"}\n')
        self.event(connection, 'login')
        self.event(connection, 'code')
        connection.close()
        time.sleep(0.05)
        second = self.connect()
        self.event(second, 'code')
        second.sendall(b'{"type":"code","value":"ABCDE"}\n')
        self.event(second, 'authenticated')
        self.finish()
        for secret in ('test-secret-password', 'WRONG', 'ABCDE'):
            self.assertNotIn(secret, self.log.getvalue())
            self.assertNotIn(secret, (self.root / 'argv.json').read_text())
        self.assertIn('fully installed', self.log.getvalue())
        # Reusing the same home must not need password/code input.
        self.start()
        self.finish()

    def test_mobile_approval(self):
        self.start(mode='approval')
        connection = self.connect()
        self.event(connection, 'approval')
        (self.root / 'approved').touch()
        self.event(connection, 'authenticated')
        self.finish()

    def test_second_client_rejected_and_cancel_cleanup(self):
        self.start()
        first = self.connect()
        self.event(first, 'code')
        second = self.connect()
        self.event(second, 'busy')
        first.sendall(b'{"type":"cancel"}\n')
        self.event(first, 'error')
        self.finish(steam_auth.AuthenticationError)

    def test_auth_timeout(self):
        self.start(mode='hang', auth_timeout=0.2)
        self.finish(steam_auth.AuthenticationError)

    def test_separate_download_timeout_and_missing_success(self):
        for mode, timeout, error in [
            ('slow-download', 2, None),
            ('hang-download', 0.2, steam_auth.DownloadError),
            ('missing-mod', 2, steam_auth.DownloadError),
        ]:
            with self.subTest(mode=mode):
                (self.root / 'authenticated').touch()
                self.start(mode=mode, auth_timeout=0.3, download_timeout=timeout)
                self.finish(error)

    def test_zero_exit_before_login_is_not_success(self):
        self.start(mode='exit-auth')
        connection = self.connect()
        self.event(connection, 'code')
        connection.sendall(b'{"type":"code","value":"ABCDE"}\n')
        self.finish(steam_auth.AuthenticationError)

    def test_attach_terminal_hides_code_and_restores_settings(self):
        self.start()
        deadline = time.monotonic() + 5
        while not self.socket.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        master, slave = pty.openpty()
        saved = termios.tcgetattr(slave)
        env = os.environ.copy()
        env['PYTHONPATH'] = str(Path(steam_auth.__file__).parent)
        helper = subprocess.Popen(
            [sys.executable, '-c', 'import steam_auth,sys; steam_auth.attach(sys.argv[1])', str(self.socket)],
            stdin=slave, stdout=slave, stderr=slave, env=env)
        transcript = b''
        try:
            while b'Steam Guard code (hidden)' not in transcript:
                self.assertTrue(select.select([master], [], [], 5)[0])
                transcript += os.read(master, 8192)
            os.write(master, b'ABCDE\n')
            helper.wait(timeout=5)
            while select.select([master], [], [], 0)[0]:
                transcript += os.read(master, 8192)
            self.assertEqual(helper.returncode, 0, transcript)
            self.assertNotIn(b'ABCDE', transcript)
            self.assertNotIn(b'test-secret-password', transcript)
            self.assertEqual(termios.tcgetattr(slave), saved)
            self.finish()
        finally:
            if helper.poll() is None:
                helper.kill()
                helper.wait()
            os.close(master)
            os.close(slave)
