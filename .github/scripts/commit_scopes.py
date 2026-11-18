"""Refuse an outgoing commit pairing a workflow-name scope with a non-ci type.

MAIN is the main the freshness step before this one fetched. Names are read
at the head; a column-0 line outside the reader's subset refuses, naming its
file, so no name leaves the rule unseen. Type, scope and name compare
case-insensitively, as Conventional Commits requires. A subject without
`type(scope)` is listed, not failed: the pr gate owns the grammar.
`--self-test` checks the reader and the rule on inline fixtures; ci-gate.yml's
aggregate job runs it, as no suite under tests/ covers this script.
"""
import re
import sys

from gate_base_freshness import MAIN, git, head_sha

KEY = re.compile(r'''(?:([A-Za-z_][\w-]*)|"([\w-]*)"|'([\w-]*)')[ \t]*:(?:[ \t]|$)''')
NAME = re.compile(r'name: ([A-Za-z0-9](?:[A-Za-z0-9 ._-]*[A-Za-z0-9._-])?)')
SUBJECT = re.compile(r'([A-Za-z]+)\(([^()]+)\)!?: ')


def workflow_name(path: str, text: str) -> str:
    names = []
    for number, line in enumerate(re.split(r'\r\n?|\n', text), 1):
        if line in ('', '---') or line[0] in ' #':
            continue
        key = KEY.match(line)
        if key is None:
            raise RuntimeError(f'{path}:{number} is not a blank line, a comment, `---` or a '
                               'plain or simply quoted top-level `key:`, which is all this reader reads')
        if 'name' in key.groups():
            name = NAME.fullmatch(line)
            if name is None:
                raise RuntimeError(f'{path}:{number} is not `name: <value>` with one space and a value '
                                   'of letters, digits, spaces, `.`, `_` and `-` that starts with a '
                                   'letter or digit and does not end with a space')
            names.append(name[1])
    if len(names) != 1:
        raise RuntimeError(f'{path} has {len(names)} top-level `name:` lines, not one')
    return names[0]


def workflow_names(head: str) -> dict[str, str]:
    found = {}
    for entry in git('ls-tree', '-z', head, '.github/workflows/').split(b'\0'):
        meta, _, raw = entry.partition(b'\t')
        path = raw.decode(errors='backslashreplace')
        if meta.split()[1:2] == [b'blob'] and raw.endswith((b'.yml', b'.yaml')):
            found[workflow_name(path, git('cat-file', 'blob', meta.split()[2].decode()).decode(errors='replace'))] = path
    if not found:
        raise RuntimeError(f'{head} holds no workflow under .github/workflows/')
    return {name.lower(): path for name, path in found.items()}


def judge(log: str, names: dict[str, str]) -> tuple[bool, str]:
    """Whether the NUL-separated `%H %s` records pass, and the report to print."""
    bad, skipped, examined = [], [], 0
    for record in filter(None, log.split('\0')):
        subject = SUBJECT.match(record.partition(' ')[2])
        if subject is None:
            skipped.append(record)
            continue
        examined += 1
        kind, scope = subject[1].lower(), subject[2].lower()
        if scope in names and kind != 'ci':
            bad.append(f'  {record}\n    scope `{subject[2]}` names {names[scope]}; type `{subject[1]}` is not `ci`')
    report = f"Examined {examined} subject{'' if examined == 1 else 's'} in main..head; {len(skipped)} not examined:\n"
    report += ''.join(f'  {record}\n' for record in skipped)
    if bad:
        return False, report + "A workflow's name is a scope only with type `ci`:\n" + '\n'.join(bad) + '\n'
    return True, report + 'No commit pairs a workflow-name scope with a type other than `ci`.\n'


def self_test() -> None:
    if not __debug__:
        sys.exit('commit_scopes self-test refuses to run under -O')
    for text, want in (('name: lint\non:\n  push:\n', 'lint'),
                       ('---\n# c\n\n"on": push\nname: ci gate\r\njobs: {}\r\n', 'ci gate'),
                       ("'on': [push]\nname: a.b_c-1\n", 'a.b_c-1'), ('Name: x\nname: y\n', 'y')):
        try:
            got = workflow_name('w.yml', text)
        except RuntimeError as error:
            raise AssertionError(f'{text!r}: refused: {error}') from None
        if got != want:
            raise AssertionError(f'{text!r}: read {got!r}, want {want!r}')
    for text, refusal in (('on: push\n', 'has 0 top-level'), ('name: a\nname: b\n', 'has 2 top-level'),
                          ('"name": evil\nname: lint\n', 'is not `name: <value>`'),
                          ('name: "lint"\n', 'is not `name: <value>`'), ('name:  lint\n', 'is not `name: <value>`'),
                          ('name: lint \n', 'is not `name: <value>`'), ('name: lint # c\n', 'is not `name: <value>`'),
                          ('name:lint\n', 'is not a blank line'), ('- name: lint\n', 'is not a blank line'),
                          ('name: lint\n...\n', 'is not a blank line')):
        try:
            workflow_name('w.yml', text)
        except RuntimeError as error:
            if refusal not in str(error):
                raise AssertionError(f'{text!r}: refused with {error}, want {refusal!r}') from None
            continue
        raise AssertionError(f'accepted {text!r}')
    names = {'lint': '.github/workflows/lint.yml', 'ci gate': '.github/workflows/ci-gate.yml'}
    log = '\0'.join(('a1 ci(lint): x', 'a2 CI(Lint): x', 'a3 fix(Lint): x', 'a4 docs(ci gate)!: x',
                     'a5 feat(store): x', 'a6 tidy up', "a7 Merge branch 'b'", 'a8 chore: x', ''))
    want = ('Examined 5 subjects in main..head; 3 not examined:\n'
            "  a6 tidy up\n  a7 Merge branch 'b'\n  a8 chore: x\n"
            "A workflow's name is a scope only with type `ci`:\n"
            '  a3 fix(Lint): x\n    scope `Lint` names .github/workflows/lint.yml; type `fix` is not `ci`\n'
            '  a4 docs(ci gate)!: x\n    scope `ci gate` names .github/workflows/ci-gate.yml; type `docs` is not `ci`\n')
    for got, expected in ((judge(log, names), (False, want)),
                          (judge('a1 ci(lint): x', names), (True, 'Examined 1 subject in main..head; 0 not examined:\n'
                                                            'No commit pairs a workflow-name scope with a type other than `ci`.\n')),
                          (judge('', names)[1].partition('\n')[0], 'Examined 0 subjects in main..head; 0 not examined:')):
        if got != expected:
            raise AssertionError(f'got {got!r}, want {expected!r}')


def main() -> int:
    if sys.argv[1:] == ['--self-test']:
        self_test()
        print('commit_scopes self-test passed')
        return 0
    try:
        head = head_sha()
        names = workflow_names(head)
        log = git('log', '--no-merges', '-z', '--format=%H %s', f'{MAIN}..{head}')
    except RuntimeError as error:
        print(f'commit scopes: {error}')
        return 1
    passed, report = judge(log.decode(errors='replace'), names)
    print(report, end='')
    return 0 if passed else 1


if __name__ == '__main__':
    sys.exit(main())
