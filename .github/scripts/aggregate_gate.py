"""Fold ci-gate.yml's `toJSON(needs)` into the aggregate's verdict.

Green only when classify succeeded and every leg in LEGS succeeded, or was
skipped by a docs-only classification. Exit 1 names each deciding job.
"""
import json
import os
import sys
from typing import Any

LEGS = ('tests', 'lint', 'types', 'audit', 'codeql', 'actionlint')
JOBS = ('classify', *LEGS)


def failures(needs: dict[str, Any]) -> list[str]:
    found = [f'{job}: missing from needs' for job in JOBS if job not in needs]
    found += [f'{job}: in needs but not a known leg'
              for job in sorted(needs.keys() - set(JOBS))]
    classify = needs.get('classify', {})
    if 'classify' in needs and classify.get('result') != 'success':
        found.append(f"classify: {classify.get('result')}")
    docs_only = classify.get('outputs', {}).get('docs_only') == 'true'
    for leg in LEGS:
        result = needs.get(leg, {}).get('result')
        if leg not in needs or result == 'success':
            continue
        if result == 'skipped' and docs_only:
            continue
        found.append(f'{leg}: {result}' + (' without a docs-only classification'
                                           if result == 'skipped' else ''))
    return found


def main() -> int:
    needs = json.loads(os.environ['SESSION_ARCHIVERS_NEEDS_JSON'])
    found = failures(needs)
    lines = [f'- `{job}`: {needs[job].get("result")}' for job in needs]
    lines.append('')
    lines += [f'- **red** {line}' for line in found] or ['**green**']
    print('\n'.join(lines))
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a', encoding='utf-8') as handle:
            handle.write('### ci gate\n\n' + '\n'.join(lines) + '\n')
    return 1 if found else 0


if __name__ == '__main__':
    sys.exit(main())
