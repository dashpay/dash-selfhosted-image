#!/usr/bin/env python3
"""Install only the immutable, hash-checked downloads in image.lock.json."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tarfile
import tempfile
import urllib.request
import zipfile

lock = json.loads(Path('/build/image.lock.json').read_text())
for artifact in lock['artifacts']:
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / 'download'
        digest = hashlib.sha256()
        with urllib.request.urlopen(artifact['url'], timeout=120) as response, archive.open('wb') as output:
            while chunk := response.read(1024 * 1024):
                digest.update(chunk)
                output.write(chunk)
        if digest.hexdigest() != artifact['sha256']:
            raise SystemExit(f"Checksum mismatch: {artifact['name']}")
        destination = Path(artifact['destination'])
        destination.parent.mkdir(parents=True, exist_ok=True)
        if artifact['format'] == 'file':
            shutil.copyfile(archive, destination)
            destination.chmod(0o755)
            continue
        extracted = Path(tmp) / 'extracted'
        extracted.mkdir()
        if artifact['format'] == 'zip':
            with zipfile.ZipFile(archive) as z:
                for member in z.infolist():
                    path = extracted / member.filename
                    if not path.resolve().is_relative_to(extracted.resolve()):
                        raise SystemExit('Unsafe archive path')
                    mode = member.external_attr >> 16
                    if stat.S_ISLNK(mode):
                        target = z.read(member).decode()
                        if not (path.parent / target).resolve().is_relative_to(extracted.resolve()):
                            raise SystemExit('Unsafe archive symlink')
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.symlink_to(target)
                        continue
                    z.extract(member, extracted)
                    if mode & 0o111 and not member.is_dir():
                        path.chmod(0o755)
        else:
            with tarfile.open(archive) as t:
                t.extractall(extracted, filter='data')
        if 'binary' in artifact:
            matches = [p for p in extracted.rglob(artifact['binary']) if p.is_file()]
            if len(matches) != 1:
                raise SystemExit(f"Ambiguous executable: {artifact['name']}")
            shutil.copyfile(matches[0], destination)
            destination.chmod(0o755)
        else:
            source = extracted / artifact.get('archive_root', '')
            shutil.copytree(source, destination, dirs_exist_ok=True, symlinks=True)
        if 'package_xml' in artifact:
            (destination / 'package.xml').write_text(artifact['package_xml'])
        print(f"Installed {artifact['name']} ({artifact['sha256']})", flush=True)
