#!/usr/bin/env python3
"""Exercise every native client generator without root, Docker or a network."""
from pathlib import Path
import subprocess
import sys
import tempfile

root = Path(sys.argv[1])
with tempfile.TemporaryDirectory() as directory:
    work = Path(directory)
    (work / 'smoke.proto').write_text(
        'syntax = "proto3"; package smoke; message Request { string value = 1; } '
        'service Smoke { rpc Get(Request) returns (Request); }\n')
    subprocess.run([
        str(root / 'bin/protoc'), '-I' + directory,
        '--js_out=import_style=commonjs:' + directory,
        '--objc_out=' + directory, '--python_out=' + directory,
        '--plugin=protoc-gen-grpc-java=' + str(root / 'bin/protoc-gen-grpc-java'),
        '--grpc-java_out=' + directory,
        '--plugin=protoc-gen-python-grpc=' + str(root / 'bin/grpc_python_plugin'),
        '--python-grpc_out=' + directory, 'smoke.proto',
    ], check=True)
    # Objective-C's builtin output and service plugin are separate invocations.
    subprocess.run([str(root / 'bin/protoc'), '-I' + directory,
                    '--plugin=protoc-gen-grpc=' + str(root / 'bin/grpc_objective_c_plugin'),
                    '--grpc_out=' + directory, 'smoke.proto'], check=True)
    for name in ('smoke_pb.js', 'Smoke.pbobjc.h', 'Smoke.pbrpc.h',
                 'smoke_pb2.py', 'smoke_pb2_grpc.py', 'smoke/SmokeGrpc.java'):
        if not (work / name).is_file():
            raise SystemExit('Missing generated output: ' + name)
print('CLIENT_CODEGEN_OK')
