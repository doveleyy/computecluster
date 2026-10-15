"""Small batch used to verify automatic scheduler placement."""

import json
import os
from pathlib import Path

output = Path(os.environ["HOME_PLATFORM_OUTPUT_DIR"])
(output / "placement.json").write_text(
    json.dumps(
        {
            "job_id": os.environ["HOME_PLATFORM_JOB_ID"],
            "cpu_limit": os.environ["HOME_PLATFORM_CPU_LIMIT"],
        }
    )
)
print("placement smoke test completed")
