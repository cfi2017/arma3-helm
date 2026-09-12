"""Bootstrap the SteamCMD image, then exec Arma directly for graceful shutdown."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import struct
import stat
import sys
import time

import steam_auth

ROOT = Path('/arma3')
WORKSHOP = ROOT / 'steamapps/workshop/content/107410'
SETTINGS = Path('/chart/settings.json')
STEAMCMD_SOURCE = Path('/steamcmd')
STEAM_STATE = Path('/var/lib/arma3-steam')
STEAMCMD = STEAM_STATE / 'steamcmd'


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


def prepare_steamcmd():
    # The pinned image ships the launcher and native binary as UID 5020,
    # mode 0764. Even root cannot execute those with capabilities dropped.
    # A private copy is owned by our UID and also permits Steam self-updates.
    marker = STEAMCMD / '.chart-ready'
    if marker.exists():
        return
    shutil.copytree(STEAMCMD_SOURCE, STEAMCMD, dirs_exist_ok=True)
    # Read-only bundled libraries must also be replaceable by self-updates.
    for path in STEAMCMD.rglob('*'):
        path.chmod(path.stat().st_mode | stat.S_IWUSR)
    for name in ('steamcmd.sh', 'linux32/steamcmd'):
        executable = STEAMCMD / name
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    atomic_write(marker, 'ready\n')


def steamcmd(settings, commands, expected):
    opts = settings['bootstrap']
    username = os.environ.get('STEAM_USER', '')
    password = os.environ.get('STEAM_PASSWORD', '')
    secrets = [username, password, os.environ.get('STEAM_BRANCH_PASSWORD', '')]
    if not username or not password:
        raise ValueError('Steam username and password must not be empty')
    STEAM_STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    STEAM_STATE.chmod(0o700)
    home = STEAM_STATE / 'home'
    home.mkdir(exist_ok=True, mode=0o700)
    home.chmod(0o700)
    env = os.environ.copy()
    # Give Steam a real, persistent home without changing the game process's home.
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / '.config'),
               XDG_DATA_HOME=str(home / '.local/share'), LANG='C', LC_ALL='C')
    # The game/admin credentials are not needed by SteamCMD's child process.
    for key in ('STEAM_PASSWORD', 'STEAM_BRANCH_PASSWORD', 'ADMIN_PASSWORD', 'SERVER_PASSWORD'):
        env.pop(key, None)
    with (STEAM_STATE / 'session.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another Steam session is using this authentication volume') from None
        prepare_steamcmd()
        # Username-only login reuses saved auth where Steam allows it. Supply the
        # existing Secret password through the terminal only when Steam asks.
        base_args = [str(STEAMCMD / 'steamcmd.sh'), '+@ShutdownOnFailedCommand', '1',
                     '+@NoPromptForPassword', '0', '+force_install_dir', str(ROOT),
                     '+login', username, *commands, '+quit']
        messages = [expected] if isinstance(expected, str) else expected
        matched = set()
        app_success = "Success! App '233780' fully installed."
        for attempt in range(opts['retries']):
            print(f'Steam operation attempt {attempt + 1}/{opts["retries"]}', flush=True)
            try:
                args = base_args
                attempt_messages = messages
                if app_success in matched:
                    # Do not re-verify the full server depot after a later
                    # Workshop item failed in the same Steam session.
                    args = []
                    index = 0
                    while index < len(base_args):
                        if base_args[index] == '+app_update':
                            index += 2
                            if index < len(base_args) and base_args[index] == '-beta':
                                index += 2
                            if index < len(base_args) and base_args[index] == '-betapassword':
                                index += 2
                            if index < len(base_args) and base_args[index] == 'validate':
                                index += 1
                            continue
                        args.append(base_args[index])
                        index += 1
                    attempt_messages = [message for message in messages if message != app_success]
                steam_auth.run_session(
                    args, attempt_messages, password, secrets, env,
                    settings['steam']['authTimeoutSeconds'], opts['timeoutSeconds'])
                return
            except steam_auth.DownloadError as error:
                matched.update(error.matched)
                print(str(error), flush=True)
                if attempt + 1 < opts['retries']:
                    time.sleep(opts['retryDelaySeconds'])
        # Authentication errors are not blindly retried; Kubernetes will back off
        # a failed init container. Codes and credentials never enter exceptions.
        raise steam_auth.DownloadError(
            'Steam downloads failed; check bootstrap logs, Workshop access and disk space',
            matched)


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
        'autoSelectMission = 1;',
        f'steamQueryPort = {server["port"] + 1};',
        'voteThreshold = 1.5;',
        'admins[] = {' + ', '.join(cfg_string(x) for x in server['admin']['uids']) + '};',
    ]
    mission = server['mission']
    if mission['template']:
        lines += ['class Missions {', '  class Mission1 {',
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


def workshop_mod_paths(path):
    """Return Arma mod roots, handling Workshop items with nested @Mod dirs."""
    if (path / 'addons').is_dir():
        return [path]
    roots = sorted(child for child in path.iterdir()
                   if child.is_dir() and (child / 'addons').is_dir()) if path.is_dir() else []
    return roots or [path]


def stage_workshop_missions(settings):
    """Expose mission folders bundled inside Workshop mods to Arma's mpmissions."""
    (ROOT / 'mpmissions').mkdir(parents=True, exist_ok=True)
    staged = []
    template = settings['server']['mission']['template']
    for root in mod_paths(settings):
        for mission_pbo in root.rglob('*.pbo'):
            staged.extend(extract_mission_pbo(mission_pbo, template))
        for mission_file in root.rglob('mission.sqm'):
            mission = mission_file.parent
            name = mission.name
            if '.' not in name or not mission.is_dir():
                continue
            destination = ROOT / 'mpmissions' / name
            shutil.copytree(mission, destination, dirs_exist_ok=True)
            staged.append(name)
    if staged:
        print('Staged Workshop missions: ' + ', '.join(sorted(set(staged))), flush=True)


def extract_mission_pbo(pbo, template):
    """Extract one mission directory from an uncompressed Arma PBO archive."""
    entries = []
    with pbo.open('rb') as stream:
        while True:
            name_bytes = bytearray()
            while True:
                byte = stream.read(1)
                if not byte:
                    return []
                if byte == b'\0':
                    break
                name_bytes.extend(byte)
            header = stream.read(20)
            if len(header) != 20:
                return []
            if not name_bytes:
                break
            method, _, _, _, size = struct.unpack('<5I', header)
            entries.append((name_bytes.decode('utf-8'), method, size))
        data_offset = stream.tell()
        mission_roots = set()
        for name, _, _ in entries:
            parts = name.replace('\\', '/').split('/')
            if not parts or parts[-1].lower() != 'mission.sqm':
                continue
            candidates = [part for part in parts[:-1] if '.' in part]
            if candidates:
                root = candidates[-1]
                if (root.lower() == template.lower()
                        or root.lower().startswith(template.split('.')[0].lower())):
                    mission_roots.add(root)
        wanted = []
        offset = data_offset
        for name, method, size in entries:
            normalized = name.replace('\\', '/')
            parts = normalized.split('/')
            roots = [index for index, part in enumerate(parts) if part in mission_roots]
            if roots:
                index = roots[-1]
                wanted.append((parts[index + 1:], method, size, offset, parts[index]))
            offset += size
        if not wanted:
            return []
        for relative, method, size, offset, root in wanted:
            if method != 0:
                raise RuntimeError('Compressed mission PBO is unsupported: ' + str(pbo))
            destination = ROOT / 'mpmissions' / root / Path(*relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            stream.seek(offset)
            with destination.open('wb') as output:
                remaining = size
                while remaining:
                    chunk = stream.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise RuntimeError('Truncated mission PBO: ' + str(pbo))
                    output.write(chunk)
                    remaining -= len(chunk)
    return sorted({root for _, _, _, _, root in wanted})


def mod_paths(settings, server_only=False):
    mods = settings['mods']
    ids = mods['serverWorkshop' if server_only else 'workshop']
    local = mods['serverLocal' if server_only else 'local']
    workshop = [root for item in ids for root in workshop_mod_paths(WORKSHOP / item)]
    return workshop + [ROOT / item for item in local]


def bootstrap(settings):
    os.chdir(ROOT)
    # Older Helm reuse-values modes may omit newly added settings.
    settings['steam'].setdefault('installBaseGame', True)
    for directory in ('configs/profiles', 'mpmissions', 'keys', '.chart'):
        (ROOT / directory).mkdir(parents=True, exist_ok=True)
    # Fail early on bad config, before downloading gigabytes.
    config = generate_config(settings['server'])
    opts, server = settings['bootstrap'], settings['server']
    game_marker = ROOT / '.chart/game.json'
    game_identity = json.dumps({'branch': settings['steam']['branch'],
                                'baseGame': settings['steam']['installBaseGame'],
                                'cdlc': server['cdlc']}, sort_keys=True)
    binary = ROOT / server['binary']
    commands, expected, pending_mods = [], [], []
    install_game = (opts['updatePolicy'] == 'always' or not binary.is_file()
                    or not game_marker.exists() or game_marker.read_text() != game_identity)
    if install_game:
        game_marker.unlink(missing_ok=True)
        if settings['steam']['installBaseGame']:
            commands += ['+app_update', '107410', '-beta', settings['steam']['branch']]
            if os.environ.get('STEAM_BRANCH_PASSWORD'):
                commands += ['-betapassword', os.environ['STEAM_BRANCH_PASSWORD']]
            if opts['validate']:
                commands += ['validate']
            expected.append("Success! App '107410' fully installed.")
        commands += ['+app_update', '233780', '-beta', settings['steam']['branch']]
        if os.environ.get('STEAM_BRANCH_PASSWORD'):
            commands += ['-betapassword', os.environ['STEAM_BRANCH_PASSWORD']]
        if opts['validate']:
            commands += ['validate']
        expected.append("Success! App '233780' fully installed.")
    for item in settings['mods']['workshop'] + settings['mods']['serverWorkshop']:
        path = WORKSHOP / item
        marker = path / '.chart-ready'
        if opts['updatePolicy'] == 'always' or not marker.exists() or not mod_valid(path):
            marker.unlink(missing_ok=True)
            commands += ['+workshop_download_item', '107410', item]
            if opts['validate']:
                commands += ['validate']
            expected.append(f'Success. Downloaded item {item}')
            pending_mods.append((item, path, marker))
    if commands:
        try:
            steamcmd(settings, commands, expected)
        except steam_auth.DownloadError as error:
            # Do not make retries re-verify the entire server when a later
            # Workshop item was rejected. The app success is separately durable.
            if install_game and "Success! App '233780' fully installed." in error.matched and binary.is_file():
                atomic_write(game_marker, game_identity)
            raise
    if install_game:
        if not binary.is_file():
            raise RuntimeError('Server binary missing after installation: ' + str(binary))
        atomic_write(game_marker, game_identity)
    if settings['steam']['installBaseGame'] and not (ROOT / 'addons/a3_map_altis.pbo').is_file():
        raise RuntimeError('Base Arma 3 content is missing: /arma3/addons/a3_map_altis.pbo; install app 107410 or disable installBaseGame')
    for item, path, marker in pending_mods:
        lower_tree(path)
        if not mod_valid(path):
            raise RuntimeError('Workshop item has no PBOs; use individual mod IDs, not collections: ' + item)
        atomic_write(marker, 'ready\n')
    stage_workshop_missions(settings)
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
        library = STEAMCMD / ('linux' + bits) / 'steamclient.so'
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
    if action == 'auth':
        steam_auth.attach(cancel='--cancel' in sys.argv[2:])
    elif action == 'bootstrap':
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
    try:
        main()
    except steam_auth.AuthenticationError as error:
        print(str(error), file=sys.stderr, flush=True)
        sys.exit(1)
