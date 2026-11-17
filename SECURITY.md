# Security

Report vulnerabilities privately through a
[security advisory on this repository](https://github.com/Nitjsefnie-Harness-Commons/session-archivers/security/advisories/new),
not the public issue tracker. The form exists while private vulnerability
reporting is enabled for the repository (`gh api
repos/Nitjsefnie-Harness-Commons/session-archivers/private-vulnerability-reporting
--jq .enabled` answers `true` as of 2026-11-17). Include the package version,
which archiver you ran (`archive-claude-sessions`, `archive-kimi-sessions` or
`archive-codex-sessions`), the operating system and Python version, and the
output needed to reproduce the problem with credentials and transcript content
removed.

These archivers hold object-storage credentials and upload session transcripts,
which contain whatever an agent saw, then delete local session files. Three
things are therefore in scope:

- **Credentials.** The R2 access key and secret are read from the environment
  or from the `env` block of settings files under the user's home directory
  (see Configuration in [README.md](README.md)). A credential value reaching a
  log line, an object key, the per-machine manifest or an uploaded object is a
  vulnerability.
- **Uploaded transcripts.** A transcript uploaded to a bucket other than the one
  its archiver and provider classification name, or one the README says is left
  in place (a session that names no known model family) uploaded anyway. That
  archiving uploads transcript content is the tool's purpose, so it is not a
  finding in itself.
- **Deletion.** Three things are deleted, and a deletion outside its stated
  limit is in scope:
  - Local session files and directories, which must be past the `--days`
    retention gate, inside the harness directories the archivers walk, and
    (for transcripts) uploaded first. One removed after its upload failed, or
    outside those directories, is a finding.
  - Rows of the local Codex thread-history database
    (`thread_history_1.sqlite`), which `archive-codex-sessions` prunes after its
    rollout pass for threads whose rollout no longer exists. This prune has no
    `--days` gate. A row deleted for a thread that still has a rollout or whose
    writer lock is held is a finding.
  - Objects in the bucket: after uploading a file in one storage form
    (`<key>` or `<key>.xz`), the archiver deletes the other form of that same
    key if the bucket holds it. An object deleted in any other case is a
    finding.

  `--dry-run` must change nothing, locally or remotely; a dry run that does is
  in scope.

The repository's workflows and release artifacts, the wheel and its
`SHA256SUMS`, are in scope too, particularly anything that lets a pull request
or an issue comment run code with a repository token.

Reproduce only against synthetic transcripts in a scratch directory and a
bucket and token you control. Do not send a real credential or a real
transcript, in a report or anywhere else.

## What happens after you report

A report is acknowledged within three working days. That acknowledgement is a
person confirming they have read it, not a verdict.

You will get an assessment within ten working days: whether it reproduces, what
it affects and, if it is accepted, a rough idea of when a fix will land. If that
takes longer, you will be told why rather than left waiting.

Those clocks are one person's working days. This project has no fallback
maintainer, and [CONTRIBUTING.md](CONTRIBUTING.md) records what was checked, so
the promises above are conditional on that person being available. A slow
answer can reflect availability rather than anything about the report, and when
availability runs out the explanation runs out with it: nobody else will
answer, because this file refuses the public tracker and there is no second
maintainer to ask.

Disclosure is coordinated with you, in this order. The fix is released first,
and the advisory then describes it and credits you unless you ask otherwise.
Ninety days after the report is the backstop: if no fix has been released by
then, the advisory is published anyway, without one, so a report cannot be
buried by being left unanswered. Publishing is the maintainer's to do, so if
they are unavailable when the ninety days fall due it happens when they return;
until then this file never asks you to hold a report past ninety days, and the
choice to disclose is yours.

If a report is declined, you will be told the reason. Disagreeing with that is
reasonable, and saying so is welcome.
