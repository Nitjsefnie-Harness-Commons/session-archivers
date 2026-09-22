"""Which model family wrote a transcript: zai (GLM), llama (a local
llama.cpp server) or claude (Anthropic).

The claude archiver walks one tree that mixes them — Claude Code sessions can
be powered by Anthropic's models, by GLM served through a z.ai endpoint, or by
a GGUF served through llama.cpp's Anthropic-compatible endpoint — and they
must not land in the same bucket. The classifier reads only what
an assistant entry itself recorded — `type: "assistant"` and `message.model`
— and never body text: a session discussing "claude-opus-5" while running on
GLM (or the reverse) is exactly the transcript a substring scan would misfile.
The first assistant entry naming a known family decides; transcripts are
append-only, so a later entry naming another family does not re-open the
question.

Unclassifiable — no assistant entry at all, or only unknown model ids — is
None, and the caller files it under the harness's own bucket.
"""

import json

ZAI = 'zai'
LLAMA = 'llama'
CLAUDE = 'claude'

# A llama.cpp server reports whatever alias it was started with, so this names
# the models actually served that way rather than the server: add a needle
# when a new GGUF joins the lane. Unknown ids stay unclassified on purpose —
# routing every unrecognised name to llama would carry a future Anthropic
# model id there too.
KNOWN_FAMILIES = (
    ('glm', ZAI),
    ('bonsai', LLAMA),
    ('llama', LLAMA),
    ('claude', CLAUDE),
    ('anthropic', CLAUDE),
)


def from_model(model):
    """'zai', 'llama' or 'claude' when `model` names a known family, else None."""
    lowered = (model or '').lower()
    for needle, family in KNOWN_FAMILIES:
        if needle in lowered:
            return family
    return None


def of_file(path):
    """The provider of a transcript file, or None when it does not say.

    Lines are read as bytes and parsed one at a time: a transcript being
    written by a live session can end mid-line, and one unparseable line
    must not cost the classification. A file that vanishes mid-walk reads
    as None, which files it conservatively under the claude bucket.
    """
    try:
        with open(path, 'rb') as fh:
            for line in fh:
                if b'"model"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict) or entry.get('type') != 'assistant':
                    continue
                message = entry.get('message')
                if not isinstance(message, dict):
                    continue
                found = from_model(message.get('model'))
                if found:
                    return found
    except OSError:
        return None
    return None


def transcript_in(directory):
    """The first *.jsonl directly under `directory` (sorted), or None.

    An orphan data dir with no transcript cannot say who ran it.
    """
    if not directory.is_dir():
        return None
    return next(iter(sorted(directory.glob('*.jsonl'))), None)
