"""Consistency checks for the Home Assistant add-on in braincall/ (run in CI)."""
import re
import sys
from pathlib import Path

import yaml

ADDON = Path(__file__).resolve().parents[2] / "braincall"

errors = []
config = yaml.safe_load((ADDON / "config.yaml").read_text(encoding="utf-8"))
options, schema = config["options"], config["schema"]
if not re.fullmatch(r"\d+\.\d+\.\d+", str(config["version"])):
    errors.append(f"version {config['version']!r} is not x.y.z (the release tag is v<version>)")
required = {k for k, v in schema.items() if not (isinstance(v, str) and v.endswith("?"))}
if missing := required - set(options):
    errors.append(f"required in schema but no default in options: {sorted(missing)}")
if extra := set(options) - set(schema):
    errors.append(f"options without schema: {sorted(extra)}")
for path in sorted((ADDON / "translations").glob("*.yaml")):
    names = set(yaml.safe_load(path.read_text(encoding="utf-8"))["configuration"])
    if diff := names ^ set(schema):
        errors.append(f"{path.name} does not match the schema: {sorted(diff)}")
# the watchdog asks /health on the ingress port, the only port of the app on the Supervisor's network (#37)
if config.get("watchdog") != f"http://[HOST]:[PORT:{config['ingress_port']}]/health":
    errors.append(f"watchdog {config.get('watchdog')!r} must be http://[HOST]:[PORT:<ingress_port>]/health")
main_py = (ADDON.parent / "app" / "main.py").read_text(encoding="utf-8")
if f"INGRESS_PORT = {config['ingress_port']} " not in main_py:
    errors.append(f"ingress_port {config['ingress_port']} differs from INGRESS_PORT in app/main.py")
# build.yaml is deprecated by the Supervisor: the base image is fixed in the Dockerfile, BUILD_FROM is not used
if (ADDON / "build.yaml").exists():
    errors.append("build.yaml is deprecated: put the base image into the Dockerfile (FROM)")
froms = [line.split()[1] for line in (ADDON / "Dockerfile").read_text(encoding="utf-8").splitlines()
         if line.startswith("FROM ")]
if not froms or any("$" in image for image in froms):
    errors.append(f"Dockerfile FROM {froms} must name a fixed image, not ${{BUILD_FROM}}")

print("\n".join(errors) or "add-on config ok")
sys.exit(1 if errors else 0)
