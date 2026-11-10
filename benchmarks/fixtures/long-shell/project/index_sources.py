from __future__ import annotations

import json
import os
import time
from pathlib import Path

root = Path(__file__).resolve().parent
build = root / "build"
build.mkdir(exist_ok=True)
with (build / "invocations.log").open("a", encoding="utf-8") as stream:
    stream.write(f"started pid={os.getpid()}\n")
    stream.flush()

records = []
for phase in range(1, 9):
    (build / f"phase-{phase}.started").write_text("started\n", encoding="utf-8")
    print(f"phase {phase}/8", flush=True)
    time.sleep(1)
    records.append({"id": f"item-{phase:03d}", "checksum": f"sha256:fixture-{phase:03d}"})

(build / "index.json").write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")
print(f"complete records={len(records)}", flush=True)
