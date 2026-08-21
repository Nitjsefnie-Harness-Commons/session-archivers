"""R2 session archivers for the Claude Code, Kimi and Codex harnesses.

Each harness stores its sessions differently — Claude by project directory,
Kimi by working directory, Codex in date buckets — so the three walks are
separate modules rather than one parameterised routine. Each also keeps its
own destination: R2_BUCKET_CLAUDE, R2_BUCKET_KIMI and R2_BUCKET_CODEX are
three different buckets, and nothing here merges them.

What the three DO share is the way each one is written to — one key layout,
one compression policy, one per-machine manifest, one single-instance lock —
so a dashboard reads all three the same way. That half lives in two modules:

  * `store.py`   — the destination. Compression, manifest, inventory, the
                   upload forms. Takes the bucket name as an argument.
  * `runtime.py` — the host. Logging, the lock, the retention predicate, the
                   local scratch sweeps.

Those two were three identical copies until 1.1.0, which was correct while
these were standalone scripts copied onto a machine one file at a time. They
install as one package, so the copies bought nothing but three places for a
fix to land in two of.

They were extracted from the agent-harness-bundle, where they ran only on
whichever machine last exercised them by hand. Here CI runs their suite on
every push, across the operating systems they actually run on.
"""
__version__ = "1.1.1"
