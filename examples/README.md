# Examples

Scripts you can submit as `python_batch` jobs, and the data they expect.

```bash
pixi run client submit-python-batch model-training/train.py \
                                    model-training/training-data.csv \
                                    --name "quick check"
```

## What a job script must do

There is no framework and nothing to import. A job script is an ordinary Python
file that reads a few environment variables:

| Variable | Meaning |
|---|---|
| `HOME_PLATFORM_DATASET` | Absolute path to the input file, read-only |
| `HOME_PLATFORM_INPUT_DIR` | Read-only directory containing that input |
| `HOME_PLATFORM_OUTPUT_DIR` | Write results here — anything left behind is published as an artifact |
| `HOME_PLATFORM_CPU_LIMIT` | The CPU quota this job was given, as a float |
| `HOME_PLATFORM_JOB_ID` | The job's UUID |
| `HOME_PLATFORM_JOB_NAME` | The submitted job name, or the UUID when unnamed |

The container has **no network access**, so everything must come from the
dataset and the pre-installed libraries. Standard output is captured but
truncated to the last 8000 characters; anything worth keeping belongs in a file
in the output directory.

Results are published under a directory named after the job — `quick-check-…`
for the command above — so give a run a name you will recognise later. The
script cannot choose that destination.

The input can also be a file already on the NAS, using the same logical paths
Job Desk shows:

```bash
pixi run client submit-python-batch model-training/train.py \
                                    --dataset-storage Shared/Datasets/training-data.csv \
                                    --name "quick check"
```

## The examples

### `cancellation/long-running.py`

A five-minute no-op used to verify cancellation. It writes a partial marker,
then waits long enough for an operator to cancel it. A successful acceptance
records `FAILED / CANCELLED_BY_USER` and publishes neither the partial marker
nor the file after the wait.

### `model-training/train.py`

Minimal end-to-end proof: logistic regression on a 16-row CSV, writes
`model.joblib` and `metrics.json`. Runs in seconds. Use it to check the pipeline
works before committing to something expensive.

### `model-training/svm-grid-search.py`

A realistic, deliberately expensive workload: an SVM grid search over 32
candidates with 5-fold cross-validation — 160 fits — on 1500 rows. Writes
`model.joblib`, `metrics.json` and `report.txt`.

Its purpose is to demonstrate **throttling**. The same job, unchanged:

| Quota | `n_jobs` | Elapsed | Container CPU |
|---|---|---|---|
| `--cpus 0.5` | 1 | 1062 s | steady 50% |
| `--cpus 4` | 4 | 173 s | steady 403% |

Both produce identical results. Running it slowly costs proportional time and
nothing else, so an overnight search can share a laptop you are still using:

```bash
pixi run client submit-python-batch model-training/svm-grid-search.py \
                                    model-training/svm-data.csv \
                                    --name "overnight search" \
                                    --cpus 0.5 --timeout-seconds 86400
```

## The one thing to copy

Size your parallelism from the quota, not from the machine:

```python
cpu_limit = float(os.environ.get("HOME_PLATFORM_CPU_LIMIT", "1"))
n_jobs = max(1, int(cpu_limit))
```

**Do not use `n_jobs=-1`.** Inside a container it sees the *host's* cores rather
than the quota, so it fans out far wider than the limit allows and spends the
difference on context switching. The worker already pins BLAS thread pools to
the quota for the same reason; this covers the parallelism your own code
chooses.
