#!/usr/bin/env bash
set -euo pipefail

index=$1
python - \
  "$index" \
  "$HOME_PLATFORM_INPUT_DIR/payload.txt" \
  "$HOME_PLATFORM_OUTPUT_DIR/result.txt" <<'PY'
from pathlib import Path
import sys
import time

index = int(sys.argv[1])
payload = Path(sys.argv[2]).read_text().strip()
time.sleep(2)
Path(sys.argv[3]).write_text(f"completed array index {index}: {payload}\n")
PY
