"""run_workflow entrypoint for the agy-dev-loop outer optimization loop."""

import json
import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from core.workflow_logic import run_outer_loop  # noqa: E402

result = await run_outer_loop(args, agent, phase, log)  # noqa: F704, F821
print(json.dumps(result, indent=2))
