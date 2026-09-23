"""Which model families wrote a transcript: zai (GLM), llama (a local
llama.cpp server), claude (Anthropic) or openrouter (OpenRouter's
Anthropic-compatible endpoint).

The claude archiver walks one tree that mixes them — Claude Code sessions can
be powered by Anthropic's models, by GLM served through a z.ai endpoint, by a
GGUF served through llama.cpp's Anthropic-compatible endpoint, or by any model
served through OpenRouter's — and they must not land in the same bucket. The
classifier reads only what an assistant entry itself recorded — `type:
"assistant"` and `message.model` — and never body text: a session discussing
"claude-opus-5" while running on GLM (or the reverse) is exactly the
transcript a substring scan would misfile.

Classification collects EVERYTHING the transcript names: every assistant
entry's model is read, so the result is the set of known families present
plus the set of unknown model ids present. A transcript that switched
harnesses mid-stream names several families, and each of those buckets gets
the transcript. `<synthetic>` is Claude Code's placeholder for locally
generated messages and is ignored outright — evidence of nothing.

Unclassifiable — no assistant entry at all, only unknown model ids, or only
`<synthetic>` — is empty on both sets, and the caller skips the session
entirely: it is never uploaded, never deleted, and logged instead.
"""

import json

ZAI = 'zai'
LLAMA = 'llama'
CLAUDE = 'claude'
OPENROUTER = 'openrouter'

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

# Claude Code's placeholder model id for messages it generated locally (an
# interrupted turn, a hook reply). It says nothing about who served the
# session, so it counts as neither a known family nor an unknown id.
SYNTHETIC_MODEL = '<synthetic>'

# OpenRouter's Anthropic-compatible endpoint is the only source of assistant
# message ids starting `gen-`, and its model ids name OpenRouter's catalog —
# `anthropic/claude-…` among them — so the id prefix decides the entry and the
# model text is never needle-matched: an OpenRouter-served claude model is an
# OpenRouter session, not an Anthropic one.
OPENROUTER_ID_PREFIX = 'gen-'


def from_model(model):
    """'zai', 'llama' or 'claude' when `model` names a known family, else
    None. An openrouter entry never reaches here: `of_file` decides it from
    the `gen-` message id before any needle is consulted."""
    lowered = str(model or '').lower()
    for needle, family in KNOWN_FAMILIES:
        if needle in lowered:
            return family
    return None


def of_file(path):
    """(known families, unknown model ids) a transcript names.

    Every assistant entry's `message.model` is read — the first no longer
    decides, because a session can switch harnesses mid-stream and every
    family named earns the transcript in its bucket. Only non-empty string
    models are collected; `<synthetic>` is skipped outright.

    An entry whose `message.id` starts `gen-` was served by OpenRouter: it is
    family openrouter, and its model id is neither needle-matched nor counted
    unknown.

    Lines are read as bytes and parsed one at a time: a transcript being
    written by a live session can end mid-line, and one unparseable line
    must not cost the classification. A file that vanishes mid-walk reads as
    empty sets, which the caller treats as no model recorded.
    """
    families, unknowns = set(), set()
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
                model = message.get('model')
                if not isinstance(model, str) or not model:
                    continue
                if model.lower() == SYNTHETIC_MODEL:
                    continue
                entry_id = message.get('id')
                if (isinstance(entry_id, str)
                        and entry_id.startswith(OPENROUTER_ID_PREFIX)):
                    families.add(OPENROUTER)
                    continue
                found = from_model(model)
                if found:
                    families.add(found)
                else:
                    unknowns.add(model)
    except OSError:
        return set(), set()
    return families, unknowns


def transcript_in(directory):
    """The first *.jsonl directly under `directory` (sorted), or None.

    An orphan data dir with no transcript cannot say who ran it.
    """
    if not directory.is_dir():
        return None
    return next(iter(sorted(directory.glob('*.jsonl'))), None)
