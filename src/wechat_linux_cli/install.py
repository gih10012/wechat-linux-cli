"""Reviewable, offline installation of the owner-scoped system service.

Building happens as an ordinary user. This module never builds source as root.
"""
from __future__ import annotations

import argparse
import email.parser
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import re
import shutil
import stat
import subprocess
import sys
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass

PREFIX = Path('/opt/wechat-linux-cli')
UNIT_DIR = Path('/etc/systemd/system')
LAUNCHER = Path('/usr/local/bin/wechat-linux')
PYTHON = Path('/usr/bin/python3')
MAX_WHEEL_BYTES = 80 * 1024 * 1024
MAX_UNPACKED_BYTES = 200 * 1024 * 1024


class InstallError(ValueError):
    pass


@dataclass(frozen=True)
class Owner:
    uid: int
    gid: int
    name: str
    home: str


@dataclass(frozen=True)
class Wheel:
    path: Path
    data: bytes
    project: str
    version: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


def target_owner(value: str | None) -> Owner:
    value = value or os.environ.get('SUDO_UID') or str(os.getuid())
    try:
        entry = pwd.getpwuid(int(value)) if value.isdecimal() else pwd.getpwnam(value)
    except (KeyError, ValueError) as exc:
        raise InstallError('UNKNOWN_USER: select an existing local user') from exc
    if entry.pw_uid == 0:
        raise InstallError('ROOT_TARGET_REFUSED: select the desktop owner with --user')
    if not os.path.isabs(entry.pw_dir) or any(ord(c) < 32 for c in entry.pw_dir):
        raise InstallError('INVALID_HOME: target user must have an absolute home path')
    return Owner(entry.pw_uid, entry.pw_gid, entry.pw_name, entry.pw_dir)


def source_wheel(path: Path) -> Path:
    if path.is_symlink():
        raise InstallError('SYMLINK_SOURCE: source must not be a symbolic link')
    if path.is_dir():
        dist = path / 'dist'
        if dist.is_symlink():
            raise InstallError('SYMLINK_SOURCE: dist must not be a symbolic link')
        matches = sorted(dist.glob('wechat_linux_cli-*.whl'))
        if len(matches) != 1:
            raise InstallError('SOURCE_WHEEL_REQUIRED: source directory needs one dist/wechat_linux_cli-*.whl; build it as the ordinary user first')
        return matches[0]
    return path


def read_wheel(path: Path) -> Wheel:
    if path.suffix != '.whl' or not re.fullmatch(r'[A-Za-z0-9_.+-]+\.whl', path.name):
        raise InstallError('INVALID_WHEEL: expected a wheel filename')
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            st = os.fstat(stream.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_WHEEL_BYTES:
                raise InstallError('INVALID_WHEEL: expected a bounded regular file')
            data = stream.read(MAX_WHEEL_BYTES + 1)
        if len(data) > MAX_WHEEL_BYTES:
            raise InstallError('INVALID_WHEEL: file exceeds size limit')
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            names = set()
            if len(infos) > 10000 or sum(i.file_size for i in infos) > MAX_UNPACKED_BYTES:
                raise InstallError('INVALID_WHEEL: archive exceeds extraction limits')
            for info in infos:
                parts = PurePosixPath(info.filename).parts
                mode = (info.external_attr >> 16) & 0xFFFF
                if (not parts or info.filename.startswith('/') or '\\' in info.filename
                        or '..' in parts or info.filename in names or ':' in info.filename
                        or str(PurePosixPath(info.filename)) != info.filename.rstrip('/')
                        or any(ord(c) < 32 for c in info.filename)
                        or stat.S_ISLNK(mode)
                        or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR))
                        or info.filename.endswith('.pth')):
                    raise InstallError('UNSAFE_WHEEL: unsafe, duplicate, linked, or executable path hook in archive')
                names.add(info.filename)
            metadata_names = [n for n in names if n.count('/') == 1 and n.endswith('.dist-info/METADATA')]
            if len(metadata_names) != 1:
                raise InstallError('INVALID_WHEEL: exactly one distribution metadata file is required')
            metadata = email.parser.BytesParser().parsebytes(archive.read(metadata_names[0]))
            project = re.sub(r'[-_.]+', '-', metadata.get('Name', '')).lower()
            version = metadata.get('Version', '')
            if project not in {'wechat-linux-cli', 'pycryptodome'} or not version:
                raise InstallError('UNEXPECTED_PACKAGE: only wechat-linux-cli and pycryptodome are installed')
            requirements = metadata.get_all('Requires-Dist', [])
            for requirement in requirements:
                if project != 'wechat-linux-cli' or not re.fullmatch(r'pycryptodome\s*(?:(?:[<>=!~]=|[<>])[0-9.*]+\s*,?\s*)*', requirement, re.I):
                    raise InstallError('UNEXPECTED_DEPENDENCY: rebuild with only the declared pycryptodome dependency')
            if project == 'wechat-linux-cli':
                required = {'wechat_linux_cli/' + f for f in ('service.py', 'cli.py', 'install.py', '__main__.py')}
                if not required.issubset(names):
                    raise InstallError('INCOMPLETE_SOURCE: wheel must contain the service, CLI, and installer')
            # Force CRC/decompression validation before root passes material to pip.
            if archive.testzip() is not None:
                raise InstallError('INVALID_WHEEL: corrupted archive member')
    except (OSError, zipfile.BadZipFile, RuntimeError, KeyError) as exc:
        raise InstallError('INVALID_WHEEL: cannot read a regular, valid wheel') from exc
    return Wheel(path.absolute(), data, project, version)


def collect_wheels(source: Path, wheelhouse: Path) -> tuple[Wheel, Wheel]:
    package = read_wheel(source_wheel(source))
    if package.project != 'wechat-linux-cli':
        raise InstallError('INVALID_SOURCE: --source must select wechat-linux-cli')
    if wheelhouse.is_symlink() or not wheelhouse.is_dir():
        raise InstallError('INVALID_WHEELHOUSE: select a real directory of prebuilt wheels')
    dependencies = []
    for path in sorted(wheelhouse.glob('*.whl')):
        wheel = read_wheel(path)
        if wheel.project == 'wechat-linux-cli':
            if wheel.sha256 != package.sha256:
                raise InstallError('AMBIGUOUS_SOURCE: wheelhouse contains a different project wheel')
        else:
            dependencies.append(wheel)
    if len(dependencies) != 1:
        raise InstallError('DEPENDENCY_WHEEL_REQUIRED: wheelhouse needs exactly one compatible pycryptodome wheel')
    dependency = dependencies[0]
    match = re.fullmatch(r'(\d+)\.(\d+)(?:\.\d+)*', dependency.version)
    if not match or not ((3, 20) <= tuple(map(int, match.groups())) < (4, 0)):
        raise InstallError('DEPENDENCY_VERSION: pycryptodome must satisfy >=3.20,<4')
    return package, dependency


def unit_name(owner: Owner) -> str:
    return f'wechat-linux-cli@{owner.uid}.service'


def unit_text(owner: Owner) -> str:
    home = owner.home.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%')
    return f'''[Unit]
Description=Local WeChat CLI for UID {owner.uid}
After=local-fs.target

[Service]
Type=exec
User={owner.uid}
Group={owner.gid}
Environment="HOME={home}"
Environment="PATH=/usr/bin:/bin"
WorkingDirectory=/
ExecStart={PREFIX}/venv/bin/python -I -m wechat_linux_cli.service --socket /run/wechat-linux-cli-{owner.uid}/control.sock
RuntimeDirectory=wechat-linux-cli-{owner.uid}
RuntimeDirectoryMode=0700
RuntimeDirectoryPreserve=yes
UMask=0077
AmbientCapabilities=CAP_SYS_PTRACE
CapabilityBoundingSet=CAP_SYS_PTRACE
NoNewPrivileges=yes
RestrictAddressFamilies=AF_UNIX
RestrictSUIDSGID=yes
LimitCORE=0
KillMode=process
SendSIGKILL=no
TimeoutStopSec=infinity
Restart=no

[Install]
WantedBy=multi-user.target
'''


def make_plan(owner: Owner, wheels: tuple[Wheel, Wheel]) -> dict:
    return {
        'ok': True, 'action': 'plan', 'applied': False,
        'target': {'uid': owner.uid, 'gid': owner.gid, 'name': owner.name, 'home': owner.home},
        'prefix': str(PREFIX), 'launcher': str(LAUNCHER),
        'unit_path': str(UNIT_DIR / unit_name(owner)),
        'socket': f'/run/wechat-linux-cli-{owner.uid}/control.sock',
        'wheels': [{'path': str(w.path), 'project': w.project, 'version': w.version, 'sha256': w.sha256} for w in wheels],
        'unit': unit_text(owner),
        'steps': ['validate root-owned system paths and unused installation targets',
                  'snapshot validated wheel bytes into root-owned /opt installation material',
                  'create a copied-interpreter virtual environment and install the two explicit wheels offline',
                  'run CLI and service --help as the target user; verify the systemd unit',
                  'install the command and unit; reload systemd',
                  f'systemctl enable --now {unit_name(owner)}'],
        'notes': ['Plan mode makes no changes. --apply requires root.',
                  'Initial installation only; existing installation targets are never overwritten.',
                  'The service must drain native calls on SIGTERM; systemd will not force-kill its GDB children.',
                  'No service deployment or real send is proved by this plan.'],
    }


def trusted_path(path: Path, *, directory: bool = False) -> None:
    """Reject writable/symlinked system destinations, including their ancestors."""
    for current in (path, *path.parents):
        st = current.lstat()
        if stat.S_ISLNK(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o022:
            raise InstallError('UNTRUSTED_SYSTEM_PATH: ' + str(current))
    if directory and not path.is_dir():
        raise InstallError('INVALID_SYSTEM_DIRECTORY: ' + str(path))


def write_new(path: Path, data: bytes, mode: int) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, 'wb') as output:
        os.fchmod(output.fileno(), mode)
        output.write(data)
        output.flush()
        os.fsync(output.fileno())


@contextmanager
def installation_umask():
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


def apply(owner: Owner, wheels: tuple[Wheel, Wheel]) -> dict:
    if os.geteuid() != 0:
        raise InstallError('ROOT_REQUIRED: review the plan, then rerun with sudo and --apply')
    for parent in (PREFIX.parent, UNIT_DIR, LAUNCHER.parent):
        trusted_path(parent, directory=True)
    python = PYTHON.resolve(strict=True)
    trusted_path(python)
    for executable in ('/usr/bin/systemctl', '/usr/bin/systemd-analyze'):
        trusted_path(Path(executable))
    if not Path('/run/systemd/system').is_dir():
        raise InstallError('SYSTEMD_REQUIRED: run on the Linux desktop host with systemd active')
    unit_path = UNIT_DIR / unit_name(owner)
    for target in (PREFIX, LAUNCHER, unit_path):
        if os.path.lexists(target):
            raise InstallError('INSTALL_TARGET_EXISTS: refusing to overwrite ' + str(target))
    env = {'PATH': '/usr/bin:/bin', 'HOME': '/root', 'LANG': 'C.UTF-8',
           'PIP_CONFIG_FILE': '/dev/null', 'PIP_NO_INDEX': '1', 'PIP_DISABLE_PIP_VERSION_CHECK': '1'}

    def run(argv: list[str], *, as_owner: bool = False) -> None:
        child_env = dict(env)
        kwargs = {}
        if as_owner:
            child_env['HOME'] = owner.home
            kwargs = {'user': owner.uid, 'group': owner.gid, 'extra_groups': []}
        result = subprocess.run(argv, env=child_env, cwd='/', stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **kwargs)
        if result.returncode:
            # Keep package/build output bounded; it can contain paths, never account data.
            raise InstallError('INSTALL_COMMAND_FAILED: ' + argv[0] + '\n' + (result.stderr or result.stdout)[-3000:])

    run([str(python), '-I', '-c', 'import sys,venv,ensurepip; assert sys.version_info >= (3,11)'])
    with installation_umask():
        return _install(owner, wheels, run, unit_path)


def _install(owner: Owner, wheels: tuple[Wheel, Wheel], run, unit_path: Path) -> dict:
    created_files = []
    activation_started = False
    PREFIX.mkdir(mode=0o755)
    try:
        material = PREFIX / 'wheels'
        material.mkdir(mode=0o700)
        for wheel in wheels:
            write_new(material / wheel.path.name, wheel.data, 0o600)
        run([str(PYTHON.resolve(strict=True)), '-I', '-m', 'venv', '--copies', str(PREFIX / 'venv')])
        vpython = str(PREFIX / 'venv/bin/python')
        run([vpython, '-I', '-m', 'pip', '--isolated', 'install', '--no-index', '--no-deps',
             '--no-cache-dir', '--only-binary=:all:', *(str(material / w.path.name) for w in wheels)])
        run([vpython, '-I', '-m', 'pip', '--isolated', 'check'])
        for module in ('wechat_linux_cli', 'wechat_linux_cli.service'):
            run([vpython, '-I', '-m', module, '--help'], as_owner=True)
        candidate = PREFIX / unit_name(owner)
        write_new(candidate, unit_text(owner).encode(), 0o644)
        run(['/usr/bin/systemd-analyze', 'verify', str(candidate)])
        write_new(unit_path, candidate.read_bytes(), 0o644)
        created_files.append(unit_path)
        launcher = f'#!/bin/sh\nexec {vpython} -I -m wechat_linux_cli "$@"\n'
        write_new(LAUNCHER, launcher.encode(), 0o755)
        created_files.append(LAUNCHER)
        write_new(PREFIX / 'installation.json', json.dumps(make_plan(owner, wheels), indent=2).encode() + b'\n', 0o644)
        run(['/usr/bin/systemctl', 'daemon-reload'])
        activation_started = True
        # Last mutation: enables boot startup and starts this exact UID instance.
        run(['/usr/bin/systemctl', 'enable', '--now', unit_name(owner)])
    except Exception:
        if not activation_started:
            for created in reversed(created_files):
                created.unlink()
            shutil.rmtree(PREFIX)
        # Once activation starts, preserve code and unit for diagnosis: a service
        # might be alive even if systemctl returned an error. Never kill GDB here.
        raise
    return {**make_plan(owner, wheels), 'action': 'install', 'applied': True,
            'status': 'installed_and_start_requested',
            'next': ['wechat-linux service-status', f'systemctl status {unit_name(owner)}']}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path, help='prebuilt project wheel, or source directory with one dist project wheel')
    parser.add_argument('--wheelhouse', required=True, type=Path, help='offline wheel directory containing exactly one compatible pycryptodome wheel')
    parser.add_argument('--user', help='desktop owner UID or name (default: SUDO_UID or current user)')
    parser.add_argument('--apply', action='store_true', help='perform the reviewed installation; requires root')
    args = parser.parse_args(argv)
    try:
        owner = target_owner(args.user)
        wheels = collect_wheels(args.source, args.wheelhouse)
        result = apply(owner, wheels) if args.apply else make_plan(owner, wheels)
    except (InstallError, OSError) as exc:
        message = str(exc)
        print(json.dumps({'ok': False, 'code': message.partition(':')[0], 'error': message}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
