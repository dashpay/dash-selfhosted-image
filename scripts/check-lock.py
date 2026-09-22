#!/usr/bin/env python3
"""Validate the build contract without downloading or executing artifacts."""
import datetime
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET

root = Path(__file__).resolve().parent.parent
lock = json.loads((root / 'image.lock.json').read_text())
dockerfile = (root / 'Dockerfile').read_text()
assert lock['schema'] == 1
assert lock['platform'] == 'linux/amd64'
assert re.fullmatch(r'ubuntu:24\.04@sha256:[a-f0-9]{64}', lock['ubuntu_image'])
assert dockerfile.startswith('FROM ' + lock['ubuntu_image'] + '\n')
assert 'ARG APT_SNAPSHOT=' + lock['apt_snapshot'] + '\n' in dockerfile
epoch = int(datetime.datetime.strptime(lock['apt_snapshot'], '%Y%m%dT%H%M%SZ').replace(tzinfo=datetime.timezone.utc).timestamp())
assert f'ARG SOURCE_DATE_EPOCH={epoch}\n' in dockerfile
assert re.fullmatch(r'[a-f0-9]{64}', lock['rust_manifest_sha256'])
assert re.fullmatch(r'[a-f0-9]{64}', lock['bootstrap_ca']['sha256'])
assert f"ADD --checksum=sha256:{lock['bootstrap_ca']['sha256']} {lock['bootstrap_ca']['url']} /tmp/bootstrap-ca.deb\n" in dockerfile
names = set()
for artifact in lock['artifacts']:
    assert artifact['name'] not in names
    names.add(artifact['name'])
    assert artifact['url'].startswith('https://')
    assert re.fullmatch(r'[a-f0-9]{64}', artifact['sha256'])
    assert artifact['destination'].startswith('/opt/')
    assert '..' not in Path(artifact['destination']).parts
    assert artifact['format'] in ('tar', 'zip', 'file')
    if 'package_xml' in artifact:
        package = ET.fromstring(artifact['package_xml'])
        assert package.find('localPackage').attrib['path'] == artifact['name']
assert {'runner', 'rustup-init', 'cargo-llvm-cov', 'cargo-nextest', 'cargo-machete', 'cargo-ndk', 'protoc', 'emulator', 'system-images;android-35;default;x86_64'} <= names
print(f'Locked base, apt snapshot, Rust manifest and {len(names)} SHA-256 artifacts validated')
