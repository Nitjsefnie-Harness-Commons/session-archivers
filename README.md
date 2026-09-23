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
harnesses mid-stream names more than one. An entry whose `message.id` starts
`gen-` was served through OpenRouter's Anthropic-compatible endpoint, whose
model ids name OpenRouter's catalog (`anthropic/claude-…` among them) — so
the id decides, the model text is never matched, and the entry is never
counted unknown. Otherwise the model text matches the known needles
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
