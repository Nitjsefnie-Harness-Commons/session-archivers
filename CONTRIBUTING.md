# Contributing to session-archivers

Issues and pull requests are welcome, especially ones that show an archiver
doing something other than what [README.md](README.md) says: a session file
removed that was never uploaded, a transcript filed under the wrong bucket, a
credential showing up where it should not. This package runs unattended from
cron or Task Scheduler with object-storage credentials, over transcripts that
hold whatever an agent saw, so a report that names the file, the run and the
output is the most valuable thing you can send.

## Getting it running

You need Python 3.11 or newer (`requires-python` in `pyproject.toml`). The
package is standard-library only at run time except for `boto3`, and the suites
need no network, no credential and no real transcript: a test that touches a
bucket does so through the in-memory `FakeS3` stub in `tests/_util.py`.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install .
python run_tests.py     # every tests/test_*.py in its own subprocess; the exit status is the verdict
```

CI runs the suites on Linux, Windows and macOS across Python 3.11 to 3.15
(`.github/workflows/tests.yml`). The lint, types, coverage, wheel, audit and
actionlint jobs run on Python 3.13.

## The gates, the way CI runs them

Each line below is a step of the workflow named beside it. Run them from the
repository root; the hashed manifests install in a `pip` command of their own,
because `--require-hashes` makes `pip` refuse a directory requirement such as
`.`.

```bash
# lint, types
pip install --require-hashes -r requirements-dev.txt
pip install .
git ls-files '*.py' | xargs pycodestyle
git ls-files '*.py' | xargs pylint --rcfile=.pylintrc
pyright

# tests, coverage job: each suite in its own process, then the floor
pip install --require-hashes -r requirements-test.txt
for s in tests/test_*.py; do
  python -m coverage run --parallel-mode --source=session_archivers "$s"
done
python -m coverage combine
python -m coverage report --fail-under=94 --precision=1

# tests, wheel job
pip install --require-hashes -r requirements-build.txt
python -m build --wheel

# audit: the package, then every root requirements manifest
pip install -r requirements-pip-audit.txt
pip-audit .
ls requirements*.txt | xargs -n1 pip-audit -r

# actionlint (zizmor half); CI sets GH_TOKEN so zizmor's online audits run,
# and without it zizmor runs offline with fewer audits
pip install --require-hashes -r requirements-zizmor.txt
GH_TOKEN="$(gh auth token)" zizmor --no-progress .github/workflows/
```

- `git ls-files` sees only tracked files, so stage a new module before running
  pylint and pycodestyle: an untracked one is checked by nobody. (`git ls-files
  -co --exclude-standard` includes untracked files that are not ignored.)
- The `actionlint` workflow runs [Nitjsefnie-OSC/actionlint
  v1.7.12-queue.1](https://github.com/Nitjsefnie-OSC/actionlint/releases/tag/v1.7.12-queue.1),
  checksum-verified, not stock actionlint 1.7.12: `claim.yml` sets
  `queue: max`, which the stock build rejects. Run `actionlint
  .github/workflows/*.yml` with that build.
- The `secrets` workflow runs gitleaks 8.30.1 over the full history
  (`gitleaks detect --verbose --redact`). No fixture or example may carry a
  credential-shaped value; use the reserved documentation values (`example.com`,
  `192.0.2.0/24`) for hosts and addresses.
- The coverage floor is a ratchet, not a target: raise it by hand in
  `tests.yml` as coverage climbs, and write tests rather than lower it to fit a
  result.
- Four more workflows check a pull request and need GitHub to run, so they
  have no local command: `pr gate` (the description against
  `.github/PULL_REQUEST_TEMPLATE.md`, Conventional Commits subjects, issue
  assignment, commit attribution), `pin test screen`, `comment share` (a
  changed file with no record may not exceed a 15% comment share; a changed
  recorded file fails when it exceeds, on both comment share and comment bytes,
  its record in `.github/comment-share.json` or the effective limit the
  workflow derives from it, which can sit below the record) and `size baseline`
  (per-file line ceilings and the `tests/` total, recorded in
  `.github/size-baseline.json`). The CI table in `README.md` describes each.
- `codeql` analyses the Python and the workflow YAML, and findings go to the
  Security tab, not the build.

## A new file is invisible until you name it

`.gitignore` denies by default: it starts with `*` and names back exactly what
the repository ships. A file you create is **untracked and unstaged and will
not appear in `git status`** until `.gitignore` names it. Git never looks
inside a directory it has already ignored, so re-open the directory with
`!/dir/` before naming its files:

```
*
!/tests/
!/tests/test_new.py
```

Prove a change to it rather than reading it. `git ls-files <path>` printing the
path is the proof a new file is named: `git check-ignore` answers whether a
path is excluded, not whether it is named. Seed a junk file in the
directory you changed and check `git check-ignore -v` names the `*` rule for it
and that `git status --porcelain` lists only what you meant to add.

## Commit messages

Conventional Commits: `type(scope): subject`, the scope and a `!` optional; the
`pr gate` checks commit subjects against it. Beyond the subject grammar nothing
is checked, and the house convention is an imperative subject with no trailing
full stop and a body that says why the change was needed.

| Type | For |
|---|---|
| `feat` | new behaviour a user can observe |
| `fix` | a defect in behaviour a user can observe |
| `perf` | faster, with the measurement in the body |
| `refactor` | no observable change in behaviour |
| `docs` | documentation only |
| `test` | the suites and their stub only |
| `build` | how the wheel is built (`pyproject.toml`) |
| `ci` | workflows, Dependabot, repository automation and the CI tool manifests (`requirements*.txt`) |
| `style` | formatting that changes no behaviour |
| `chore` | housekeeping that fits nothing above |
| `revert` | undoing an earlier commit, which it names |

A scope names what the change is about, not the directory it landed in:
`ci(codeql): analyse the actions language as well`, `docs(readme): describe the
audit as it now runs`, `fix(codex): keep a rollout whose writer lock is held`.
A manifest bump written by hand uses `ci(deps)`, as this repository's own
manifest commits do; Dependabot's pull requests carry the prefix
`.github/dependabot.yml` configures (`deps` for the pip manifests, `ci` for the
workflow actions).

`__version__` in `session_archivers/__init__.py` is the release. Between
releases `main` carries a development version, `X.Y.Z.devN`, which the `tag`
workflow never tags. The commit that sets it to a release version `X.Y.Z` is
the release: `tag` waits for the required gates on that commit (named in
`tag.yml` and in the CI table of [README.md](README.md)), refuses to tag if any
check on it has failed, pushes `vX.Y.Z` and starts the `release` workflow, which
builds the wheel from that tag and publishes it with its `SHA256SUMS` and a
build-provenance attestation. `tag` then commits `X.Y.(Z+1).dev0` to `main`.
A later push that cancels that commit's gates before they finish is tagged in
its place, as it still carries the version. Say in the pull request when a
change warrants a bump; the maintainer decides which part.

## Issues

Use the [issue form](.github/ISSUE_TEMPLATE/issue.md). Its section order is
fixed and its **Description is observed behaviour only**: what is actually
wrong, not the mechanism and not the fix. A mechanism you have proved still goes
under Suggested Fix, marked unverified. A report whose description is a
hypothesis sends the reader to the wrong place when the hypothesis is wrong.

Not reliably reproducible? Drop the Reproduction Steps section and say so in the
description, rather than writing steps that do not trigger it. Do not attach a
real transcript or a credential to an issue: describe the shape of the file, or
build a synthetic one.

Something exploitable goes to a [security
advisory](https://github.com/Nitjsefnie-Harness-Commons/session-archivers/security/advisories/new),
not the tracker; [SECURITY.md](SECURITY.md) says what is in scope.

### Claim it before you start

Comment `/claim` on an open issue nobody holds and this repository's own
[`claim` workflow](.github/workflows/claim.yml) assigns you; you need no write
access. It runs [Nitjsefnie-Actions/claim](https://github.com/Nitjsefnie-Actions/claim)
against this repository, so a claim that does not work is itself a bug report.

- The comment body must be the command alone, optionally followed by the
  issue's number with or without `#`. A sentence that merely mentions
  `/claim` gets no reply. A line that starts with a command word but is not
  exactly one command, a wrong carried number, a closed issue and a pull
  request are each declined with a comment on the issue and a failed run.
- Re-read the issue afterwards and confirm your login is in `assignees`: a
  posted comment is not a claim.
- **Claim limits.** `claim.yml` sets `max-claims` to `read=2, triage=4, write=6,
  maintain=10, admin=-1`: the number of open issues one account may hold, by its
  role on this repository, with `-1` meaning unlimited. An account with no
  access to the repository counts as `read`, so it may hold two.
- **Claim expiry.** `claim.yml` sets `expire` to `7`: a claim the workflow made
  expires once it has been held longer than seven days, counted from the
  assignment, not from your last activity. After that, anyone's `/claim` takes
  the issue over, and an account with write access or above may `/release` it.
  An assignment a person made by hand does not expire.
- Release an issue you stop working with `/unclaim` or `/release`, before the
  merge that closes it. The workflow acts on open issues only, so a stale
  assignment on a closed one can no longer be removed.

## One maintainer, on purpose

One person maintains this project,
[@Nitjsefnie](https://github.com/Nitjsefnie), and that is the shape it is meant
to have rather than a stage it is passing through. It is written down here so a
contributor does not have to infer it from how long a pull request sits. There
is no second maintainer to fall back on.

As of 2026-11-17 the organisation had one member, the repository one
collaborator holding admin and push, no teams and no CODEOWNERS file, and the
only ruleset bypass actor was a deploy key, not a person. That is a reading of
the repository on that date, not a promise that it holds after it, so re-check
it before treating it as current:

```bash
gh api orgs/Nitjsefnie-Harness-Commons/members --jq '.[].login'
gh api repos/Nitjsefnie-Harness-Commons/session-archivers/collaborators \
  --jq '.[] | "\(.login) admin=\(.permissions.admin) push=\(.permissions.push)"'
gh api orgs/Nitjsefnie-Harness-Commons/teams --jq 'length'
git ls-files | grep -i codeowners
gh api repos/Nitjsefnie-Harness-Commons/session-archivers/rulesets --jq '.[].id' |
  while read -r id; do
    gh api "repos/Nitjsefnie-Harness-Commons/session-archivers/rulesets/$id" \
      --jq '.bypass_actors'
  done
```

[SECURITY.md](SECURITY.md) says what that means for someone reporting a
vulnerability: the response times it promises are one person's.

## Pull requests

Small and single-purpose beats large and comprehensive. One logical change per
commit, in the form [Commit messages](#commit-messages) sets out.

Claim the issue before you start, write the description in the form of
[`.github/PULL_REQUEST_TEMPLATE.md`](.github/PULL_REQUEST_TEMPLATE.md), and put
a closing keyword (`Fixes #N`) only under its **Related Issues and Pull
Requests** section. Under Testing, paste what you ran and what it printed.

Pull requests merge by rebase: as of 2026-11-17 the repository's settings allow
no other method. Each commit lands on `main` one by one, under a new hash, so
squash fixups away before you ask for review and let every remaining commit
stand on its own. Re-check the setting with:

```bash
gh api repos/Nitjsefnie-Harness-Commons/session-archivers \
  --jq '[.allow_merge_commit, .allow_squash_merge, .allow_rebase_merge]'
```

If you find a second defect while fixing the first, **file it** rather than
folding it in. A commit that fixes two things is a commit that cannot be
reverted for one of them.
