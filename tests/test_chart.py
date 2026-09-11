import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import jsonschema
import yaml

CHART = Path('charts/arma3')


def render(overrides=None, success=True):
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml') as values:
        yaml.safe_dump(overrides or {}, values)
        values.flush()
        result = subprocess.run(['helm', 'template', 'test', str(CHART), '-f', values.name],
                                capture_output=True, text=True)
    if not success:
        assert result.returncode != 0, result.stdout
        return result.stderr
    assert result.returncode == 0, result.stderr
    return [x for x in yaml.safe_load_all(result.stdout) if x]


def kind(objects, name):
    return [x for x in objects if x['kind'] == name]


class ChartTest(unittest.TestCase):
    def test_defaults_and_credential_isolation(self):
        objects = render()
        self.assertEqual(len(kind(objects, 'PersistentVolumeClaim')), 3)
        deployment = kind(objects, 'Deployment')[0]
        self.assertEqual(deployment['spec']['strategy']['type'], 'Recreate')
        self.assertEqual(deployment['spec']['replicas'], 1)
        pod = deployment['spec']['template']['spec']
        self.assertFalse(pod['automountServiceAccountToken'])
        init = pod['initContainers'][0]
        self.assertEqual(next(x for x in init['env'] if x['name'] == 'STEAM_USER')['valueFrom']['secretKeyRef'],
                         {'name': 'arma3-steam', 'key': 'username'})
        self.assertNotIn('env', pod['containers'][0])
        mounts = {x['name']: x['mountPath'] for x in init['volumeMounts']}
        self.assertEqual(mounts['steam'], '/var/lib/arma3-steam')
        self.assertNotIn('steam', [m['name'] for m in pod['containers'][0]['volumeMounts']])
        self.assertEqual(mounts['data'], '/arma3')
        self.assertEqual(mounts['workshop'], '/arma3/steamapps/workshop')
        ports = kind(objects, 'Service')[0]['spec']['ports']
        self.assertEqual([x['port'] for x in ports], list(range(32302, 32307)))
        self.assertEqual([x['nodePort'] for x in ports], list(range(32302, 32307)))
        self.assertTrue(all(x['protocol'] == 'UDP' for x in ports))
        self.assertTrue(all(x['metadata']['annotations']['helm.sh/resource-policy'] == 'keep'
                            for x in kind(objects, 'PersistentVolumeClaim')))

    def test_gateway_routes(self):
        objects = render(yaml.safe_load(Path('examples/gateway.yaml').read_text()))
        routes = kind(objects, 'UDPRoute')
        self.assertEqual(len(routes), 5)
        schema = json.loads(Path('tests/schemas/udproute_v1alpha2.json').read_text())
        for i, route in enumerate(routes):
            jsonschema.validate(route, schema)
            ref = route['spec']['rules'][0]['backendRefs'][0]
            self.assertEqual(ref['name'], 'test-arma3')
            self.assertEqual(ref['port'], 2302 + i)
            self.assertIn('sectionName', route['spec']['parentRefs'][0])
        self.assertTrue(all('nodePort' not in p for p in kind(objects, 'Service')[0]['spec']['ports']))

    def test_gateway_port_attachment_and_nodeport_together(self):
        objects = render({'gateway': {'enabled': True, 'parentRefs': [{'name': 'games'}]}})
        for i, route in enumerate(kind(objects, 'UDPRoute')):
            self.assertEqual(route['spec']['parentRefs'][0]['port'], 32302 + i)
        self.assertEqual(kind(objects, 'Service')[0]['spec']['type'], 'NodePort')

    def test_existing_and_ephemeral_storage(self):
        objects = render({'persistence': {'data': {'existingClaim': 'my-data'},
                                          'workshop': {'enabled': False}, 'steam': {'existingClaim': 'my-steam'}}})
        self.assertFalse(kind(objects, 'PersistentVolumeClaim'))
        volumes = kind(objects, 'Deployment')[0]['spec']['template']['spec']['volumes']
        self.assertEqual(volumes[0]['persistentVolumeClaim']['claimName'], 'my-data')
        self.assertEqual(next(v for v in volumes if v['name'] == 'workshop')['emptyDir'], {})
        self.assertEqual(next(v for v in volumes if v['name'] == 'steam')['persistentVolumeClaim']['claimName'], 'my-steam')

    def test_storage_class_and_retention(self):
        objects = render({'persistence': {'data': {'storageClass': '-', 'retain': False}}})
        pvc = kind(objects, 'PersistentVolumeClaim')[0]
        self.assertEqual(pvc['spec']['storageClassName'], '')
        self.assertNotIn('annotations', pvc['metadata'])

    def test_full_config_secret(self):
        objects = render({'server': {'existingConfigSecret': 'my-config',
                                     'configKey': 'custom.cfg', 'admin': {'existingSecret': ''}}})
        pod = kind(objects, 'Deployment')[0]['spec']['template']['spec']
        self.assertEqual(len(pod['initContainers'][0]['env']), 4)
        volume = next(x for x in pod['volumes'] if x['name'] == 'server-config')
        self.assertEqual(volume['secret']['items'], [{'key': 'custom.cfg', 'path': 'server.cfg'}])

    def test_config_change_rolls_pod(self):
        def checksum(overrides):
            return kind(render(overrides), 'Deployment')[0]['spec']['template']['metadata']['annotations']['checksum/config']
        self.assertNotEqual(checksum({}), checksum({'mods': {'workshop': ['2867537125']}}))

    def test_invalid_values(self):
        for values in [
            {'mods': {'workshop': ['https://steamcommunity.com/sharedfiles/filedetails/?id=1']}},
            {'mods': {'serverWorkshop': ['3020755032']}},
            {'mods': {'local': ['mods/../../etc']}},
            {'server': {'port': 65534}},
            {'server': {'port': 2302}},
            {'gateway': {'enabled': True}},
            {'bootstrap': {'retries': 0}},
            {'steam': {'authTimeoutSeconds': 0}},
            {'server': {'admin': {'existingSecret': ''}}},
            {'server': {'mission': {'parameters': {'bad;name': 1}}}},
        ]:
            with self.subTest(values=values):
                render(values, success=False)

    def test_examples(self):
        for filename in Path('examples').glob('*.yaml'):
            if filename.name != 'gateway-resource.yaml':
                with self.subTest(example=filename.name):
                    render(yaml.safe_load(filename.read_text()))
