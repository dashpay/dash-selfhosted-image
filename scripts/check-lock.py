#!/usr/bin/env python3
"""Verify the standalone locked inputs and their generated Dockerfile."""
from pathlib import Path
from image_contract import read_json, render, validate_lock

root = Path(__file__).resolve().parent.parent
lock = validate_lock(read_json(root / 'image.lock.json'))
expected = render(lock, (root / 'Dockerfile.template').read_text())
if (root / 'Dockerfile').read_text() != expected:
    raise SystemExit('Dockerfile differs from Dockerfile.template + image.lock.json; regenerate it.')
print(f"Validated locked base, packages and {len(lock['artifacts'])} SHA-256 artifacts")
