"""Package each declared workload source directory as a reproducible ZIP."""

import zipfile
from pathlib import Path

from manifest import read_manifest

root = Path(__file__).resolve().parent.parent
manifest = read_manifest(root / "manifest.template.json", allow_placeholders=True)
output = root / "workloads"
output.mkdir(exist_ok=True)
for workload in manifest.get("workloads", []):
    source = root / "workload" / workload["id"]
    paths = sorted(source.rglob("*"))
    for path in paths:
        if path.is_symlink():
            raise SystemExit(f"Workload sources cannot contain symlinks: {path}")
    files = [path for path in paths if path.is_file()]
    if not files:
        raise SystemExit(f"Workload {workload['id']} has no files in {source}")
    target = output / workload["asset"]
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_STORED) as archive:
        for path in files:
            name = path.relative_to(source).as_posix()
            entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            entry.create_system = 3
            mode = 0o100755 if name == workload["entrypoint"] else 0o100644
            entry.external_attr = mode << 16
            archive.writestr(entry, path.read_bytes())
    print(target.relative_to(root))
