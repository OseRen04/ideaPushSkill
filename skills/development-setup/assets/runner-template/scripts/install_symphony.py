"""Install a release executable only after checking its pinned SHA-256."""

import hashlib
import os
from pathlib import Path
import platform
import sys
import urllib.request


RELEASE = "v0.0.3"
ASSETS = {
    ("Linux", "x86_64"): ("linux_x86_64", "ea35a04a54a6d37c0cafe3f195da871e47614a8c05765b90dbb4cac32e1435ee"),
    ("Linux", "aarch64"): ("linux_arm64", "bf204639b836d378f5d2ddb389f5d76602a62e9d1b115bdbefb07916848ae066"),
    ("Darwin", "arm64"): ("macos_arm64", "b85d78b25cd5cacff92424416f6a3af7cafee5d675f56b5cd26d22d601c2026d"),
    ("Darwin", "x86_64"): ("macos_x86_64", "e7db24d8b35d99a96b96db51e50c7974ab9a8ae06fd05abf6d03ead6bf183d90"),
}


def install(destination=None):
    system = (platform.system(), platform.machine())
    if system not in ASSETS:
        raise SystemExit("Unsupported Symphony platform")
    suffix, expected = ASSETS[system]
    name = "symphony-%s-%s" % (RELEASE, suffix)
    request = urllib.request.Request(
        "https://github.com/openai/symphony/releases/download/%s/%s" % (RELEASE, name),
        headers={"User-Agent": "idea-vault-runner"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        content = response.read(200 * 1024 * 1024)
    if hashlib.sha256(content).hexdigest() != expected:
        raise SystemExit("Symphony checksum mismatch")
    destination = Path(destination or Path.home() / ".local/bin/symphony")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)
    destination.chmod(0o700)
    if os.environ.get("GITHUB_PATH"):
        with open(os.environ["GITHUB_PATH"], "a", encoding="utf-8") as stream:
            stream.write(str(destination.parent) + "\n")
    return destination


if __name__ == "__main__":
    print(install(sys.argv[1] if len(sys.argv) > 1 else None))
