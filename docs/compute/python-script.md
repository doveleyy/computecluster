# Python Script Job

**Job type:** `python_batch`

Use this job type to run one Python file against one input file. The operator
may submit both through Job Desk or the CLI and choose an eligible compute
worker.

Both interfaces create the same `python_batch` job and accept the same three
input sources:

| Source | Job Desk | CLI |
|---|---|---|
| A file already on the NAS | **Choose from Home or Shared** | `--dataset-storage PATH` |
| A small local CSV | **Upload a small CSV** | positional `dataset` argument |
| A large externally hosted CSV | **Use a verified download link** | `--dataset-url` with `--dataset-sha256` and `--dataset-size-bytes` |

NAS files are named by the same logical paths the batch job type uses —
`Home/...` for your private tree and `Shared/...` for deliberately shared data.
See [Files in and out of a job](../storage/workflow.md). A verified HTTPS file
travels directly to the selected worker rather than through the coordinator;
its SHA-256 and byte size are integrity requirements, not hints.

Only the input transport differs. The container, script contract, limits,
scheduling, and artifacts are identical across all three.

## The script contract

The script is an ordinary Python program. It does not import a Home Platform
SDK and it does not return a Python object to the platform. It communicates in
three ways:

1. exit code `0` means the computation succeeded;
2. standard output and standard error provide short diagnostic logs; and
3. regular files written to the output directory become downloadable artifacts.

Six environment variables are available. They are the same names the batch job
type uses, so a script can move between the two without rewriting its I/O:

| Variable | Contract |
|---|---|
| `HOME_PLATFORM_DATASET` | Absolute path to the read-only input file |
| `HOME_PLATFORM_INPUT_DIR` | Read-only directory containing that input |
| `HOME_PLATFORM_OUTPUT_DIR` | Writable directory for result artifacts |
| `HOME_PLATFORM_JOB_ID` | Server-generated UUID for this run |
| `HOME_PLATFORM_JOB_NAME` | The submitted job name, or the UUID when unnamed |
| `HOME_PLATFORM_CPU_LIMIT` | CPU quota assigned to the container, as a float |

Read the input through `HOME_PLATFORM_DATASET`. `HOME_PLATFORM_INPUT_DIR` is
the directory that contains it and exists for parity with the batch contract;
do not assume any other file is present in it.

Minimal template:

```python
import json
import os
from pathlib import Path

import pandas as pd

dataset_path = Path(os.environ["HOME_PLATFORM_DATASET"])
output_directory = Path(os.environ["HOME_PLATFORM_OUTPUT_DIR"])

frame = pd.read_csv(dataset_path)
summary = {
    "rows": len(frame),
    "columns": list(frame.columns),
}

(output_directory / "summary.json").write_text(
    json.dumps(summary, indent=2),
    encoding="utf-8",
)
print(f"processed {len(frame)} rows")
```

Only files placed directly inside `HOME_PLATFORM_OUTPUT_DIR` are published.
A script cannot choose where results are stored; the platform decides that, so
one run can never overwrite another's output. Use portable names beginning with
a letter or number and containing only letters, numbers, dots, underscores, or
hyphens, for example:

```text
metrics.json
model.joblib
predictions.csv
confusion-matrix.png
report.txt
```

Do not treat standard output as durable storage. The platform keeps only the
last 8,000 characters of each output stream. Write anything important to an
artifact instead.

## Available runtime

The fixed batch image includes Python, NumPy, pandas, SciPy, PyArrow, Joblib,
scikit-learn, statsmodels, XGBoost, Matplotlib, Seaborn, CPU-only PyTorch, and
their pinned dependencies. The image has no network access, so a script cannot
download packages, models, or data while it runs.

Give the operator every required input up front. If another dependency is
needed, it must be reviewed and added to a future image rather than installed
dynamically by the script.

## Resource-aware parallelism

CPU quota is a hard limit. Size application-level parallelism from the assigned
quota rather than asking a library to use the entire host:

```python
cpu_limit = float(os.environ.get("HOME_PLATFORM_CPU_LIMIT", "1"))
n_jobs = max(1, int(cpu_limit))
```

For scikit-learn and Joblib, use `n_jobs=n_jobs`, not `n_jobs=-1`. Native
numerical thread pools are already pinned by the worker.

Memory and wall time are hard boundaries. Crossing the declared memory limit
produces `FAILED / MEMORY_LIMIT_EXCEEDED`; crossing the time limit produces
`FAILED / TIMED_OUT`. A run may request up to seven days (`604800` seconds).

## Outputs and limits

Every run publishes into its own directory, named after the job so results are
recognisable when browsing storage rather than only through the API:

```text
artifacts/<owner-id>/
├── SVM_model-7dcf9099/
│   ├── metrics.json
│   └── model.joblib
└── SVM_model-1a4be012/        a second run of the same script
```

Job names are not unique, so the directory carries the first eight characters of
the job's UUID as well. Two runs sharing a name therefore stay separate —
publication never overwrites an earlier result. Access is derived from job
ownership rather than from the layout, so the `<owner-id>/` level organises
storage without deciding who may read a run — see
[files in and out of a job](../storage/workflow.md). Runs published before this
layout existed are still served from their original directory. The layout is
identical for the [batch job type](batch-script.md), which nests array children
one level deeper.

Current defaults allow up to 100 MiB per artifact and 512 MiB across one job.
The job result records at most 100 output filenames. Publication is intended
for models, metrics, reports, plots, and modest result tables—not multi-gigabyte
checkpoints or generated datasets.

An uncaught exception or non-zero exit produces `FAILED / EXECUTION_ERROR`.
Artifacts from an unsuccessful or cancelled run are not published. Keep
temporary files outside the output directory when possible.

## Cancellation and progress

A queued job can be cancelled immediately. A running container is cancelled
cooperatively: the control plane records the request, the worker receives it on
its next lease heartbeat, and the worker force-removes only that job's
container. The terminal record remains `FAILED` with
`failure_kind: CANCELLED_BY_USER`.

Application progress reporting is not part of the current contract. Printing
epochs or percentages does not make them visible live; stdout and artifacts are
available only after successful completion.

## Author checklist

- The script runs from top to bottom without interactive input.
- It reads the input path from `HOME_PLATFORM_DATASET`.
- It writes durable results to `HOME_PLATFORM_OUTPUT_DIR` and nowhere else.
- It needs no network access, secrets, GPU, or unlisted package.
- It derives parallelism from `HOME_PLATFORM_CPU_LIMIT`.
- It has been tested locally against a small representative CSV.
- Output files fit the documented artifact limits.
- The job has a name worth reading later; it becomes the results directory.
- The operator knows the expected CPU, memory, and maximum runtime.

See the runnable examples under [`examples/`](../../examples/) and the complete
[job contract](job-contract.md).
