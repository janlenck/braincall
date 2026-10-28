"""Consistency checks for the Home Assistant add-on in claude_telefon/ (run in CI)."""
import re
import sys
from pathlib import Path

import yaml

ADDON = Path(__file__).resolve().parents[2] / "claude_telefon"
# Supervisor accepts build images only as namespace/name[:tag] (supervisor/apps/validate.py)
RE_IMAGE = re.compile(r"^([a-zA-Z\-\.:\d{}]+/)*?([\-\w{}]+)/([\-\w{}]+)(:[\.\-\w{}]+)?$")

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
build = yaml.safe_load((ADDON / "build.yaml").read_text(encoding="utf-8"))
for arch, image in build["build_from"].items():
    if not RE_IMAGE.match(image):
        errors.append(f"build_from {arch}: {image!r} must be namespace/name[:tag], e.g. library/python:3.12-slim")
if set(build["build_from"]) != set(config["arch"]):
    errors.append("build.yaml architectures differ from config.yaml arch")

print("\n".join(errors) or "add-on config ok")
sys.exit(1 if errors else 0)
