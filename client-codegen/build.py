#!/usr/bin/env python3
"""Build the legacy DAPI client generators without root or a Docker daemon."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request

HERE = Path(__file__).resolve().parent
BINARIES = ('protoc', 'grpc_objective_c_plugin', 'grpc_python_plugin', 'protoc-gen-grpc-java')


def build(destination, jobs):
    lock = json.loads((HERE / 'lock.json').read_text())
    destination = destination.resolve()
    if destination.exists():
        raise ValueError(f'Destination already exists: {destination}')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='client-codegen-') as tmp:
        root = Path(tmp)
        sources = root / 'sources'
        sources.mkdir()
        for index, source in enumerate(lock['sources']):
            archive = root / f'{index}.tar.gz'
            with urllib.request.urlopen(source['url'], timeout=120) as response, archive.open('wb') as output:
                shutil.copyfileobj(response, output)
            if hashlib.sha256(archive.read_bytes()).hexdigest() != source['sha256']:
                raise ValueError(f"Checksum mismatch: {source['url']}")
            with tarfile.open(archive) as tar:
                tar.extractall(sources, filter='data')
        build_dir = root / 'build'
        subprocess.run(['cmake', '-S', str(HERE), '-B', str(build_dir),
                        f'-DSOURCES={sources}', '-DCMAKE_BUILD_TYPE=Release',
                        '-DCMAKE_POLICY_VERSION_MINIMUM=3.5'], check=True)
        subprocess.run(['cmake', '--build', str(build_dir), '--parallel', str(jobs),
                        '--target', *BINARIES], check=True)
        # Publish only complete installations, on the destination filesystem.
        with tempfile.TemporaryDirectory(prefix='.codegen-', dir=destination.parent) as staging:
            stage = Path(staging) / 'toolchain'
            (stage / 'bin').mkdir(parents=True)
            for binary in BINARIES:
                source = build_dir / ('protobuf' if binary == 'protoc' else '') / binary
                shutil.copy2(source, stage / 'bin' / binary)
            shutil.copytree(sources / 'protobuf-3.18.1/src/google', stage / 'include/google',
                            ignore=lambda path, names: [name for name in names
                                if (Path(path) / name).is_file() and not name.endswith('.proto')])
            licenses = stage / 'share/licenses'
            licenses.mkdir(parents=True)
            for component in ('protobuf-3.18.1', 'grpc-1.46.3', 'grpc-java-1.42.1'):
                shutil.copy2(sources / component / 'LICENSE', licenses / (component + '.txt'))
            (stage / 'lock.json').write_text(json.dumps(lock, indent=2) + '\n')
            version = subprocess.check_output([str(stage / 'bin/protoc'), '--version'], text=True).strip()
            if version != 'libprotoc ' + lock['versions']['protobuf']:
                raise ValueError(f'Unexpected compiler: {version}')
            stage.rename(destination)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--jobs', type=int, default=min(os.cpu_count() or 2, 8))
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error('--jobs must be positive')
    build(args.destination, args.jobs)
