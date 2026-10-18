"""Store the checkout CVV in this service's private .env file."""

import getpass
import os
import re
import tempfile
from pathlib import Path


ENV_FILE = Path(__file__).with_name(".env")
CVV_LINE = re.compile(r"^\s*(?:export\s+)?SHUFERSAL_CVV\s*=", re.MULTILINE)


def set_local_cvv(cvv: str, env_file: Path = ENV_FILE) -> None:
    if not (len(cvv) in (3, 4) and cvv.isascii() and cvv.isdecimal()):
        raise ValueError("CVV must contain exactly 3 or 4 ASCII digits.")
    if env_file.is_symlink():
        raise ValueError("The .env path must not be a symlink.")

    content = env_file.read_text(encoding="utf-8") if env_file.exists() else ""
    lines = content.splitlines(keepends=True)
    updated = []
    inserted = False
    for line in lines:
        if CVV_LINE.match(line):
            if not inserted:
                updated.append(f"SHUFERSAL_CVV={cvv}\n")
                inserted = True
            continue
        updated.append(line)
    if not inserted:
        if updated and not updated[-1].endswith("\n"):
            updated.append("\n")
        updated.append(f"SHUFERSAL_CVV={cvv}\n")

    descriptor, temporary_name = tempfile.mkstemp(prefix=".env.", dir=env_file.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
            temporary.writelines(updated)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, env_file)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


if __name__ == "__main__":
    value = getpass.getpass("Shufersal checkout CVV: ")
    set_local_cvv(value)
    print("Local checkout CVV saved. Restart the Shufersal service to load it.")
