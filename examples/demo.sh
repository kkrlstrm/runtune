#!/usr/bin/env bash
# The whole loop on a synthetic trace: scan -> derive -> stage -> apply (and two refusals)
# -> review -> ledger. Writes only to a temp directory it deletes on exit. No network, no DB.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
cd "$TMP"
rt() { PYTHONPATH="$HERE" python3 -m runtune "$@"; }
J=(--jsonl "$HERE/runtune/data/demo.jsonl")

echo "== scan";   rt scan "${J[@]}"
echo "== derive"; rt derive "${J[@]}" --out report.md
ID=$(python3 -c "import json,glob; r=json.load(open(sorted(glob.glob('.runtune/candidates/*.json'))[-1])); print([c['id'] for c in r['candidates'] if c['kind']=='constraint'][0])")
echo "== stage $ID"; rt stage "$ID"
echo "== apply as block (refused: the evidence tier does not allow it)"; rt apply "$ID" --approve demo --action block || true
echo "== apply as deny";  rt apply "$ID" --approve demo --action deny
echo "== retire without a reason (refused)"; rt retire "$ID" --approve demo --reason "" || true
echo "== review"; rt review "${J[@]}"
echo "== ledger"; rt ledger
echo; echo "report excerpt:"; sed -n '1,25p' report.md
