"""Local, single-client SteamCMD terminal broker. No network listener or code files."""
import codecs
import errno
import fcntl
import json
import os
from pathlib import Path
import pty
import re
import selectors
import signal
import socket
import subprocess
import sys
import termios
import time

SOCKET = Path('/tmp/arma3-auth/control.sock')
ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
PASSWORD = re.compile(r'(?i)password[^\r\n:]{0,120}:\s*')
GUARD = re.compile(r'(?i)(?:steam guard|two.factor|authenticator|authentication code|'
                   r'check your email|code (?:from|sent to) your email|'
                   r'(?:confirm|approve)[^\r\n]{0,100}(?:login|mobile|phone))')
APPROVAL = re.compile(r'(?i)(?:confirm|approve)[^\r\n]{0,100}(?:login|mobile|phone)')
CODE_PROMPT = re.compile(r'(?i)(?:(?:steam guard|two.factor|authentication|authenticator) code\s*:|'
                         r'(?:enter|provide)[^\r\n]{0,160}code)')
LOGGED_IN = re.compile(r'Waiting for user info\.\.\.\s*OK', re.I)


class AuthenticationError(RuntimeError):
    pass


class DownloadError(RuntimeError):
    def __init__(self, message, matched=()):
        super().__init__(message)
        self.matched = frozenset(matched)


def redact(text, secrets):
    for secret in sorted(filter(None, secrets), key=len, reverse=True):
        text = text.replace(secret, '[REDACTED]')
    return text


def terminal_exec(args):
    # Popen creates a new session; acquire the slave as its controlling terminal.
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    attrs = termios.tcgetattr(0)
    attrs[3] &= ~(termios.ECHO | termios.ECHONL)
    termios.tcsetattr(0, termios.TCSANOW, attrs)
    os.execvpe(args[0], args, os.environ)


def run_session(args, expected, password, secrets, env, auth_timeout,
                download_timeout, socket_path=SOCKET):
    """Authenticate once, then run queued commands. Codes stay in process memory.

    Auth output is parsed but never copied to pod logs or client output. Only
    fixed status messages are exposed until login completes; this also avoids
    leaking echoed codes split over multiple PTY reads.
    """
    socket_path = Path(socket_path)
    socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    socket_path.parent.chmod(0o700)
    # Refuse a competing broker, but recover a socket left by a dead process.
    if socket_path.exists():
        with socket.socket(socket.AF_UNIX) as probe:
            try:
                probe.connect(str(socket_path))
            except ConnectionRefusedError:
                socket_path.unlink()
            else:
                raise AuthenticationError('A Steam authentication session is already running')
    listener = socket.socket(socket.AF_UNIX)
    master = slave = None
    process = client = None
    selector = selectors.DefaultSelector()
    state = 'login'
    status = 'Steam login in progress; saved authentication will be used when available.'
    code_buffer = b''
    login_text = ''
    decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
    escape_tail = ''
    output_line = ''
    password_sent = False
    logged_in = False
    matched = set()
    known_secrets = list(secrets)
    auth_deadline = time.monotonic() + auth_timeout
    download_deadline = None

    def disconnect():
        nonlocal client, code_buffer
        if client is not None:
            selector.unregister(client)
            client.close()
            client = None
        code_buffer = b''

    def send(event):
        if client is not None:
            try:
                client.sendall((json.dumps(event) + '\n').encode())
            except (OSError, socket.timeout):
                disconnect()

    def announce(next_state, message):
        nonlocal state, status
        changed = (next_state, message) != (state, status)
        state, status = next_state, message
        if changed:
            print(message, flush=True)
            send({'state': state, 'message': status})

    def log_line(line):
        line = redact(line, known_secrets)
        if line.strip():
            print(line, flush=True)

    try:
        listener.bind(str(socket_path))
        socket_path.chmod(0o600)
        listener.listen(1)
        listener.setblocking(False)
        selector.register(listener, selectors.EVENT_READ, 'listener')
        master, slave = pty.openpty()
        # Disable echo before the child starts, not only after its first prompt.
        attrs = termios.tcgetattr(slave)
        attrs[3] &= ~(termios.ECHO | termios.ECHONL)
        termios.tcsetattr(slave, termios.TCSANOW, attrs)
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), 'exec', *args],
            stdin=slave, stdout=slave, stderr=slave, env=env,
            start_new_session=True, close_fds=True)
        os.close(slave)
        slave = None
        os.set_blocking(master, False)
        selector.register(master, selectors.EVENT_READ, 'terminal')
        print(status, flush=True)
        namespace = os.environ.get('POD_NAMESPACE', '<namespace>')
        pod = os.environ.get('POD_NAME', '<pod-name>')
        print(f'For Steam Guard: kubectl exec -it -n {namespace} {pod} '
              '-c bootstrap -- python3 /chart/runtime.py auth', flush=True)
        terminal_open = True
        while terminal_open or process.poll() is None:
            now = time.monotonic()
            if not logged_in and now >= auth_deadline:
                raise AuthenticationError('Steam authentication timed out; attach on the next bootstrap attempt')
            if logged_in and now >= download_deadline:
                raise DownloadError('Steam downloads timed out')
            for key, _ in selector.select(0.2):
                if key.data == 'listener':
                    connection, _ = listener.accept()
                    connection.settimeout(0.2)
                    if client is not None:
                        try:
                            connection.sendall(b'{"state":"busy","message":"Another authentication terminal is attached."}\n')
                        except OSError:
                            pass
                        connection.close()
                    else:
                        client = connection
                        selector.register(client, selectors.EVENT_READ, 'client')
                        send({'state': state, 'message': status})
                elif key.data == 'client':
                    if client is None or key.fileobj is not client:
                        continue
                    try:
                        data = client.recv(1024)
                    except OSError:
                        data = b''
                    if not data:
                        disconnect()
                        continue
                    code_buffer += data
                    if len(code_buffer) > 2048:
                        disconnect()
                        continue
                    while client is not None and b'\n' in code_buffer:
                        request, code_buffer = code_buffer.split(b'\n', 1)
                        try:
                            request = json.loads(request)
                        except (ValueError, UnicodeDecodeError):
                            disconnect()
                            break
                        if not isinstance(request, dict):
                            disconnect()
                            break
                        if request.get('type') == 'cancel':
                            raise AuthenticationError('Steam authentication cancelled by operator')
                        value = request.get('value', '')
                        if (request.get('type') != 'code' or state != 'code'
                                or not isinstance(value, str)
                                or not re.fullmatch(r'[A-Za-z0-9]{5,10}', value)):
                            send({'state': state, 'message': 'No code accepted. Wait for a code prompt; use 5–10 letters/digits.'})
                            continue
                        known_secrets.extend((value, value.upper(), value.lower()))
                        os.write(master, value.encode() + b'\n')
                        # Discard earlier prompts so a repeated prompt is a new event.
                        login_text = ''
                        announce('login', 'Steam Guard code submitted; waiting for Steam.')
                else:
                    try:
                        data = os.read(master, 8192)
                    except OSError as error:
                        if error.errno != errno.EIO:
                            raise
                        data = b''
                    if not data:
                        selector.unregister(master)
                        terminal_open = False
                        continue
                    text = escape_tail + decoder.decode(data)
                    escape_tail = ''
                    partial = re.search(r'\x1b(?:\[[0-?]*[ -/]*)?$', text)
                    if partial:
                        escape_tail = text[partial.start():]
                        text = text[:partial.start()]
                    # Keep only text; authentication output is never forwarded raw.
                    text = ANSI.sub('', text).replace('\r', '\n')
                    if not logged_in:
                        login_text = (login_text + text)[-32768:]
                        success = LOGGED_IN.search(login_text)
                        if success:
                            logged_in = True
                            download_deadline = time.monotonic() + download_timeout
                            announce('authenticated', 'Steam authentication succeeded; downloading configured content.')
                            text = login_text[success.end():]
                            login_text = ''
                        else:
                            prompt = PASSWORD.search(login_text)
                            if prompt:
                                if password_sent:
                                    raise AuthenticationError('Steam requested the password again; check the existing Secret')
                                if not password or any(c in password for c in '\r\n\x00'):
                                    raise AuthenticationError('Steam password is empty or contains unsupported control characters')
                                os.write(master, password.encode() + b'\n')
                                password_sent = True
                                login_text = login_text[prompt.end():]
                            if GUARD.search(login_text):
                                if APPROVAL.search(login_text):
                                    announce('approval', 'Waiting for Steam Guard approval. Check the Steam Mobile app.')
                                elif CODE_PROMPT.search(login_text):
                                    announce('code', 'Waiting for Steam Guard authentication. Attach and enter the current email/authenticator code.')
                                else:
                                    announce('guard', 'Steam Guard challenge detected; waiting for Steam to request a code or app approval.')
                            continue
                    # Match complete success messages even across reads. Buffered
                    # complete lines also prevent partial-secret logging.
                    output_line += text
                    while '\n' in output_line:
                        line, output_line = output_line.split('\n', 1)
                        for message in expected:
                            if message in line:
                                matched.add(message)
                        log_line(line)
                    if len(output_line) > 65536:
                        output_line = ''
                        log_line('[Steam output line exceeded 64 KiB; omitted]')
        if not logged_in:
            for pattern, message in (
                (r'invalid.?password', 'Steam rejected the password; check the existing Secret'),
                (r'rate.?limit|too many', 'Steam rate-limited login; wait before retrying'),
                (r'invalid.*(?:auth|guard|code)|(?:code|auth).*mismatch',
                 'Steam rejected the authentication code; enter a fresh code on the next attempt'),
            ):
                if re.search(pattern, login_text, re.I):
                    raise AuthenticationError(message)
            raise AuthenticationError('Steam login failed before authentication completed; check credentials, Steam connectivity or Guard approval')
        for message in expected:
            if message in output_line:
                matched.add(message)
        log_line(output_line)
        if process.wait() != 0 or len(matched) != len(set(expected)):
            raise DownloadError(
                'Steam did not confirm all downloads; check the download logs and account access',
                matched)
        send({'state': 'done', 'message': 'Steam downloads completed.'})
    except (AuthenticationError, DownloadError) as error:
        send({'state': 'error', 'message': str(error)})
        raise
    finally:
        if process is not None:
            # Kill the entire session, including SteamCMD shell/self-update children.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        disconnect()
        selector.close()
        listener.close()
        for fd in (master, slave):
            if fd is not None:
                os.close(fd)
        socket_path.unlink(missing_ok=True)


def attach(socket_path=SOCKET, cancel=False):
    """Attach without terminal echo. Ctrl-C/D detaches; --cancel stops the session."""
    with socket.socket(socket.AF_UNIX) as connection:
        try:
            connection.connect(str(socket_path))
        except (FileNotFoundError, ConnectionRefusedError):
            raise AuthenticationError('No active Steam session. Check bootstrap logs and the pod name.') from None
        if cancel:
            connection.sendall(b'{"type":"cancel"}\n')
            print('Cancellation requested. Kubernetes may restart the init container.')
            return
        if not sys.stdin.isatty():
            raise AuthenticationError('A terminal is required: use kubectl exec -it')
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        selector = selectors.DefaultSelector()
        pending = b''
        entered = ''
        state = 'login'
        attrs = termios.tcgetattr(fd)
        attrs[3] &= ~(termios.ECHO | termios.ECHONL | termios.ICANON)
        attrs[6][termios.VMIN] = 1
        attrs[6][termios.VTIME] = 0
        try:
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
            selector.register(connection, selectors.EVENT_READ, 'server')
            selector.register(fd, selectors.EVENT_READ, 'input')
            print('Connected. Input is hidden. Ctrl-C/D detaches without cancelling login.', flush=True)
            while True:
                for key, _ in selector.select():
                    if key.data == 'server':
                        data = connection.recv(8192)
                        if not data:
                            print('\nSteam session closed; check bootstrap logs.')
                            return
                        pending += data
                        while b'\n' in pending:
                            line, pending = pending.split(b'\n', 1)
                            event = json.loads(line)
                            state = event['state']
                            print('\n' + event['message'], flush=True)
                            if state == 'code':
                                print('Steam Guard code (hidden): ', end='', flush=True)
                            if state in ('authenticated', 'done'):
                                return
                            if state in ('error', 'busy'):
                                raise AuthenticationError(event['message'])
                    else:
                        data = os.read(fd, 1024)
                        if not data or b'\x04' in data:
                            return
                        for char in data.decode('ascii', errors='ignore'):
                            if char in '\r\n':
                                if entered and state == 'code':
                                    request = {'type': 'code', 'value': entered}
                                    connection.sendall((json.dumps(request) + '\n').encode())
                                entered = ''
                            elif char in '\x7f\b':
                                entered = entered[:-1]
                            elif char.isalnum() and len(entered) < 10 and state == 'code':
                                entered += char
        except KeyboardInterrupt:
            print('\nDetached; Steam authentication is still waiting.')
        finally:
            termios.tcsetattr(fd, termios.TCSANOW, saved)
            selector.close()


if __name__ == '__main__':
    if sys.argv[1] == 'exec':
        terminal_exec(sys.argv[2:])
