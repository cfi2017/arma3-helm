import copy
import importlib.util
import os
import sys
from pathlib import Path
import tempfile
import unittest
import struct
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path('charts/arma3/files').resolve()))

spec = importlib.util.spec_from_file_location('runtime', 'charts/arma3/files/runtime.py')
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)
DEFAULTS = yaml.safe_load(Path('charts/arma3/values.yaml').read_text())


class RuntimeTest(unittest.TestCase):
    def setUp(self):
        self.settings = copy.deepcopy(DEFAULTS)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workshop = self.root / 'steamapps/workshop/content/107410'
        self.steam_source = self.root / 'image-steamcmd'
        self.steam_staged = self.root / 'staged-steamcmd'
        (self.steam_source / 'linux32').mkdir(parents=True)
        # Exercise an actual launcher/native-process chain, not mocked Popen.
        (self.steam_source / 'steamcmd.sh').write_text(
            '#!/bin/sh\nexec "${0%/*}/linux32/steamcmd" "$@"\n')
        (self.steam_source / 'linux32/steamcmd').write_text(
            '#!/bin/sh\nprintf "%s\\n" "Waiting for user info...OK" "Success. Downloaded item 1234"\n')
        for name in ('steamcmd.sh', 'linux32/steamcmd'):
            (self.steam_source / name).chmod(0o644)
        for key, value in [('ROOT', self.root), ('WORKSHOP', self.workshop),
                           ('STEAMCMD_SOURCE', self.steam_source),
                           ('STEAMCMD', self.steam_staged),
                           ('STEAM_STATE', self.root / 'steam-state')]:
            p = patch.object(runtime, key, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.dict(os.environ, {'STEAM_USER': 'test-user', 'STEAM_PASSWORD': 'test-steam-password',
                                    'ADMIN_PASSWORD': 'test-admin'})
        p.start()
        self.addCleanup(p.stop)
        previous = Path.cwd()
        self.addCleanup(os.chdir, previous)

    def fake_download(self, settings, commands, expected):
        if '+app_update' in commands:
            (self.root / 'arma3server_x64').touch()
            (self.root / 'addons').mkdir(exist_ok=True)
            (self.root / 'addons/map_altis.pbo').touch()
        for i, command in enumerate(commands):
            if command != '+workshop_download_item':
                continue
            item = commands[i + 2]
            path = self.workshop / item
            (path / 'Addons').mkdir(parents=True, exist_ok=True)
            (path / 'Addons/Test.PBO').write_text('content')
            (path / 'Keys').mkdir(exist_ok=True)
            (path / 'Keys/Test.BIKEY').write_text('key')

    def test_bootstrap_cache_save_preservation_and_removed_mods(self):
        self.settings['bootstrap']['updatePolicy'] = 'if-missing'
        with patch.object(runtime, 'steamcmd', side_effect=self.fake_download) as steam:
            runtime.bootstrap(self.settings)
            self.assertEqual(steam.call_count, 1)
            self.assertTrue((self.workshop / '3020755032/addons/test.pbo').exists())
            self.assertTrue((self.root / 'keys/test.bikey').exists())
            save = self.root / 'configs/profiles/antistasi.vars.Arma3Profile'
            save.write_text('campaign')
            runtime.bootstrap(self.settings)
            self.assertEqual(steam.call_count, 1)
            self.assertEqual(save.read_text(), 'campaign')
            self.settings['mods']['workshop'] = []
            runtime.bootstrap(self.settings)
            self.assertFalse((self.root / 'keys/test.bikey').exists())
            self.assertTrue((self.workshop / '3020755032').exists())
            self.assertEqual(save.read_text(), 'campaign')

    def test_failed_download_does_not_mark_ready(self):
        with patch.object(runtime, 'steamcmd', side_effect=RuntimeError('failed')):
            with self.assertRaises(RuntimeError):
                runtime.bootstrap(self.settings)
        self.assertFalse((self.root / '.chart/game.json').exists())

    def test_success_message_without_mod_files_fails(self):
        self.settings['steam']['installBaseGame'] = False
        (self.root / 'arma3server_x64').touch()
        with patch.object(runtime, 'steamcmd'):
            with self.assertRaisesRegex(RuntimeError, 'no PBOs'):
                runtime.bootstrap(self.settings)
        self.assertFalse((self.workshop / '3020755032/.chart-ready').exists())

    def test_download_failure_retries_but_auth_failure_does_not(self):
        self.settings['bootstrap'].update(retries=2, retryDelaySeconds=0)
        with patch.object(runtime.steam_auth, 'run_session',
                          side_effect=runtime.steam_auth.DownloadError('failed')) as session:
            with self.assertRaises(RuntimeError):
                runtime.steamcmd(self.settings, ['+quit'], 'Success')
            self.assertEqual(session.call_count, 2)
        with patch.object(runtime.steam_auth, 'run_session',
                          side_effect=runtime.steam_auth.AuthenticationError('failed')) as session:
            with self.assertRaises(runtime.steam_auth.AuthenticationError):
                runtime.steamcmd(self.settings, ['+quit'], 'Success')
            self.assertEqual(session.call_count, 1)

    def test_successful_game_is_marked_when_workshop_fails(self):
        self.settings['bootstrap']['retries'] = 1
        (self.root / 'arma3server_x64').touch()
        with patch.object(runtime.steam_auth, 'run_session',
                          side_effect=runtime.steam_auth.DownloadError(
                              'workshop failed', {"Success! App '233780' fully installed."})):
            with self.assertRaises(runtime.steam_auth.DownloadError):
                runtime.bootstrap(self.settings)
        self.assertTrue((self.root / '.chart/game.json').exists())

    def test_retry_drops_completed_game_update(self):
        self.settings['bootstrap'].update(retries=2, retryDelaySeconds=0)
        app = "Success! App '233780' fully installed."
        workshop = 'Success. Downloaded item 3020755032'
        calls = []

        def fail_then_succeed(args, messages, *unused):
            calls.append((args, messages))
            if len(calls) == 1:
                raise runtime.steam_auth.DownloadError('workshop failed', {app})

        with patch.object(runtime.steam_auth, 'run_session', side_effect=fail_then_succeed):
            runtime.steamcmd(self.settings, ['+app_update', '233780', '-beta', 'public',
                                             'validate', '+workshop_download_item', '107410',
                                             '3020755032', 'validate'], [app, workshop])
        self.assertIn('+app_update', calls[0][0])
        self.assertNotIn('+app_update', calls[1][0])
        self.assertEqual(calls[1][1], [workshop])

    def test_cached_login_arguments_and_persistent_home(self):
        with patch.object(runtime.steam_auth, 'run_session') as session:
            runtime.steamcmd(self.settings, ['+quit'], 'Success')
        args, _, password, _, env, _, _ = session.call_args.args
        self.assertEqual(args[args.index('+login') + 1], 'test-user')
        self.assertNotIn('test-steam-password', args)
        self.assertEqual(password, 'test-steam-password')
        self.assertEqual(env['HOME'], str(self.root / 'steam-state/home'))
        self.assertNotIn('STEAM_PASSWORD', env)
        self.assertNotIn('ADMIN_PASSWORD', env)
        self.assertEqual((self.root / 'steam-state').stat().st_mode & 0o777, 0o700)

    def test_non_executable_image_files_are_staged_and_launched(self):
        self.settings['bootstrap']['retries'] = 1
        library = self.steam_source / 'linux32/steamclient.so'
        library.write_text('bundled SDK')
        library.chmod(0o444)
        runtime.steamcmd(self.settings, ['+workshop_download_item', '107410', '1234'],
                         'Success. Downloaded item 1234')
        for name in ('steamcmd.sh', 'linux32/steamcmd'):
            source = self.steam_source / name
            staged = self.steam_staged / name
            self.assertEqual(source.stat().st_mode & 0o777, 0o644)
            self.assertEqual(staged.stat().st_uid, os.getuid())
            self.assertTrue(os.access(staged, os.X_OK))
        (self.steam_staged / 'linux32/steamclient.so').write_text('updated SDK')
        self.assertEqual(library.read_text(), 'bundled SDK')
        # Subsequent Workshop operations must retain Steam's self-updates.
        binary = self.steam_staged / 'linux32/steamcmd'
        binary.write_text('#!/bin/sh\nprintf "%s\\n" "Waiting for user info...OK" "Updated binary"\n')
        runtime.steamcmd(self.settings, ['+quit'], 'Updated binary')

    def test_case_collision_fails(self):
        (self.root / 'Test.PBO').touch()
        (self.root / 'test.pbo').touch()
        with self.assertRaisesRegex(RuntimeError, 'Case-colliding'):
            runtime.lower_tree(self.root)

    def test_config_and_argument_boundaries(self):
        server = self.settings['server']
        server['hostname'] = 'My "server"'
        with patch.dict(os.environ, {'ADMIN_PASSWORD': 'a"b'}):
            config = runtime.generate_config(server)
        self.assertIn('hostname = "My ""server""";', config)
        self.assertIn('passwordAdmin = "a""b";', config)
        self.assertIn('autoLoadLastGame = 60;', config)
        self.assertIn('persistent = 1;', config)
        self.assertIn('autoSelectMission = 1;', config)
        self.assertIn('class Mission1 {', config)
        server['extraArgs'] = ['-test=spaces and $(literal)']
        self.settings['mods']['serverWorkshop'] = ['1234']
        args = runtime.server_args(self.settings)
        self.assertIn('-test=spaces and $(literal)', args)
        self.assertIn('-serverMod=' + str(self.workshop / '1234'), args)
        self.assertIn(str(self.workshop / '3020755032'), next(x for x in args if x.startswith('-mod=')))
        self.assertNotIn('test-admin', ' '.join(args))
        server['hostname'] = 'bad\nvalue'
        with self.assertRaises(ValueError):
            runtime.generate_config(server)

    def test_nested_workshop_mod_root_is_used(self):
        nested = self.workshop / '3020755032' / '@A3A' / 'addons'
        nested.mkdir(parents=True)
        (nested / 'a3a.pbo').touch()
        args = runtime.server_args(self.settings)
        mod_arg = next(x for x in args if x.startswith('-mod='))
        self.assertIn(str(nested.parent), mod_arg)
        self.assertNotIn(str(self.workshop / '3020755032') + ';', mod_arg)

    def test_workshop_mission_is_staged(self):
        mission = self.workshop / '3020755032' / 'addons' / 'maps' / 'Antistasi_Altis.Altis'
        mission.mkdir(parents=True)
        (mission / 'mission.sqm').write_text('class Mission {};')
        runtime.stage_workshop_missions(self.settings)
        self.assertTrue((self.root / 'mpmissions/Antistasi_Altis.Altis/mission.sqm').exists())

    def test_workshop_mission_pbo_is_staged(self):
        addons = self.workshop / '3020755032' / 'addons'
        addons.mkdir(parents=True)
        name = b'Antistasi_Altis.Altis/mission.sqm\0'
        payload = b'class Mission {};'
        header = struct.pack('<5I', 0, len(payload), 0, 0, len(payload))
        (addons / 'maps.pbo').write_bytes(name + header + b'\0' * 21 + payload)
        runtime.stage_workshop_missions(self.settings)
        self.assertEqual((self.root / 'mpmissions/Antistasi_Altis.Altis/mission.sqm').read_bytes(), payload)
