"""Read-only version check; does not install packages or change project data."""

import re
import sys
from importlib import metadata
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PIN_PATTERN = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([A-Za-z0-9][A-Za-z0-9.!+_-]*)")


def normalize_name(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def read_pins(path):
    pins = {}
    for number, raw_line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = PIN_PATTERN.fullmatch(line)
        if match is None:
            raise ValueError(f"{Path(path).name}:{number}: expected package==version")
        name, version = match.groups()
        name = normalize_name(name)
        if name in pins:
            raise ValueError(f"{Path(path).name}:{number}: duplicate package {name}")
        pins[name] = version
    if not pins:
        raise ValueError(f"{Path(path).name}: no version pins found")
    return pins


def check_environment(locked, direct, *, version_reader=None, python_version=None):
    version_reader = metadata.version if version_reader is None else version_reader
    python_version = sys.version_info[:2] if python_version is None else python_version
    issues = []
    if tuple(python_version[:2]) != (3, 11):
        issues.append("Python 3.11 is required for this baseline")
    for name, version in direct.items():
        if locked.get(name) != version:
            issues.append(f"direct dependency differs from snapshot: {name}=={version}")
    for name, expected in locked.items():
        try:
            actual = version_reader(name)
        except metadata.PackageNotFoundError:
            issues.append(f"missing: {name}=={expected}")
            continue
        if actual != expected:
            issues.append(f"version differs: {name}, expected {expected}, installed {actual}")
    return issues


def main():
    try:
        locked = read_pins(PROJECT_ROOT / "requirements.lock.txt")
        direct = read_pins(PROJECT_ROOT / "requirements.txt")
        issues = check_environment(locked, direct)
    except (OSError, ValueError) as error:
        print(f"Environment check failed: {error}", file=sys.stderr)
        return 1
    if issues:
        print("Environment check failed:")
        for issue in issues:
            print(f"- {issue}")
        return 1
    print(f"OK: Python 3.11; all {len(locked)} recorded package versions match.")
    print("Read-only check. Extra packages, artifact hashes and dependency compatibility are not checked.")
    print("Next: python -m pip check; python -m unittest discover -s tests -v")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
