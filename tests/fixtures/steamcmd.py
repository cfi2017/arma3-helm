"""Interactive SteamCMD fixture: real PTY reads, fake authentication/downloads."""
import json
import os
from pathlib import Path
import sys
import time

assert sys.stdin.isatty() and sys.stdout.isatty()
mode = os.environ.get('FAKE_STEAM_MODE', 'code')
home = Path(os.environ['HOME'])
marker = home / 'authenticated'
(home / 'argv.json').write_text(json.dumps(sys.argv))


def output(value):
    sys.stdout.write(value)
    sys.stdout.flush()


if not marker.exists():
    if mode == 'hang':
        output('Steam is connecting...')
        time.sleep(30)
        sys.exit(1)
    output('password: ')
    password = input()
    assert password == 'test-secret-password'
    if mode == 'approval':
        output('Please confirm the login in the Steam Mobile app on your phone.\n')
        while not (home / 'approved').exists():
            time.sleep(0.01)
    else:
        output('Steam Gu')
        time.sleep(0.03)
        output('ard code: ')
        code = input()
        # Deliberately echo sensitive input across reads to test log isolation.
        output(password[:5])
        time.sleep(0.01)
        output(password[5:] + '\n' + code[:2])
        time.sleep(0.01)
        output(code[2:] + '\n')
        while code != 'ABCDE':
            output('Invalid code. Steam Guard code: ')
            code = input()
        if mode == 'exit-auth':
            sys.exit(0)
    marker.write_text('fake saved authentication')
output('Waiting for user info...')
time.sleep(0.03)
output('\x1b[')
time.sleep(0.01)
output('0mOK\n')
if mode == 'slow-download':
    time.sleep(0.5)
if mode == 'hang-download':
    time.sleep(30)
output("Success! App '233780' fully installed.\n")
if mode != 'missing-mod':
    output('Success. Downloaded item 3020755032 to fake-path\n')
