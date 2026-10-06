"""Build the shared, headless Windows interaction component outside source."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid

from runtime_paths import DATA_ROOT, PROJECT_ROOT
from isolated_interaction import verify_package


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root', type=Path, default=DATA_ROOT)
    parser.add_argument('--sdk', type=Path)
    args = parser.parse_args()
    root = args.data_root.resolve()
    if root.is_relative_to(PROJECT_ROOT): parser.error('Build outside source')
    sdk = args.sdk or root / 'toolchains' / 'dotnet-10.0.401' / 'dotnet.exe'
    # Publish into a fresh directory. An already running note keeps its original
    # binary; only a user-initiated restart selects the new, verified release.
    package_root, work = root / 'window-link', root / 'dotnet-build'
    release = uuid.uuid4().hex
    dist = package_root / 'releases' / release
    env = {**os.environ, 'DOTNET_CLI_TELEMETRY_OPTOUT': '1', 'DOTNET_CLI_HOME': str(root / 'toolchains' / 'dotnet-home')}
    subprocess.run([str(sdk), 'publish', str(PROJECT_ROOT / 'native' / 'WindowLink' / 'WindowLink.csproj'),
        '-c', 'Release', '--self-contained', 'false', '--nologo', '-o', str(dist),
        f'-p:NativeOutputRoot={work}{os.sep}', '-p:RestoreLockedMode=true'], env=env, check=True)
    licenses = dist / 'licenses'; licenses.mkdir(exist_ok=True)
    for package, version in (('flaui.core','5.0.0'),('flaui.uia3','5.0.0'),('interop.uiautomationclient','10.19041.0'),('system.management','8.0.0')):
        for source in (work / 'packages' / package / version).iterdir():
            if source.is_file() and ('license' in source.name.lower() or source.suffix == '.nuspec'):
                shutil.copyfile(source, licenses / (package + '-' + source.name))
    files = {p.relative_to(dist).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted(dist.rglob('*')) if p.is_file() and p.name != 'build-manifest.json'}
    manifest = dist / 'build-manifest.json'
    manifest.write_text(json.dumps({'version': '0.1.3', 'files': files}, indent=2), encoding='utf-8')
    verify_package(dist)
    pending = package_root / ('current-' + release + '.json')
    pending.write_text(json.dumps({'release': release, 'manifest_sha256': hashlib.sha256(manifest.read_bytes()).hexdigest()}), encoding='utf-8')
    pending.replace(package_root / 'current.json')
    print(json.dumps({'ok': True, 'component': 'window-link', 'version': '0.1.3', 'restart_required': True}))


if __name__ == '__main__': main()
