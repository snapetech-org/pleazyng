#!/usr/bin/env python3
"""Check the core24 USN-9187-1 fixes and the libraries the Snap launcher loads."""

from pathlib import Path
import subprocess
import sys
import tempfile

import yaml


TRIPLETS = {"amd64": "x86_64-linux-gnu", "arm64": "aarch64-linux-gnu"}
LIBRARIES = {"libssh-4": "libssh.so.4", "libssh-gcrypt-4": "libssh-gcrypt.so.4"}
MINIMUM = "0.10.6-2ubuntu0.6"


def verify(snap: Path, arch: str) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        staged = Path("usr/lib") / TRIPLETS[arch]
        bundle = Path("opt/pleazyng/lib")
        subprocess.run([
            "unsquashfs", "-no-progress", "-no-xattrs", "-processors", "2",
            "-d", directory, str(snap), "snap/manifest.yaml",
            str(staged / "libssh*.so*"), str(bundle / "libssh*.so*"),
        ], check=True)
        manifest = yaml.safe_load((root / "snap/manifest.yaml").read_text())
        if manifest["base"] != "core24":
            raise SystemExit("Expected a core24 Snap for the noble package check")
        packages = manifest["primed-stage-packages"]
        for package, soname in LIBRARIES.items():
            versions = [p.split("=", 1)[1] for p in packages if p.startswith(package + "=")]
            if len(versions) != 1 or subprocess.run(
                ["dpkg", "--compare-versions", versions[0], "ge", MINIMUM]
            ).returncode:
                raise SystemExit(f"USN-9187-1 requires {package} >= {MINIMUM}, found {versions}")
            archive_lib = (root / staged / soname).resolve(strict=True)
            if not archive_lib.is_relative_to(root / staged) or not archive_lib.is_file():
                raise SystemExit(f"Invalid archive library for {package}")
            payload = archive_lib.read_bytes()
            runtime_lib = (root / bundle / soname).resolve(strict=True)
            if not payload or not runtime_lib.is_relative_to(root) or runtime_lib.read_bytes() != payload:
                raise SystemExit(f"Bundled {soname} differs from the staged {package}")
            print(f"{arch}: verified runtime {soname} against {package}={versions[0]}")


if __name__ == "__main__":
    verify(Path(sys.argv[1]), sys.argv[2])
