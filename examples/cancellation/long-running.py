"""Long-running batch used to verify operator cancellation."""

import os
import time
from pathlib import Path

import pandas as pd

dataset = pd.read_csv(os.environ["HOME_PLATFORM_DATASET"])
output_directory = Path(os.environ["HOME_PLATFORM_OUTPUT_DIR"])

(output_directory / "partial.txt").write_text(
    f"started with {len(dataset)} rows\n",
    encoding="utf-8",
)
print("waiting for cancellation", flush=True)
time.sleep(300)
(output_directory / "unexpected-completion.txt").write_text(
    "the cancellation test did not stop this container\n",
    encoding="utf-8",
)
