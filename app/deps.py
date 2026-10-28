"""Prüft beim Start, ob alle Pakete da sind, und installiert nur fehlende – mit sichtbarer Ausgabe."""
import importlib.util
import subprocess
import sys
from pathlib import Path

# Importname -> pip-Paket
REQUIRED = {
    "pyVoIP": "pyVoIP==1.6.8",
    "anthropic": "anthropic>=0.40",
    "requests": "requests>=2.31",
    "numpy": "numpy>=1.26",
    "tzdata": "tzdata",
    "truststore": "truststore>=0.9",
}


def ensure():
    missing = [pkg for mod, pkg in REQUIRED.items() if importlib.util.find_spec(mod) is None]
    if not missing:
        return
    print(f"\033[93mInstalliere fehlende Pakete: {', '.join(missing)}\033[0m", flush=True)
    rc = subprocess.call([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", *missing])
    if rc != 0:
        print(f"\033[91mInstallation fehlgeschlagen (Code {rc}). Ausgabe oben prüfen.\033[0m", flush=True)
        sys.exit(rc)
    importlib.invalidate_caches()
    print("\033[92mPakete installiert.\033[0m", flush=True)


if __name__ == "__main__":
    ensure()
    print(Path(sys.executable))
