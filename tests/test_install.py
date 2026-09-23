import io
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch
import zipfile

from wechat_linux_cli import install


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.owner = install.Owner(1000, 1000, 'desktop', '/home/desktop')

    def tearDown(self):
        self.temp.cleanup()

    def wheel(self, project='wechat-linux-cli', version='0.1.0a1', extra=None, requirement=None):
        stem = project.replace('-', '_')
        path = self.root / f'{stem}-{version}-py3-none-any.whl'
        entries = {f'{stem}-{version}.dist-info/METADATA': f'Metadata-Version: 2.1\nName: {project}\nVersion: {version}\n'}
        if requirement is not None:
            entries[f'{stem}-{version}.dist-info/METADATA'] += f'Requires-Dist: {requirement}\n'
        if project == 'wechat-linux-cli':
            entries.update({f'wechat_linux_cli/{f}': '' for f in ('service.py', 'cli.py', 'install.py', '__main__.py')})
        entries.update(extra or {})
        with zipfile.ZipFile(path, 'w') as output:
            for name, data in entries.items():
                output.writestr(name, data)
        return path

    def pair(self):
        source = self.wheel(requirement='pycryptodome<4,>=3.20')
        self.wheel('pycryptodome', '3.23.0')
        return source, install.collect_wheels(source, self.root)

    def test_dry_run_checks_material_and_does_not_install(self):
        source, _ = self.pair()
        with patch.object(install, 'target_owner', return_value=self.owner), patch.object(install, 'apply') as apply, patch('sys.stdout', new_callable=io.StringIO) as output:
            status = install.main(['--source', str(source), '--wheelhouse', str(self.root)])
        apply.assert_not_called()
        result = json.loads(output.getvalue())
        self.assertEqual(status, 0)
        self.assertFalse(result['applied'])
        self.assertEqual(result['target']['uid'], 1000)
        self.assertEqual(len(result['wheels']), 2)
        self.assertTrue(all(len(w['sha256']) == 64 for w in result['wheels']))

    def test_source_directory_requires_one_built_wheel(self):
        dist = self.root / 'dist'
        dist.mkdir()
        source = self.wheel()
        with self.assertRaisesRegex(install.InstallError, 'SOURCE_WHEEL_REQUIRED'):
            install.source_wheel(self.root)
        source.rename(dist / source.name)
        self.assertEqual(install.source_wheel(self.root), dist / source.name)
        (dist / 'wechat_linux_cli-2-py3-none-any.whl').touch()
        with self.assertRaisesRegex(install.InstallError, 'SOURCE_WHEEL_REQUIRED'):
            install.source_wheel(self.root)

    def test_archive_traversal_path_hooks_and_noncanonical_paths_rejected(self):
        for name in ('../escape', '/absolute', 'a/../../escape', 'a\\b', 'a/./b', 'a//b', 'injected.pth'):
            with self.subTest(name=name):
                with self.assertRaisesRegex(install.InstallError, 'UNSAFE_WHEEL'):
                    install.read_wheel(self.wheel(extra={name: 'payload'}))

    def test_archive_symlinks_rejected(self):
        source = self.wheel()
        with zipfile.ZipFile(source, 'a') as output:
            link = zipfile.ZipInfo('wechat_linux_cli/linked.py')
            link.create_system = 3
            link.external_attr = 0o120777 << 16
            output.writestr(link, '/outside')
        with self.assertRaisesRegex(install.InstallError, 'UNSAFE_WHEEL'):
            install.read_wheel(source)

    def test_wheel_file_symlink_rejected(self):
        source = self.wheel()
        link = self.root / 'alias.whl'
        link.symlink_to(source)
        with self.assertRaisesRegex(install.InstallError, 'INVALID_WHEEL'):
            install.read_wheel(link)

    def test_unrelated_packages_and_dependency_urls_rejected(self):
        with self.assertRaisesRegex(install.InstallError, 'UNEXPECTED_PACKAGE'):
            install.read_wheel(self.wheel('unrelated'))
        with self.assertRaisesRegex(install.InstallError, 'UNEXPECTED_DEPENDENCY'):
            install.read_wheel(self.wheel(requirement='pycryptodome @ https://example.invalid/a.whl'))
        with self.assertRaisesRegex(install.InstallError, 'UNEXPECTED_DEPENDENCY'):
            install.read_wheel(self.wheel(requirement='zstandard>=1'))

    def test_dependency_comparison_operators_in_wheel_metadata(self):
        for requirement in ('pycryptodome>=3.20,<4', 'pycryptodome<4,>=3.20',
                            'pycryptodome >=3.20, <4', 'pycryptodome~=3.20',
                            'pycryptodome==3.23.0', 'pycryptodome!=3.21,<=3.23.0'):
            with self.subTest(requirement=requirement):
                self.assertEqual(install.read_wheel(self.wheel(requirement=requirement)).project,
                                 'wechat-linux-cli')
        for requirement in ('pycryptodome=>3.20', 'pycryptodome=<4',
                            'pycryptodome~3.20', 'pycryptodome!3.21',
                            'pycryptodome[extra]>=3.20', 'pycryptodome>=3.20; python_version>="3.11"'):
            with self.subTest(requirement=requirement):
                with self.assertRaisesRegex(install.InstallError, 'UNEXPECTED_DEPENDENCY'):
                    install.read_wheel(self.wheel(requirement=requirement))

    def test_source_requires_service(self):
        source = self.wheel('pycryptodome', '3.23.0')
        with zipfile.ZipFile(source) as inp:
            metadata = inp.read('pycryptodome-3.23.0.dist-info/METADATA').replace(b'pycryptodome', b'wechat-linux-cli')
        with zipfile.ZipFile(source, 'w') as out:
            out.writestr('wechat_linux_cli-0.1.dist-info/METADATA', metadata)
        with self.assertRaisesRegex(install.InstallError, 'INCOMPLETE_SOURCE'):
            install.read_wheel(source)

    def test_dependency_version_and_ambiguity_rejected(self):
        source = self.wheel()
        old = self.wheel('pycryptodome', '3.19.0')
        with self.assertRaisesRegex(install.InstallError, 'DEPENDENCY_VERSION'):
            install.collect_wheels(source, self.root)
        old.unlink()
        self.wheel('pycryptodome', '3.23.0')
        self.wheel('pycryptodome', '3.24.0')
        with self.assertRaisesRegex(install.InstallError, 'DEPENDENCY_WHEEL_REQUIRED'):
            install.collect_wheels(source, self.root)

    def test_unit_limits_capabilities_and_preserves_pending_debugger(self):
        unit = install.unit_text(self.owner)
        for text in ('User=1000', 'Group=1000', 'AmbientCapabilities=CAP_SYS_PTRACE',
                     'CapabilityBoundingSet=CAP_SYS_PTRACE', 'NoNewPrivileges=yes',
                     'RestrictAddressFamilies=AF_UNIX', 'UMask=0077', 'RuntimeDirectoryMode=0700',
                     'KillMode=process', 'SendSIGKILL=no', 'TimeoutStopSec=infinity',
                     'Restart=no', '-I -m wechat_linux_cli.service --socket /run/wechat-linux-cli-1000/control.sock'):
            self.assertIn(text, unit)
        for forbidden in ('PrivateTmp=', 'PrivateUsers=', 'ProtectHome=', 'setcap', 'ExecStop=/bin/kill'):
            self.assertNotIn(forbidden, unit)

    def test_home_specifiers_escaped(self):
        owner = install.Owner(1000, 1000, 'desktop', '/home/a%b"c')
        self.assertIn('Environment="HOME=/home/a%%b\\"c"', install.unit_text(owner))

    def test_root_target_refused_and_sudo_owner_used(self):
        entry = types.SimpleNamespace(pw_uid=1000, pw_gid=1001, pw_name='desktop', pw_dir='/home/desktop')
        with patch.dict(os.environ, {'SUDO_UID': '1000'}), patch.object(install.pwd, 'getpwuid', return_value=entry) as lookup:
            self.assertEqual(install.target_owner(None).gid, 1001)
        lookup.assert_called_once_with(1000)
        entry.pw_uid = 0
        with patch.object(install.pwd, 'getpwuid', return_value=entry):
            with self.assertRaisesRegex(install.InstallError, 'ROOT_TARGET_REFUSED'):
                install.target_owner('0')

    def test_apply_requires_root_before_any_write(self):
        _, wheels = self.pair()
        with patch.object(install.os, 'geteuid', return_value=1000), patch.object(install, 'write_new') as write:
            with self.assertRaisesRegex(install.InstallError, 'ROOT_REQUIRED'):
                install.apply(self.owner, wheels)
        write.assert_not_called()

    def test_staging_uses_snapshot_offline_and_activation_is_last(self):
        source, wheels = self.pair()
        # Changing user-owned input after validation cannot change root's install material.
        source.write_bytes(b'replaced after validation')
        prefix = self.root / 'prefix'
        unit_path = self.root / 'unit.service'
        launcher = self.root / 'wechat-linux'
        calls = []
        def run(argv, **kwargs):
            calls.append((argv, kwargs))
        with patch.object(install, 'PREFIX', prefix), patch.object(install, 'LAUNCHER', launcher):
            result = install._install(self.owner, wheels, run, unit_path)
        self.assertTrue(result['applied'])
        self.assertEqual((prefix / 'wheels' / source.name).read_bytes(), wheels[0].data)
        self.assertEqual((prefix / 'wheels').stat().st_mode & 0o777, 0o700)
        self.assertEqual((prefix / 'wheels' / source.name).stat().st_mode & 0o777, 0o600)
        pip = next(args for args, _ in calls if 'install' in args)
        for flag in ('--no-index', '--no-deps', '--only-binary=:all:', '--isolated'):
            self.assertIn(flag, pip)
        help_calls = [(args, opts) for args, opts in calls if '--help' in args]
        self.assertEqual(len(help_calls), 2)
        self.assertTrue(all(opts['as_owner'] for _, opts in help_calls))
        self.assertEqual(calls[-1][0], ['/usr/bin/systemctl', 'enable', '--now', 'wechat-linux-cli@1000.service'])

    def test_failed_smoke_check_removes_new_installation_before_activation(self):
        _, wheels = self.pair()
        prefix = self.root / 'prefix'
        launcher = self.root / 'wechat-linux'
        calls = []
        def fail_help(argv, **kwargs):
            calls.append(argv)
            if '--help' in argv:
                raise install.InstallError('bad smoke check')
        with patch.object(install, 'PREFIX', prefix), patch.object(install, 'LAUNCHER', launcher):
            with self.assertRaisesRegex(install.InstallError, 'smoke'):
                install._install(self.owner, wheels, fail_help, self.root / 'unit.service')
        self.assertFalse(prefix.exists())
        self.assertFalse(launcher.exists())
        self.assertFalse(any('enable' in c for c in calls))

    def test_operator_interrupt_before_activation_cleans_initial_install(self):
        _, wheels = self.pair()
        prefix = self.root / 'prefix'
        unit_path = self.root / 'unit.service'
        launcher = self.root / 'wechat-linux'
        def interrupt(argv, **kwargs):
            if '--help' in argv:
                raise KeyboardInterrupt
        with patch.object(install, 'PREFIX', prefix), patch.object(install, 'LAUNCHER', launcher):
            with self.assertRaises(KeyboardInterrupt):
                install._install(self.owner, wheels, interrupt, unit_path)
        self.assertFalse(prefix.exists())
        self.assertFalse(unit_path.exists())
        self.assertFalse(launcher.exists())

    def test_failed_activation_preserves_material_for_live_service(self):
        _, wheels = self.pair()
        prefix = self.root / 'prefix'
        unit_path = self.root / 'unit.service'
        launcher = self.root / 'wechat-linux'
        def fail_start(argv, **kwargs):
            if 'enable' in argv:
                raise install.InstallError('activation uncertain')
        with patch.object(install, 'PREFIX', prefix), patch.object(install, 'LAUNCHER', launcher):
            with self.assertRaisesRegex(install.InstallError, 'uncertain'):
                install._install(self.owner, wheels, fail_start, unit_path)
        self.assertTrue(prefix.exists())
        self.assertTrue(unit_path.exists())
        self.assertTrue(launcher.exists())

    def test_umask_restored_after_failure(self):
        previous = os.umask(0o077)
        try:
            with self.assertRaisesRegex(RuntimeError, 'stop'):
                with install.installation_umask():
                    raise RuntimeError('stop')
            current = os.umask(0o077)
            self.assertEqual(current, 0o077)
        finally:
            os.umask(previous)


if __name__ == '__main__':
    unittest.main()
