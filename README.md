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

Extracted from [`agent-harness-bundle`](https://github.com/Nitjsefnie/agent-harness-bundle),
where they ran only on whichever machine last exercised them by hand. Here CI
runs the suite on every push. The bundle keeps thin wrappers at the documented
paths, so every schedule and doc naming one of them still works.

## License

MIT.
