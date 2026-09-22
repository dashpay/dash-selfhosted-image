#!/usr/bin/env python3
"""Fail early on toolchain drift or a privileged runner runtime."""
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

def require(condition, message):
    if not condition:
        raise SystemExit(message)

def run(*args):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT).strip()

require(os.getuid() == 1001, 'Expected non-root runner uid 1001')
require(shutil.which('sudo') is None, 'sudo must not be installed')
require(shutil.which('docker') is None, 'Docker CLI must not be installed')
require(not Path('/var/run/docker.sock').exists(), 'Host Docker socket must not be mounted')
lock = json.loads(Path('/opt/ci/image.lock.json').read_text())
for command, expected in [
    (['rustc', '--version'], 'rustc ' + lock['rust_version']),
    (['cargo', 'llvm-cov', '--version'], 'cargo-llvm-cov 0.9.1'),
    (['cargo', 'nextest', '--version'], 'cargo-nextest 0.9.144'),
    (['cargo', 'machete', '--version'], 'cargo-machete 0.9.2'),
    (['cargo', 'ndk', '--version'], 'cargo-ndk 4.1.2'),
    (['protoc', '--version'], 'libprotoc 32.0'),
]:
    actual = run(*command)
    require(actual == expected or actual.startswith(expected + ' '), f'Unexpected version: {actual}')
    print(actual)
for command in ['clang', 'clang++', 'llvm-config', 'cmake', 'gh', 'git', 'python3', 'jq', 'zip', 'unzip', 'gpg', 'pkg-config', 'javac', 'adb', 'sdkmanager', 'avdmanager', 'emulator']:
    require(shutil.which(command), f'Missing {command}')
require('javac 17.' in run('javac', '-version'), 'JDK 17 required')
for artifact in lock['artifacts']:
    if not artifact.get('build_only'):
        require(Path(artifact['destination']).exists(), f"Missing {artifact['name']}")
with tempfile.TemporaryDirectory() as tmp:
    p = Path(tmp)
    (p / 'smoke.rs').write_text('fn main() { println!("rust-ok"); }\n')
    run('rustc', str(p / 'smoke.rs'), '-o', str(p / 'rust-smoke'))
    require(run(str(p / 'rust-smoke')) == 'rust-ok', 'Rust compile/run failed')
    (p / 'smoke.c').write_text('#include <gmp.h>\n#include <openssl/crypto.h>\n#include <snappy-c.h>\nint main(void) { mpz_t n; mpz_init(n); mpz_clear(n); return OpenSSL_version_num() == 0 || snappy_max_compressed_length(1) == 0; }\n')
    run('clang', str(p / 'smoke.c'), '-lgmp', '-lcrypto', '-lsnappy', '-o', str(p / 'clang-smoke'))
    run(str(p / 'clang-smoke'))
    ndk_linker = Path(os.environ['ANDROID_NDK_HOME']) / 'toolchains/llvm/prebuilt/linux-x86_64/bin/x86_64-linux-android24-clang'
    run('rustc', '--target', 'x86_64-linux-android', '-C', 'linker=' + str(ndk_linker), str(p / 'smoke.rs'), '-o', str(p / 'android-smoke'))
    (p / 'Smoke.java').write_text('public class Smoke { public static void main(String[] args) { System.out.println("java-ok"); } }\n')
    run('javac', str(p / 'Smoke.java'))
    require(run('java', '-cp', tmp, 'Smoke') == 'java-ok', 'Java compile/run failed')
    (p / 'smoke.proto').write_text('syntax = "proto3"; message Smoke { string value = 1; }\n')
    run('protoc', '-I' + tmp, '--descriptor_set_out=' + str(p / 'smoke.pb'), str(p / 'smoke.proto'))
if '--confined' in sys.argv:
    status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
    for field in ['CapInh', 'CapPrm', 'CapEff', 'CapBnd', 'CapAmb']:
        require(int(status[field].strip(), 16) == 0, f'{field} capabilities must be empty')
    require(status['NoNewPrivs'].strip() == '1', 'no-new-privileges must be enabled')
    require(status['Seccomp'].strip() == '2', 'Seccomp filtering must be enabled')
if '--kvm' in sys.argv:
    with open('/dev/kvm', 'rb+', buffering=0) as device:
        require(fcntl.ioctl(device, 0xAE00, 0) == 12, 'Unexpected KVM API')
        vm = fcntl.ioctl(device, 0xAE01, 0)
        os.close(vm)
    run('emulator', '-accel-check')
    print('KVM API and VM creation verified')
print('IMAGE_CONTRACT_OK')
