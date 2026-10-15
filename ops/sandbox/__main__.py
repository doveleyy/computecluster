"""Start the test-only sandbox: seed synthetic state, then serve the real core.

The process reads only ``HP_SANDBOX_*`` variables. It refuses any core,
fixture, or development-identity setting so a sandbox can never be pointed at
real data or accept a fictional user.
"""

import asyncio
import json
import os
import sys

from app.app_contracts import ContractError
from ops import preview_core
from ops.sandbox.seed import SandboxRefusal, plan_from_environment, seed


def main() -> int:
    try:
        plan = plan_from_environment(os.environ)
        summary = seed(plan)
    except (SandboxRefusal, ContractError, OSError) as error:
        print(f"sandbox refused to start: {error}", file=sys.stderr)
        return 2
    print(json.dumps({"sandbox": "test-only", **summary}, sort_keys=True), flush=True)
    os.environ.update(plan.core_environment())
    asyncio.run(preview_core.main())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
