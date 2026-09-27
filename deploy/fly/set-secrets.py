#!/usr/bin/env python3
"""Stage runtune's secrets on fly, one named secret each (never a whole .env).

    python3 deploy/fly/set-secrets.py --from-env            # print what WOULD be set (names only)
    python3 deploy/fly/set-secrets.py --from-env --apply    # fly secrets set --stage

Values come from this process's environment. Prints key names, never values.
"""
import os
import subprocess
import sys

KEYS = ["RUNTUNE_DB_URL", "RUNTUNE_SLACK_TOKEN", "RUNTUNE_SLACK_USER", "RUNTUNE_APPROVER",
        "RUNTUNE_GIT_REPO", "RUNTUNE_GIT_TOKEN", "RUNTUNE_ROUTES", "RUNTUNE_AGENTS_DIR",
        "RUNTUNE_CHANNEL", "RUNTUNE_EMAIL_TO", "RUNTUNE_GMAIL_TOKEN_JSON", "OPENROUTER_PROVISIONING_KEY"]

present = {k: os.environ[k] for k in KEYS if os.environ.get(k)}
print("would set:", ", ".join(sorted(present)) or "(nothing)")
print("missing:  ", ", ".join(k for k in KEYS if k not in present))
if "--apply" in sys.argv and present:
    args = ["fly", "secrets", "set", "--stage", "--app", os.environ.get("FLY_APP", "runtune")]
    subprocess.run(args + [f"{k}={v}" for k, v in present.items()], check=True)
    print("staged; they take effect on the next deploy")
