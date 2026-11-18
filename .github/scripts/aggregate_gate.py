"""Fold ci-gate.yml's `toJSON(needs)` into the aggregate's verdict.

Green only when classify succeeded and every leg in LEGS succeeded, or was
skipped by a docs-only classification. Exit 1 names each deciding job.
`--self-test` checks every fold branch on inline needs documents;
ci-gate.yml's aggregate job runs it, as no suite under tests/ covers this
script.
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


def self_test() -> None:
    if not __debug__:
        sys.exit('aggregate_gate self-test refuses to run under -O')

    def needs(docs_only: str | None = 'false', **results: str) -> dict[str, Any]:
        document: dict[str, Any] = {job: {'result': results.get(job, 'success'), 'outputs': {}} for job in JOBS}
        if docs_only is not None:
            document['classify']['outputs'] = {'docs_only': docs_only}
        return document

    all_skipped = dict.fromkeys(LEGS, 'skipped')
    cases = [('every job succeeded', needs(), []),
             ('docs-only, every leg skipped', needs('true', **all_skipped), []),
             ('docs_only not exactly "true"', needs('True', tests='skipped'),
              ['tests: skipped without a docs-only classification']),
             ('classify missing', {job: {'result': 'success'} for job in LEGS}, ['classify: missing from needs']),
             ('an extra job', {**needs(), 'diff-coverage': {'result': 'success'}},
              ['diff-coverage: in needs but not a known leg']),
             ('classify failed, legs skipped', needs(None, classify='failure', **all_skipped),
              ['classify: failure', *(f'{leg}: skipped without a docs-only classification' for leg in LEGS)]),
             ('classify failed on docs-only', needs('true', classify='failure', **all_skipped), ['classify: failure'])]
    for leg in LEGS:
        for result in ('failure', 'cancelled'):
            cases.append((f'{leg} {result}', needs(**{leg: result}), [f'{leg}: {result}']))
            cases.append((f'{leg} {result} on docs-only', needs('true', **{leg: result}), [f'{leg}: {result}']))
        cases.append((f'{leg} skipped without docs-only', needs(**{leg: 'skipped'}),
                      [f'{leg}: skipped without a docs-only classification']))
        cases.append((f'{leg} missing', {job: value for job, value in needs().items() if job != leg},
                      [f'{leg}: missing from needs']))
    for label, document, want in cases:
        got = failures(document)
        if got != want:
            raise AssertionError(f'{label}: got {got}, want {want}')


def main() -> int:
    if sys.argv[1:] == ['--self-test']:
        self_test()
        print('aggregate_gate self-test passed')
        return 0
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
