"""Refuse an outgoing commit pairing a workflow-name scope with a non-ci type.

MAIN is the main the freshness step before this one fetched. Names are read
at the head; a column-0 line outside the reader's subset refuses, naming its
file, so no name leaves the rule unseen. Type, scope and name compare
case-insensitively, as Conventional Commits requires. A subject without
`type(scope)` is listed, not failed: the pr gate owns the grammar.
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


def main() -> int:
    try:
        head = head_sha()
        names = workflow_names(head)
        log = git('log', '--no-merges', '-z', '--format=%H %s', f'{MAIN}..{head}')
    except RuntimeError as error:
        print(f'commit scopes: {error}')
        return 1
    bad, skipped, examined = [], [], 0
    for record in filter(None, log.decode(errors='replace').split('\0')):
        subject = SUBJECT.match(record.partition(' ')[2])
        if subject is None:
            skipped.append(record)
            continue
        examined += 1
        kind, scope = subject[1].lower(), subject[2].lower()
        if scope in names and kind != 'ci':
            bad.append(f'  {record}\n    scope `{subject[2]}` names {names[scope]}; type `{subject[1]}` is not `ci`')
    print(f'Examined {examined} subjects in main..head; {len(skipped)} not examined:')
    print(''.join(f'  {record}\n' for record in skipped), end='')
    if bad:
        print("A workflow's name is a scope only with type `ci`:\n" + '\n'.join(bad))
        return 1
    print('No commit pairs a workflow-name scope with a type other than `ci`.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
