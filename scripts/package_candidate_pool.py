#!/usr/bin/env python3
"""Build an offline runtime bundle; never install, schedule, or register workers."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import tarfile

from image_contract import read_json, require, validate_lock

# Retain the repository layout: image_contract resolves this reviewed data lock
# relative to scripts/. Copying Python modules alone is not a complete runtime.
RUNTIME_FILES = (
    "scripts/candidate_pool.py",
    "scripts/candidate_pool_common.py",
    "scripts/candidate_pool_host.py",
    "scripts/candidate_runner.py",
    "scripts/platform_request.py",
    "scripts/image_contract.py",
    "client-codegen/lock.json",
)


def build_bundle(root):
    root = Path(root).resolve()
    files = {}
    for name in RUNTIME_FILES:
        path = root / name
        require(path.is_file() and not path.is_symlink()
                and path.resolve() == path, "Missing or symlinked runtime input: " + name)
        data = path.read_bytes()
        require(len(data) <= 256 * 1024, "Oversized runtime input: " + name)
        if name.endswith(".py"):
            compile(data, name, "exec")
        files[name] = data
    # This checks the reviewed codegen lock against the image's actual contract,
    # rather than treating an arbitrary JSON file as a sufficient dependency.
    lock = read_json(root / "image.lock.json")
    require(lock.get("client_codegen") == json.loads(files["client-codegen/lock.json"]),
            "Bundled codegen lock differs from the image contract")
    validate_lock(lock)
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    files["runtime-files.sha256.json"] = (json.dumps(hashes, sort_keys=True, indent=2) + "\n").encode()
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name, data in sorted(files.items()):
            item = tarfile.TarInfo(name)
            item.size = len(data)
            item.mode = 0o600
            item.mtime = 0
            archive.addfile(item, io.BytesIO(data))
    return output.getvalue()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    data = build_bundle(Path(__file__).resolve().parents[1])
    # A bundle is a new artifact. Existing deployment files are never replaced.
    with args.output.open("xb") as stream:
        stream.write(data)
    print(json.dumps({"output": str(args.output), "sha256": hashlib.sha256(data).hexdigest(),
                      "runtime_files": list(RUNTIME_FILES), "installed": False}))


if __name__ == "__main__":
    main()
