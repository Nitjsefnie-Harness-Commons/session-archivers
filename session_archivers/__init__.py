"""R2 session archivers for the Claude Code, Kimi and Codex harnesses.

Each harness stores its sessions differently — Claude by project directory,
Kimi by working directory, Codex in date buckets — so the three archivers are
separate modules rather than one parameterised routine. What they share is the
destination: one R2 bucket layout, one manifest format, one single-instance
lock, and one compression policy, so a dashboard reads all three the same way.

They were extracted from the agent-harness-bundle, where they ran only on
whichever machine last exercised them by hand. Here CI runs their suite on
every push.
"""
__version__ = "1.0.0"
