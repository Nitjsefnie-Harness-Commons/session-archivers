# session-archivers

R2 archivers for the session transcripts of three agent harnesses: Claude Code,
Kimi and Codex.

Each harness stores sessions differently — Claude by project directory, Kimi by
working directory, Codex in date buckets — so the three walks are three modules
rather than one parameterised routine. Each also has its own bucket:
`R2_BUCKET_CLAUDE`, `R2_BUCKET_KIMI` and `R2_BUCKET_CODEX` are separate
destinations and nothing merges them. The Claude archiver splits its tree one
step further: a Claude Code session can be powered by GLM (served through a
z.ai endpoint), by a GGUF served through a local llama.cpp server, or by a
model served through OpenRouter's Anthropic-compatible endpoint just as well
as by Anthropic's models, and each session files under the bucket of the
provider that actually ran it — `R2_BUCKET_CLAUDE`, `R2_BUCKET_ZAI`,
`R2_BUCKET_LLAMA` or `R2_BUCKET_OPENROUTER`.

What the three share is the *way* each one is written to — one key layout, one
manifest format, one single-instance lock, one compression policy — so a
dashboard reads all three the same way. Since 1.1.0 that half is two modules
instead of three copies of itself:

| Module | What it owns |
| --- | --- |
| `store.py` | The destination. Compression policy, per-machine manifest, bucket inventory, the upload forms. Takes the bucket name as an argument. |
| `runtime.py` | The host. Logging, the single-instance lock, the retention predicate, the local scratch sweeps. |
| `claude.py` / `kimi.py` / `codex.py` | One tree walk each, and the keys it files things under. |

The copies were the right call while these were standalone scripts, copied onto
a machine one file at a time with nothing else present. They install as one
package now, so three copies bought nothing but three places for a fix to land
in two of.

## Install

Not on PyPI. Every release publishes the wheel with a `SHA256SUMS` file beside
it, and checking against it is the point: fetching "the newest release" is
otherwise a promise about a URL, not about the artifact CI built.

```sh
gh release download v1.1.1 --repo Nitjsefnie-Harness-Commons/session-archivers
sha256sum -c SHA256SUMS
pip install ./session_archivers-1.1.1-py3-none-any.whl
```

Three console scripts:

```
archive-claude-sessions [--days N] [--dry-run]
archive-kimi-sessions   [--days N] [--dry-run]
archive-codex-sessions  [--days N] [--dry-run]
```

`--dry-run` previews every action and writes nothing, locally or remotely.
`--days` sets the retention threshold for the delete gate; upload is
independent of it. A stale session or uploaded file is removed only once
the bucket verifiably holds every file in it, in every bucket the session
was filed under; one that does not stays, and the run logs why:
`kept, not verified in the bucket`, a failed upload, or a bucket it could
not list.

The Codex archiver keeps a rollout whose thread-writer lock a live Codex
process still holds, however old it is. It uploads every rollout in
`~/.codex/archived_sessions/` too, under the key it would have in `sessions/`,
and never deletes one there, whatever its age: Codex unarchives a thread by
moving its rollout back into `sessions/`. After the rollout pass it also prunes
`~/.codex/thread_history_1.sqlite`, Codex's rebuildable projection of the
rollouts. Codex cannot read a thread's rows there once the rollout is gone,
and nothing else deletes them. The prune keeps every thread that has a rollout
under `sessions/` or `archived_sessions/`, plus every thread whose writer lock
is held (lock probing is POSIX-only; on Windows the rollout alone decides). It
refuses to run when `sessions/` is missing or cannot be listed, and skips the
run when a Codex writer holds the database for more than 30 seconds. Neither
case changes the exit code.

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
| `R2_BUCKET_ZAI` | destination bucket for GLM transcripts the Claude archiver finds, defaulting to `zai` |
| `R2_BUCKET_LLAMA` | destination bucket for transcripts served by a local llama.cpp server, defaulting to `llama` |
| `R2_BUCKET_OPENROUTER` | destination bucket for transcripts served through OpenRouter's Anthropic-compatible endpoint, defaulting to `openrouter` |

Resolution order: the environment, then `~/.agent-bundle/settings.local.json`,
then `~/.agent-bundle/settings.json`, then legacy `~/.claude` settings, then the
built-in default. The bundle's settings file is read when present and is not
required — the wheel stands on its own.

A missing credential raises naming the exact key. A half-configured archiver
that silently uploads nowhere is worse than one that refuses to start.

### How a transcript is classified zai vs llama vs claude vs openrouter

The archiver reads only what an assistant entry itself recorded — the
`message.model` field of `type: "assistant"` lines — never body text, because
a GLM session discussing "claude-opus-5" (or the reverse) is exactly what a
substring scan misfiles. Every assistant entry is read, and the session files
under the bucket of EVERY family it names, because a transcript that switched
harnesses mid-stream names more than one. An entry served through
OpenRouter's Anthropic-compatible endpoint is recognised by a `gen-`
`message.id` or, since that endpoint also answers with `msg_` ids, by a model
id shaped like an OpenRouter catalog slug (`vendor/model`, optionally
`:variant`; `anthropic/claude-…` among them). Either decides the entry: the
model text is never matched against the needles, and the entry is never
counted unknown. A path-shaped id (a llama.cpp server started without an
alias, `.gguf`, more than one `/`) is not a slug. Otherwise the model text
matches the known needles
(`glm` → zai, `bonsai`/`llama` → llama, `claude`/`anthropic` → claude).
A transcript that names no known family — a session that never got a reply,
say — is not archived anywhere: it is logged and left in place. A llama.cpp
server reports whatever alias it was started with, so a new GGUF on that lane
means a new needle in `provider.KNOWN_FAMILIES`, never a catch-all.
Classifications are memoised in `~/.claude/cleanup-sessions.providers.json`
keyed by uuid and size, so an unchanged transcript is not re-read every run.

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
pip-audit . && ls requirements*.txt | xargs -n1 pip-audit -r     # audit
actionlint .github/workflows/*.yml && zizmor .github/workflows/  # workflows
```

The workflow lint needs the actionlint build the `actionlint` workflow
installs, Nitjsefnie-OSC/actionlint v1.7.12-queue.1: stock 1.7.12 rejects
the `queue: max` key in `claim.yml`.

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
Gated at **94%**, a ratchet under the current 95.4%, not a target.

Five suites, one per seam, and only four of them move that number:

| Suite | What it covers |
| --- | --- |
| `test_store.py` | The destination — compression policy, manifest round-trip, inventory paging, the upload and stale-twin forms, credential resolution. |
| `test_runtime.py` | The host — the logger, the lock's exclusivity and release, the retention predicate, the local sweeps. |
| `test_archive_walks.py` | The three walks and their `main()` drivers — which files each archiver finds, which key it files them under, what it counts, what it will delete. |
| `test_archiver_behaviour.py` | What no other archiver shares: Codex key construction, and settings resolution. |
| `test_package_contract.py` | Reads the package as TEXT and parses its ASTs — no credential literal ships, the wheel promises what it promises. Deliberately never imports the code, so it contributes nothing to line coverage. |

The whole suite stays off the network. `store.client()` is the only place a
real boto3 client is built and nothing in the suite calls it: every test
constructs a `Store` around the in-memory stub in `tests/_util.py`, which
answers the four S3 operations the archivers actually use and raises on
anything else — including a request against a bucket other than the one the
Store was given.

What is left uncovered is the Windows `msvcrt` leg of the single-instance
lock, the `boto3.client(...)` call itself, and the handful of `OSError`
branches that need an unwritable filesystem to reach.

### CI

| Workflow | What it does |
| --- | --- |
| `tests` | `run_tests.py` across 3 OSes × 5 Pythons, plus a single-run coverage job — the matrix would otherwise report the same coverage number fifteen times. |
| `lint` / `types` | pylint + pycodestyle, and pyright. |
| `codeql` | Security analysis (Python and the `actions` workflow files — no JS here). Findings go to the Security tab, never the build. Weekly cron on top of push, because a query published today would otherwise only ever run against files touched after it shipped. |
| `audit` | `pip-audit` over the package (`pyproject.toml`) and over each root `requirements*.txt` manifest, one run per manifest, resolving the full transitive tree. **Daily** cron: this answers "is a version we froze months ago still safe", and that answer changes with no commit here to hang it on. |
| `secrets` | `gitleaks` over the full git history, on every pull request, every push to `main`, a daily cron and manual dispatch. A finding fails the check. |
| `actionlint` | `actionlint` + `zizmor` over the workflow YAML. A broken workflow does not go red, it silently stops running. |
| `pr gate` | `Nitjsefnie-Actions/pr-gate`: the pull-request description against `.github/PULL_REQUEST_TEMPLATE.md`, every issue it closes assigned to its author, Conventional Commits subjects, and every commit's author and committer attributable to the pull-request author. |
| `claim` | `Nitjsefnie-Actions/claim`: `/claim`, `/unclaim` and `/release` on an issue assign and unassign the commenter. |
| `pin test screen` | `Nitjsefnie-Actions/pin-guard`: comments on test lines that assert a pinned dependency version, tag, SHA or digest. |
| `comment share` | `Nitjsefnie-Actions/comment-ratchet`: a pull request may not raise a tracked file's comment share above 15%, or above its record in `.github/comment-share.json`. |
| `size baseline` | `Nitjsefnie-Actions/size-ratchet`: per-file line ceilings and the `tests/` line total, recorded in `.github/size-baseline.json`. |
| `tag` | Watches `session_archivers/__init__.py`. When `__version__` on `main` is a release version `X.Y.Z` (never a `.devN` one), it waits for the named gates (`pycodestyle`, `pylint`, `pyright`, `pip-audit`, `analyze`, `suites`, `wheel`, `coverage`) to pass and every other check on that commit to end `success`, `skipped` or `neutral`, pushes `vX.Y.Z`, starts `release`, then commits `X.Y.(Z+1).dev0` to `main`. |
| `release` | Fires on that tag: runs every suite, builds the wheel, and publishes `SHA256SUMS` beside it. |

**There is deliberately no speed gate here**, unlike the dashboard repos.
pytest cannot run these suites — the tests are plain functions taking
helper arguments, which pytest reads as fixture requests — and the
comparator needs `--junitxml`, which only pytest emits. Rather than
reshape the tests to suit a gate, the gate is omitted.

**Releasing = one commit setting `__version__` to the release version.**
Between releases `main` carries a development version, `X.Y.Z.devN`,
which `tag` never tags. The commit that sets it to a release version
`X.Y.Z` is the release; choosing patch, minor or major stays a judgement
about what changed. `tag` waits for the named gates on that commit, tags
it, starts `release`, which publishes from the tag, and then commits
`X.Y.(Z+1).dev0` to `main`, so no later commit claims a published
version. If that last push is refused because `main` moved, re-run the
failed `bump` job.

**Actions are hash-pinned**, with the version in a trailing comment. Do
not "tidy" one back to `@v4`: a tag is a moving pointer, and these jobs
run with a repository token. Dependabot keeps the hashes current.

**`.gitignore` is deny-by-default**: `*` first, then each shipped path
named back. `build/`, `dist/` and `*.egg-info/` need no rules at all now —
they are simply never named back. A new file of an unlisted type is
invisible to git and will NOT appear in `git status`;
`git check-ignore -v <path>` names the rule hiding it. Never "fix" it by
loosening the leading `*`.

**The comment-share and size records** are kept by their actions, and they
never rise. With the deploy key of the `main-push` environment, each push to
`main` tightens `.github/comment-share.json` and `.github/size-baseline.json`
and commits any change itself. The adoption pull request seeded
`.github/comment-share.json` by running the action's tighten over its own
head (`COMMENT_RATCHET_MODE=tighten GITHUB_WORKSPACE="$PWD" python3 -I
<comment-ratchet checkout>/run.py`); the first push to `main` seeds the size
records.
