"""Restore bundled immutable samples after cloning the private competition repository."""
from pathlib import Path
import hashlib
import json
import zipfile

def restore(root=None):
    root = Path(root or Path(__file__).resolve().parents[1]).resolve()
    index = json.loads((root / "data/offline-samples-index.json").read_text())
    archive = root / index["archive"]
    if hashlib.sha256(archive.read_bytes()).hexdigest() != index["sha256"]:
        raise ValueError("Sample archive checksum mismatch")
    expected = {row["path"]: row for row in index["files"]}
    with zipfile.ZipFile(archive) as bundle:
        if set(bundle.namelist()) != set(expected):
            raise ValueError("Unexpected sample archive members")
        for name, row in expected.items():
            target = (root / name).resolve()
            if not target.is_relative_to(root):
                raise ValueError("Unsafe sample archive path")
            content = bundle.read(name)
            if len(content) != row["bytes"] or hashlib.sha256(content).hexdigest() != row["sha256"]:
                raise ValueError("Sample member checksum mismatch")
            if target.exists() and target.read_bytes() != content:
                raise ValueError("Existing modified sample retained: " + name)
        restored = 0
        for name in expected:
            target = root / name
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(bundle.read(name))
                restored += 1
    return {"members_verified": len(expected), "files_restored": restored}

if __name__ == "__main__":
    print(json.dumps(restore(), ensure_ascii=False))

