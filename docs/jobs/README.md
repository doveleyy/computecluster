# Job Types

Home Platform has two authoring contracts. Choose the smallest contract that
fits the work, then follow that contract's page exactly.

| Job type | Use it when | Availability | Authoring guide |
|---|---|---|---|
| `python_batch` | One Python file consumes one input file and writes result files | Implemented and live-verified | [Python script job](python-script.md) |
| `batch` | A project needs Bash, named file inputs, and a numeric array of independently scheduled runs | Implemented; core execution live-verified | [Batch script job](batch-script.md) |

`sleep` is an operational test job, not a contributor-facing workload format.
The retired CSV Summary job is intentionally not part of this catalogue.

These are two interfaces to one evolving scheduler, not two independent job
systems. The lightweight Python form will eventually be translated into the
general batch contract internally.

## How to choose

Use a Python script job when all of these are true:

- the workload is one `.py` file;
- it has exactly one input file;
- the fixed scientific Python image contains every dependency; and
- flat result files are sufficient.

Use the current batch script job when:

- the workload contains several source or configuration files;
- its entrypoint is Bash or it runs several commands;
- it consumes one or more project, NAS, uploaded, or verified-URL files;
- independent parameter sets should run as an array; or
- its numeric index-to-work mapping belongs in project shell code.

Verified HTTPS inputs can be downloaded directly by a worker. Provisioned
members can select regular files through logical `Home/...` and `Shared/...`
paths, and Job Desk resolves safe defaults declared in `submit.hp`. Directory
references, alternate runtimes, dependencies, nested artifacts, and single
non-array batch submission remain planned.

Both types choose inputs the same way — the same logical roots through Job Desk
and the CLI — and publish results the same way, into a per-submission directory
named after the job. Neither lets a script pick its output destination. So the
choice above is about the shape of the workload, not about how files get in or
out.

The parent/child queue and expandable Job Desk group are implemented and
live-verified, including automatic numeric `#HP --array` expansion. See the
[batch script guide](batch-script.md#how-arrays-appear-in-job-desk) for the
group state and cancellation contract.

For the practical upload/select/submit sequence, see
[Files in and out of a job](storage-workflow.md).
