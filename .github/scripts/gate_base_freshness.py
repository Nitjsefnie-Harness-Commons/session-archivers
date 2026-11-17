"""Refuse a head lacking a commit main holds on GATE_PATHS at run time.

--full-history keeps side branches a merge left TREESAME.
"""
import os
import re
import subprocess
import sys

# shortcut: fixed, so a new root tool config is outside it until added;
# upgrade: derive it from the workflows (claim's tests/gate_base_freshness.py).
GATE_PATHS = ('.github/', 'requirements*.txt', 'pyproject.toml', 'setup.cfg',
              '.pylintrc', 'pyrightconfig.json', 'run_tests.py')
MAIN = 'refs/remotes/origin/main'


def git(*args: str) -> bytes:
    done = subprocess.run(('git', *args), capture_output=True, check=False)
    if done.returncode:
        raise RuntimeError(f"`git {' '.join(args)}` exited {done.returncode}: "
                           f"{done.stderr.decode(errors='replace').strip()}")
    return done.stdout


def head_sha() -> str:
    head = os.environ.get('SESSION_ARCHIVERS_HEAD_SHA', '')
    if not re.fullmatch(r'[0-9a-f]{40}([0-9a-f]{24})?', head):
        raise RuntimeError(f'the head sha {head!r} is not a full commit id')
    return head


def main() -> int:
    try:
        head = head_sha()
        if git('rev-parse', '--is-shallow-repository').strip() != b'false':
            raise RuntimeError('the checkout is shallow, which hides what the head carries; check out with fetch-depth: 0')
        git('fetch', '--no-tags', '--quiet', 'origin', f'+refs/heads/main:{MAIN}')
        stale = git('log', '--full-history', '--format=%H %s', f'{head}..{MAIN}', '--', *GATE_PATHS)
    except RuntimeError as error:
        print(f'gate freshness: {error}')
        return 1
    paths = ', '.join(GATE_PATHS)
    if stale:
        print(f'main holds commits this head lacks that touch {paths}:')
        print(''.join(f'  {line}\n' for line in stale.decode(errors='replace').splitlines()), end='')
        print('Rebase onto main and push again: the pull request lands by rebase, '
              "so its own commits must carry main's gate definitions.")
        return 1
    print(f'This head carries every commit on main that touches {paths}. '
          'A commit main gains after this run is not seen until the check runs again.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
