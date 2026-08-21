# session-archivers

R2 archivers for the session transcripts of three agent harnesses: Claude Code,
Kimi and Codex.

Each harness stores sessions differently — Claude by project directory, Kimi by
working directory, Codex in date buckets — so these are three archivers rather
than one parameterised routine. What they share is the destination: one bucket
layout, one manifest format, one single-instance lock, and one compression
policy, so a dashboard reads all three the same way.

## Install

Not on PyPI. Every release publishes the wheel with a `SHA256SUMS` file beside
it, and checking against it is the point: fetching "the newest release" is
otherwise a promise about a URL, not about the artifact CI built.

```sh
gh release download v1.0.0 --repo Nitjsefnie-Harness-Commons/session-archivers
sha256sum -c SHA256SUMS
pip install ./session_archivers-1.0.0-py3-none-any.whl
```

Three console scripts:

```
archive-claude-sessions [--days N] [--dry-run]
archive-kimi-sessions   [--days N] [--dry-run]
archive-codex-sessions  [--days N] [--dry-run]
```

`--dry-run` previews every action and writes nothing, locally or remotely.
`--days` sets the retention threshold for the delete gate; upload is
independent of it, so a file is always uploaded before it can be removed.

## Configuration

No credential is ever written into the source — this repository is public, and
a literal key would travel with the code to every machine that installs it. The
values are read at call time, so importing a module never demands them:

| name | meaning |
|---|---|
| `R2_ACCESS_KEY_ID` | R2 API token access key |
| `R2_SECRET_ACCESS_KEY` | R2 API token secret |
| `R2_ACCOUNT_ID` | account that owns the bucket; falls back to `CLOUDFLARE_ACCOUNT_ID`, because it is the same account |
| `R2_BUCKET_CLAUDE` / `R2_BUCKET_KIMI` / `R2_BUCKET_CODEX` | destination buckets, defaulting to `claude` / `kimi` / `codex` |

Resolution order: the environment, then `~/.agent-bundle/settings.local.json`,
then `~/.agent-bundle/settings.json`, then legacy `~/.claude` settings, then the
built-in default. The bundle's settings file is read when present and is not
required — the wheel stands on its own.

A missing credential raises naming the exact key. A half-configured archiver
that silently uploads nowhere is worse than one that refuses to start.

## Where this came from

Extracted from a private agent-harness bundle, where they ran only on whichever
machine last exercised them by hand. Here CI runs the suite on every push. That
bundle keeps thin wrappers at the documented script paths, so every schedule and
doc naming one of them still works.

## License

MIT.

## Development

```sh
python3 run_tests.py                                             # the suite
git ls-files -co --exclude-standard '*.py' | xargs pylint        # lint
git ls-files -co --exclude-standard '*.py' | xargs pycodestyle   # lint
pyright                                                          # types
pip-audit -r requirements-dev.txt -r requirements-test.txt       # audit
actionlint .github/workflows/*.yml && zizmor .github/workflows/  # workflows
```

`pip install -r requirements-dev.txt -r requirements-test.txt` gets the
pinned toolchain. Use `-co --exclude-standard`, not a bare `git ls-files`:
a brand-new module is untracked until you stage it, and pylint would
otherwise report a clean run over every file except the one you just
wrote.

Coverage:

```sh
for s in tests/test_*.py; do
  python3 -m coverage run --parallel-mode --source=session_archivers "$s"
done
python3 -m coverage combine && python3 -m coverage report
```

Each suite in its own subprocess, because that is what `run_tests.py` does.
Gated at **92%**, a ratchet under the current 93.9%, not a target.

Three suites, and only two of them move that number.
`test_package_contract.py` reads the package as TEXT and parses its ASTs —
it asserts no credential literal ships and that the wheel promises what it
promises — and deliberately never imports the code, so it contributes
nothing to line coverage. `test_archiver_behaviour.py` executes what needs
no client at all: key construction, the deletion predicate, content-type
classification, settings resolution. `test_r2_paths.py` executes everything
downstream of `_client()` — the manifest round-trip, the bucket inventory,
the compression policy, the three archive walks and the `main()` drivers —
against an in-memory stub answering the four S3 calls the archivers actually
make.

The whole suite stays off the network, and no real R2 client is constructed
anywhere in it. What is left uncovered is the Windows `msvcrt` leg of the
single-instance lock, the `boto3.client(...)` call itself, and the handful of
`OSError` branches that need an unwritable filesystem to reach.

### CI

| Workflow | What it does |
| --- | --- |
| `tests` | `run_tests.py` across 3 OSes × 3 Pythons, plus a single-run coverage job — the matrix would otherwise report the same coverage number nine times. |
| `lint` / `types` | pylint + pycodestyle, and pyright. |
| `codeql` | Security analysis (Python only — no JS here). Findings go to the Security tab, never the build. Weekly cron on top of push, because a query published today would otherwise only ever run against files touched after it shipped. |
| `audit` | `pip-audit` over both requirements files, resolving the full transitive tree. **Daily** cron: this answers "is a version we froze months ago still safe", and that answer changes with no commit here to hang it on. |
| `actionlint` | `actionlint` + `zizmor` over the workflow YAML. A broken workflow does not go red, it silently stops running. |
| `tag` | Watches `session_archivers/__init__.py`. When `__version__` changes on `main`, it waits for every other check on that commit and then pushes `v<version>`. |
| `release` | Fires on that tag: runs every suite, builds the wheel, and publishes `SHA256SUMS` beside it. |

**There is deliberately no speed gate here**, unlike the dashboard repos.
pytest cannot run these suites — the tests are plain functions taking
helper arguments, which pytest reads as fixture requests — and the
comparator needs `--junitxml`, which only pytest emits. Rather than
reshape the tests to suit a gate, the gate is omitted.

**Releasing = bumping `__version__`.** `tag` creates the tag, `release`
publishes from it. Nothing bumps the version automatically: deciding
patch-vs-minor is a judgement about what changed.

**Actions are hash-pinned**, with the version in a trailing comment. Do
not "tidy" one back to `@v4`: a tag is a moving pointer, and these jobs
run with a repository token. Dependabot keeps the hashes current.

**`.gitignore` is deny-by-default**: `*` first, then each shipped path
named back. `build/`, `dist/` and `*.egg-info/` need no rules at all now —
they are simply never named back. A new file of an unlisted type is
invisible to git and will NOT appear in `git status`;
`git check-ignore -v <path>` names the rule hiding it. Never "fix" it by
loosening the leading `*`.
