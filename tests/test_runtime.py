import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock

import yaml

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
        for key, value in [('ROOT', self.root), ('WORKSHOP', self.workshop)]:
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
        else:
            item = commands[2]
            path = self.workshop / item
            (path / 'Addons').mkdir(parents=True, exist_ok=True)
            (path / 'Addons/Test.PBO').write_text('content')
            (path / 'Keys').mkdir(exist_ok=True)
            (path / 'Keys/Test.BIKEY').write_text('key')

    def test_bootstrap_cache_save_preservation_and_removed_mods(self):
        self.settings['bootstrap']['updatePolicy'] = 'if-missing'
        with patch.object(runtime, 'steamcmd', side_effect=self.fake_download) as steam:
            runtime.bootstrap(self.settings)
            self.assertEqual(steam.call_count, 2)
            self.assertTrue((self.workshop / '3020755032/addons/test.pbo').exists())
            self.assertTrue((self.root / 'keys/test.bikey').exists())
            save = self.root / 'configs/profiles/antistasi.vars.Arma3Profile'
            save.write_text('campaign')
            runtime.bootstrap(self.settings)
            self.assertEqual(steam.call_count, 2)
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
        (self.root / 'arma3server_x64').touch()
        with patch.object(runtime, 'steamcmd'):
            with self.assertRaisesRegex(RuntimeError, 'no PBOs'):
                runtime.bootstrap(self.settings)
        self.assertFalse((self.workshop / '3020755032/.chart-ready').exists())

    def test_zero_exit_download_failure_retries_and_redacts(self):
        self.settings['bootstrap'].update(retries=2, retryDelaySeconds=0)
        process = MagicMock()
        process.__enter__.return_value = process
        process.returncode = 0
        process.communicate.return_value = ('ERROR test-user test-steam-password', None)
        output = io.StringIO()
        with patch.object(runtime.subprocess, 'Popen', return_value=process) as popen:
            with contextlib.redirect_stdout(output), self.assertRaises(RuntimeError):
                runtime.steamcmd(self.settings, ['+quit'], 'Success')
            self.assertEqual(popen.call_count, 2)
        self.assertNotIn('test-user', output.getvalue())
        self.assertNotIn('test-steam-password', output.getvalue())
        self.assertIn('[REDACTED]', output.getvalue())

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
        server['extraArgs'] = ['-test=spaces and $(literal)']
        self.settings['mods']['serverWorkshop'] = ['1234']
        args = runtime.server_args(self.settings)
        self.assertIn('-test=spaces and $(literal)', args)
        self.assertIn('-serverMod=' + str(self.workshop / '1234'), args)
        self.assertIn('-mod=' + str(self.workshop / '3020755032'), args)
        self.assertNotIn('test-admin', ' '.join(args))
        server['hostname'] = 'bad\nvalue'
        with self.assertRaises(ValueError):
            runtime.generate_config(server)
