"""Install an integrity-checked npm CLI when only a Node executable is available."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import tarfile
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit


def install(destination: Path) -> Path:
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen("https://registry.npmjs.org/npm/10.9.2", timeout=45) as response:
        metadata = json.load(response)
    url = metadata["dist"]["tarball"]
    if urlsplit(url).hostname != "registry.npmjs.org" or urlsplit(url).scheme != "https":
        raise ValueError("Unexpected npm package host")
    with urllib.request.urlopen(url, timeout=60) as response:
        payload = response.read()
    integrity = "sha512-" + base64.b64encode(hashlib.sha512(payload).digest()).decode()
    if integrity != metadata["dist"]["integrity"]:
        raise ValueError("npm integrity mismatch")
    archive = destination / "npm-10.9.2.tgz"
    archive.write_bytes(payload)
    with tarfile.open(archive) as package:
        package.extractall(destination, filter="data")
    return destination / "package/bin/npm-cli.js"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--destination", default=".eviforge/integration/tooling/npm")
    args = parser.parse_args()
    print(json.dumps({"npm_cli": str(install(Path(args.destination))), "version": "10.9.2"}))
