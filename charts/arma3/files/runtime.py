"""Bootstrap the SteamCMD image, then exec Arma directly for graceful shutdown."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time

ROOT = Path('/arma3')
WORKSHOP = ROOT / 'steamapps/workshop/content/107410'
SETTINGS = Path('/chart/settings.json')


def cfg_string(value):
    value = str(value)
    if any(c in value for c in '\r\n\x00'):
        raise ValueError('Config strings cannot contain newlines or NUL')
    return '"' + value.replace('"', '""') + '"'


def atomic_write(path, text, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(text)
    tmp.chmod(mode)
    tmp.replace(path)


def lower_tree(root):
    # Arma on Linux needs lowercase mod paths, including addons and keys.
    for parent, dirs, files in os.walk(root, topdown=False):
        for name in files + dirs:
            source = Path(parent) / name
            if source.is_symlink():
                raise RuntimeError('Workshop content must not contain symlinks')
            target = source.with_name(name.lower())
            if source != target:
                if target.exists():
                    raise RuntimeError('Case-colliding Workshop paths: ' + str(source))
                source.rename(target)


def mod_valid(path):
    return path.is_dir() and any(path.rglob('*.pbo'))


def steamcmd(settings, commands, expected):
    opts = settings['bootstrap']
    secrets = [os.environ.get(k, '') for k in
               ('STEAM_USER', 'STEAM_PASSWORD', 'STEAM_BRANCH_PASSWORD')]
    if not secrets[0] or not secrets[1]:
        raise ValueError('Steam username and password must not be empty')
    args = ['/steamcmd/steamcmd.sh', '+@ShutdownOnFailedCommand', '1',
            '+@NoPromptForPassword', '1', '+force_install_dir', str(ROOT),
            '+login', secrets[0], secrets[1], *commands, '+quit']
    for attempt in range(opts['retries']):
        print(f'Steam operation attempt {attempt + 1}/{opts["retries"]}', flush=True)
        try:
            with subprocess.Popen(args, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True,
                                  errors='replace', start_new_session=True) as process:
                try:
                    output, _ = process.communicate(timeout=opts['timeoutSeconds'])
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate()
                    raise
                # SteamCMD can return zero even when a Workshop download fails.
                success = process.returncode == 0 and expected in output
        except subprocess.TimeoutExpired:
            output = 'SteamCMD timed out; retrying.'
            success = False
        for secret in sorted(filter(None, secrets), key=len, reverse=True):
            output = output.replace(secret, '[REDACTED]')
        print(output, flush=True)
        if success:
            return
        if attempt + 1 < opts['retries']:
            time.sleep(opts['retryDelaySeconds'])
    # Do not raise CalledProcessError: its command contains Steam credentials.
    raise RuntimeError('Steam operation failed; check bootstrap logs, credentials and disk space')


def generate_config(server):
    if server['existingConfigSecret']:
        return Path('/server-config/server.cfg').read_text()
    admin = os.environ['ADMIN_PASSWORD']
    if not admin:
        raise ValueError('ADMIN_PASSWORD must not be empty')
    lines = [
        'hostname = ' + cfg_string(server['hostname']) + ';',
        'passwordAdmin = ' + cfg_string(admin) + ';',
        'password = ' + cfg_string(os.environ.get('SERVER_PASSWORD', '')) + ';',
        f'maxPlayers = {server["maxPlayers"]};',
        f'persistent = {int(server["persistent"])};',
        f'BattlEye = {int(server["battleye"])};',
        f'verifySignatures = {server["verifySignatures"]};',
        f'allowedFilePatching = {server["allowedFilePatching"]};',
        f'steamQueryPort = {server["port"] + 1};',
        'voteThreshold = 1.5;',
        'admins[] = {' + ', '.join(cfg_string(x) for x in server['admin']['uids']) + '};',
    ]
    mission = server['mission']
    if mission['template']:
        lines += ['class Missions {', '  class Antistasi {',
                  '    template = ' + cfg_string(mission['template']) + ';',
                  '    difficulty = ' + cfg_string(mission['difficulty']) + ';',
                  '    class Params {']
        for name, value in mission['parameters'].items():
            if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name):
                raise ValueError('Invalid mission parameter name')
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError('Mission parameters must be numbers')
            lines.append(f'      {name} = {value};')
        lines += ['    };', '  };', '};']
    lines += [server['extraConfig']]
    return '\n'.join(lines) + '\n'


def mod_paths(settings, server_only=False):
    mods = settings['mods']
    ids = mods['serverWorkshop' if server_only else 'workshop']
    local = mods['serverLocal' if server_only else 'local']
    return [WORKSHOP / item for item in ids] + [ROOT / item for item in local]


def bootstrap(settings):
    os.chdir(ROOT)
    for directory in ('configs/profiles', 'mpmissions', 'keys', '.chart'):
        (ROOT / directory).mkdir(parents=True, exist_ok=True)
    # Fail early on bad config, before downloading gigabytes.
    config = generate_config(settings['server'])
    opts, server = settings['bootstrap'], settings['server']
    game_marker = ROOT / '.chart/game.json'
    game_identity = json.dumps({'branch': settings['steam']['branch'],
                                'cdlc': server['cdlc']}, sort_keys=True)
    binary = ROOT / server['binary']
    if (opts['updatePolicy'] == 'always' or not binary.is_file()
            or not game_marker.exists() or game_marker.read_text() != game_identity):
        game_marker.unlink(missing_ok=True)
        commands = ['+app_update', '233780', '-beta', settings['steam']['branch']]
        if os.environ.get('STEAM_BRANCH_PASSWORD'):
            commands += ['-betapassword', os.environ['STEAM_BRANCH_PASSWORD']]
        if opts['validate']:
            commands += ['validate']
        steamcmd(settings, commands, "Success! App '233780' fully installed.")
        if not binary.is_file():
            raise RuntimeError('Server binary missing after installation: ' + str(binary))
        atomic_write(game_marker, game_identity)
    for item in settings['mods']['workshop'] + settings['mods']['serverWorkshop']:
        path = WORKSHOP / item
        marker = path / '.chart-ready'
        if opts['updatePolicy'] == 'always' or not marker.exists() or not mod_valid(path):
            marker.unlink(missing_ok=True)
            command = ['+workshop_download_item', '107410', item]
            if opts['validate']:
                command += ['validate']
            steamcmd(settings, command, f'Success. Downloaded item {item}')
            lower_tree(path)
            if not mod_valid(path):
                raise RuntimeError('Workshop item has no PBOs; use individual mod IDs, not collections: ' + item)
            atomic_write(marker, 'ready\n')
    # Remove only previously managed keys; retain Bohemia's installed keys.
    key_manifest = ROOT / '.chart/keys.json'
    old_keys = json.loads(key_manifest.read_text()) if key_manifest.exists() else {}
    for name, digest in old_keys.items():
        key = ROOT / 'keys' / name
        if key.is_file() and hashlib.sha256(key.read_bytes()).hexdigest() == digest:
            key.unlink()
    managed = {}
    for path in mod_paths(settings) + mod_paths(settings, True):
        if not mod_valid(path):
            raise RuntimeError('Mod directory missing or contains no lowercase PBOs: ' + str(path))
        for key in path.rglob('*.bikey'):
            destination = ROOT / 'keys' / key.name.lower()
            if destination.exists() and destination.read_bytes() != key.read_bytes():
                raise RuntimeError('Conflicting signature key: ' + key.name)
            if not destination.exists() or destination.name in managed:
                shutil.copyfile(key, destination)
                managed[destination.name] = hashlib.sha256(key.read_bytes()).hexdigest()
    atomic_write(key_manifest, json.dumps(managed))
    # SteamCMD self-updates only in the init container. Keep its Steam SDK
    # libraries for the separate game container's Steam authentication.
    for bits in ('32', '64'):
        library = Path('/steamcmd') / ('linux' + bits) / 'steamclient.so'
        if library.is_file():
            sdk = ROOT / '.chart' / ('sdk' + bits)
            sdk.mkdir(exist_ok=True)
            shutil.copyfile(library, sdk / 'steamclient.so')
    atomic_write(ROOT / 'configs/server.cfg', config)
    print('Bootstrap complete. Campaign profiles remain in /arma3/configs/profiles.', flush=True)


def server_args(settings):
    s = settings['server']
    args = [s['binary'], '-config=/arma3/configs/server.cfg',
            '-profiles=/arma3/configs/profiles', '-name=' + s['profile'],
            '-port=' + str(s['port']), '-world=' + s['world'],
            '-limitFPS=' + str(s['limitFPS'])]
    client = [str(x) for x in mod_paths(settings)] + s['cdlc']
    server = [str(x) for x in mod_paths(settings, True)]
    if client:
        args.append('-mod=' + ';'.join(client))
    if server:
        args.append('-serverMod=' + ';'.join(server))
    return args + s['extraArgs']


def health(settings):
    # UDP bind check, not a gameplay or mission-state check.
    port = f'{settings["server"]["port"]:04X}'
    for table in ('/proc/net/udp', '/proc/net/udp6'):
        if Path(table).exists():
            for line in Path(table).read_text().splitlines()[1:]:
                if line.split()[1].split(':')[-1] == port:
                    return True
    return False


def main():
    settings = json.loads(SETTINGS.read_text())
    action = sys.argv[1]
    if action == 'bootstrap':
        bootstrap(settings)
    elif action == 'run':
        os.chdir(ROOT)
        for bits in ('32', '64'):
            library = ROOT / '.chart' / ('sdk' + bits) / 'steamclient.so'
            if library.is_file():
                sdk = Path.home() / '.steam' / ('sdk' + bits)
                sdk.mkdir(parents=True, exist_ok=True)
                link = sdk / 'steamclient.so'
                if not link.exists():
                    link.symlink_to(library)
        os.execv(settings['server']['binary'], server_args(settings))
    elif action == 'stop':
        # Keep preStop running so kubelet does not immediately follow SIGINT
        # with SIGTERM while Arma is shutting down. Pod grace bounds this wait.
        try:
            os.kill(1, signal.SIGINT)
            while True:
                os.kill(1, 0)
                time.sleep(1)
        except ProcessLookupError:
            return
    elif action == 'health':
        sys.exit(0 if health(settings) else 1)
    else:
        raise ValueError('Unknown action')


if __name__ == '__main__':
    main()
