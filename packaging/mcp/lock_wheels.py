"""Generate the architecture-specific lock from a resolved, reviewed wheelhouse."""
import hashlib
from pathlib import Path
import sys
import zipfile
from email.parser import BytesParser

rows = []
for wheel in Path(sys.argv[1]).glob("*.whl"):
    with zipfile.ZipFile(wheel) as archive:
        name = next(name for name in archive.namelist() if name.endswith(".dist-info/METADATA"))
        metadata = BytesParser().parsebytes(archive.read(name))
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    rows.append("{}=={} --hash=sha256:{}".format(metadata["Name"].lower(), metadata["Version"], digest))
print("# Rocky Linux 10 x86_64, Python 3.12. Regenerate only when updating the reviewed SDK runtime.")
print("\n".join(sorted(rows)))
