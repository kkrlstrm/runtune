# Contributing

Thanks for looking. RunTune's rules for itself are the same ones it applies to agents: say
what the evidence shows, report what was withheld, and never widen authority quietly.

## Set up

```bash
git clone https://github.com/kkrlstrm/runtune && cd runtune
python3 -m pip install -e '.[dev]'
python3 -m pytest tests -q
runtune demo
```

The core is standard library only and must stay importable under `python -S` (CI checks).
Optional features import their dependency inside the function that needs it: `psycopg` for
warehouse sources, `playwright` for the card PNG.

## What a good change looks like

- **A new deriver or gate comes with a fixture that has a known answer** (`tests/fixtures.py`)
  and a test for the case it must *withhold*, not only the case it proposes.
- **A fix for a wrong answer comes with the regression test that would have caught it.** Most
  tests in this repo exist because a real run produced a confident wrong number.
- **Anything that writes outside `.runtune/` goes through `lifecycle/promoter.py`.** No other
  module writes target files. The governance tests (`class Governance`) must keep passing
  unchanged; if one has to change, explain why in the PR.
- **Reports name their limits.** If a number is observational, a projection, or read from
  partial data, the output says so.

## Sharing results

If you run RunTune on your own agents, results are welcome as an issue or a PR to
`docs/community-results/`. Aggregates only: no customer names, no command text. Null
results are as useful as positive ones.
