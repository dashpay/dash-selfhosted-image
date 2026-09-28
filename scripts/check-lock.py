#!/usr/bin/env python3
"""Verify the standalone locked inputs and their generated Dockerfile."""
from pathlib import Path
from image_contract import read_json, render, require, validate_lock

root = Path(__file__).resolve().parent.parent
lock = validate_lock(read_json(root / 'image.lock.json'))
expected = render(lock, (root / 'Dockerfile.template').read_text())
if (root / 'Dockerfile').read_text() != expected:
    raise SystemExit('Dockerfile differs from Dockerfile.template + image.lock.json; regenerate it.')
print(f"Validated locked base, packages and {len(lock['artifacts'])} SHA-256 artifacts")

arm = validate_lock(read_json(root / 'image.arm64.lock.json'))
require(arm['platform'] == 'linux/arm64' and arm['profile'] == 'rust',
        'ARM64 lock must use the Linux Rust-only profile')
for field in ('apt_snapshot', 'rust_version', 'rust_manifest_sha256', 'java_major', 'client_codegen'):
    require(arm.get(field) == lock.get(field), f'ARM64/AMD64 shared input drift: {field}')
for tool, version in arm['versions'].items():
    require(version == lock['versions'][tool], f'ARM64/AMD64 tool version drift: {tool}')
expected = render(arm, (root / 'Dockerfile.template').read_text(), 'image.arm64.lock.json')
require((root / 'Dockerfile.arm64').read_text() == expected,
        'Dockerfile.arm64 differs from the template and ARM64 lock; regenerate it.')
print('Validated native ARM64 Rust profile and shared tool-version parity')
