"""Checks at startup that all packages are present and new enough; installs only missing or outdated ones,
with visible output."""
import importlib.metadata
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

# import name -> pip package
REQUIRED = {
    "pyVoIP": "pyVoIP==1.6.8",
    "anthropic": "anthropic>=0.80",  # web search and web fetch result blocks (#52)
    "requests": "requests>=2.31",
    "websocket": "websocket-client>=1.7",
    "numpy": "numpy>=1.26",
    "tzdata": "tzdata",
    "truststore": "truststore>=0.9",
    "audioop": "audioop-lts",  # built in up to Python 3.12, so only installed on 3.13+
}


def _version(text: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", text)[:3])


def outdated(pkg: str) -> bool:
    """True if pkg ("name>=x.y") is installed in an older version; unknown versions count as new enough."""
    m = re.fullmatch(r"([\w.-]+)>=([\d.]+)", pkg)
    if not m:
        return False
    try:
        return _version(importlib.metadata.version(m.group(1))) < _version(m.group(2))
    except importlib.metadata.PackageNotFoundError:
        return False


def ensure():
    missing = [pkg for mod, pkg in REQUIRED.items() if importlib.util.find_spec(mod) is None or outdated(pkg)]
    if not missing:
        return
    print(f"\033[93mInstalling missing or outdated packages: {', '.join(missing)}\033[0m", flush=True)
    rc = subprocess.call([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--upgrade",
                          *missing])
    if rc != 0:
        print(f"\033[91mInstallation failed (code {rc}). Check the output above.\033[0m", flush=True)
        sys.exit(rc)
    importlib.invalidate_caches()
    print("\033[92mPackages installed.\033[0m", flush=True)


if __name__ == "__main__":
    ensure()
    print(Path(sys.executable))
