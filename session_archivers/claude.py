#!/usr/bin/env python3
'''Archive Claude Code sessions to R2 + sweep stale ~/.claude scratch dirs.

Designed to run hourly via cron or Task Scheduler.

Behaviour per run:
1. Acquire single-instance lock. If a previous run still holds it, exit
   silently.
2. Sweep claude-local stale paths (files / dirs older than DAYS):
   ~/.claude/debug/*, ~/.claude/file-history/*/, ~/.claude/telemetry/*
3. For every project under ~/.claude/projects/<project>/:
     Pass 1 — each *.jsonl plus its matching UUID data dir:
       the transcript is classified zai (GLM), llama (local llama.cpp) or
       claude (Anthropic) and the whole session — transcript and data dir —
       files under that provider's bucket; upload (size-skip, idempotent), then delete locally if jsonl
       mtime > DAYS.
     Pass 2 — orphan UUID dirs (no matching jsonl, excluding memory/, tasks/):
       routed by the transcript inside them when one is there, else claude;
       upload always, delete if dir mtime > DAYS.
4. Log a one-liner summary to ~/.claude/cleanup-sessions.log.

Only the WALK is here. The destination — key layout, compression policy,
manifest, inventory — lives in store.py, and the host-side scaffolding —
logging, locking, the retention predicate — in runtime.py, both shared with
the Kimi and Codex archivers; provider.py classifies transcripts for the
per-provider split.

Flags:
  --days N    retention threshold for the delete gate (default 3)
  --dry-run   preview every action; no R2 puts, no local removals

Exit code: 0 on success, 1 if any upload failed.
'''

import argparse
import json
import shutil
import time
from pathlib import Path

from . import provider, runtime, store
from .settings import setting

# R2 targets. Credentials and account live in the `env` block of
# ~/.agent-bundle/settings.json, never here — this file ships in the bundle and
# is mirrored to a public git repository, so a literal key would travel with
# the code to every machine that installs it. See store.r2_config.
BUCKET = setting('R2_BUCKET_CLAUDE', 'claude')
# GLM sessions arrive through the same Claude Code tree as Anthropic ones —
# same projects layout, same key layout — but they are a different provider's
# data and keep their own bucket.
ZAI_BUCKET = setting('R2_BUCKET_ZAI', 'zai')
# Same again for sessions served by a local llama.cpp server through its
# Anthropic-compatible endpoint: same tree, a third provider's data.
LLAMA_BUCKET = setting('R2_BUCKET_LLAMA', 'llama')

CLAUDE_DIR = Path.home() / '.claude'
PROJECTS_DIR = CLAUDE_DIR / 'projects'
DEBUG_DIR = CLAUDE_DIR / 'debug'
FILE_HISTORY_DIR = CLAUDE_DIR / 'file-history'
TELEMETRY_DIR = CLAUDE_DIR / 'telemetry'

LOCK_FILE = CLAUDE_DIR / 'cleanup-sessions.lock'
LOG_FILE = CLAUDE_DIR / 'cleanup-sessions.log'
# uuid -> [size at scan time, provider or '']. Local-only bookkeeping beside
# the lock and log: losing it costs one rescan, never a misroute.
PROVIDER_CACHE = CLAUDE_DIR / 'cleanup-sessions.providers.json'

DAYS = runtime.DAYS

# Working state, not session history — and memory/ is private notes. Neither
# belongs in the bucket.
NOT_SESSION_DIRS = ('memory', 'tasks')


def _entry_ok(entry):
    """A cache value is [size, provider-or-''] and nothing else."""
    return (isinstance(entry, list) and len(entry) == 2
            and isinstance(entry[0], int) and isinstance(entry[1], str))


class _ProviderCache:
    """uuid -> [size at scan time, provider or ''] for this machine.

    The routing scan reads a whole transcript; without a memo every hourly
    run re-read every transcript in the tree just to re-learn what the first
    assistant entry already said. A transcript is append-only, so a provider
    once classified holds no matter how much the file grows; only a file
    SMALLER than the cache remembers (rewritten under the same uuid) and a
    still-unclassified file that changed (new assistant entries may have
    appeared) are scanned again.

    `record` sizes with the PRE-scan stat, so a file that grows while being
    read records a size below its current one and the next run rescans —
    the growth might contain the first assistant entry.
    """

    def __init__(self, path):
        self.path = Path(path)
        self.entries = {}
        self.dirty = False
        try:
            loaded = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return
        if isinstance(loaded, dict):
            self.entries = {key: value for key, value in loaded.items()
                            if _entry_ok(value)}

    def known(self, key, size):
        '''The cached provider for `key`, or None when it must be scanned.

        Returns the provider string when it holds for a file of this size;
        `provider.CLAUDE` for an unchanged file that never classified; None
        when the file must be scanned.
        '''
        hit = self.entries.get(key)
        if hit is None:
            return None
        old_size, name = hit[0], hit[1]
        if size < old_size:
            return None  # shrank: rewritten under the same name, rescan
        if name:
            return name  # classified once, first-model is append-stable
        return provider.CLAUDE if size == old_size else None

    def record(self, key, size, found):
        self.entries[key] = [size, found or '']
        self.dirty = True

    def save(self, log, dry_run):
        if dry_run or not self.dirty:
            return
        try:
            body = json.dumps(self.entries, separators=(',', ':'))
            self.path.write_text(body, encoding='utf-8')
        except OSError as e:
            log(f'provider cache save failed: {e}')


def _route(cache, key, source):
    '''The provider a session files under: 'zai', 'llama' or 'claude'.

    `source` is the transcript, or an orphan dir (routed by the transcript
    inside it when one is there). Unclassifiable sessions file under the
    claude bucket — the harness's own — which is the conservative default:
    only positive evidence of another family routes away from it.
    '''
    if not source.is_file():
        source = provider.transcript_in(source)
        if source is None:
            return provider.CLAUDE
    try:
        size = source.stat().st_size
    except OSError:
        size = -1
    known = cache.known(key, size)
    if known is not None:
        return known
    found = provider.of_file(source)
    cache.record(key, size, found)
    return found or provider.CLAUDE


def cleanup_local(cutoff, dry_run, log):
    '''Delete ~/.claude/{debug/*, file-history/*/, telemetry/*} past `cutoff`.'''
    runtime.sweep_files(DEBUG_DIR, cutoff, dry_run, log)
    runtime.sweep_dirs(FILE_HISTORY_DIR, cutoff, dry_run, log)
    runtime.sweep_files(TELEMETRY_DIR, cutoff, dry_run, log)


def archive_projects(dest, zai_dest, cutoff, llama_dest=None):
    '''Upload every project, delete what is past the retention gate.

    Each session files under the bucket of the provider its transcript
    names — `dest` for claude, `zai_dest` for GLM, `llama_dest` for a local
    llama.cpp server (falling back to `dest` when no store is given, the
    pre-1.3 behaviour). Returns (uploaded, deleted, failed) summed over
    every bucket.
    '''
    n_uploaded = n_deleted = n_failed = 0
    if not PROJECTS_DIR.is_dir():
        return 0, 0, 0
    cache = _ProviderCache(PROVIDER_CACHE)
    routes = {provider.ZAI: zai_dest, provider.LLAMA: llama_dest or dest}

    for project_dir in PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        project = project_dir.name

        # Pass 1: each transcript and its matching UUID data dir.
        jsonl_stems = set()
        for jsonl in project_dir.glob('*.jsonl'):
            uuid = jsonl.stem
            jsonl_stems.add(uuid)
            r2_prefix = f'{project}/{uuid}'
            uuid_dir = project_dir / uuid
            fam = _route(cache, f'{project}/{uuid}', jsonl)
            route = routes.get(fam, dest)

            try:
                if route.upload_file(jsonl, f'{r2_prefix}/{uuid}.jsonl'):
                    n_uploaded += 1
            except Exception as e:  # pylint: disable=broad-except
                route.log(f'upload failed: jsonl={jsonl} err={e}')
                n_failed += 1
                continue

            if uuid_dir.is_dir():
                try:
                    n_uploaded += route.upload_dir(uuid_dir, f'{r2_prefix}/data')
                except Exception as e:  # pylint: disable=broad-except
                    route.log(f'upload failed: uuid_dir={uuid_dir} err={e} '
                               '(jsonl uploaded; not deleting either)')
                    n_failed += 1
                    continue

            if runtime.is_old(jsonl, cutoff):
                if route.dry_run:
                    route.log(f'  DRY rm {jsonl}')
                    if uuid_dir.is_dir():
                        route.log(f'  DRY rmtree {uuid_dir}')
                else:
                    try:
                        jsonl.unlink()
                    except OSError as dest_e:
                        route.log(f'  rm failed: {jsonl}: {dest_e}')
                        continue
                    if uuid_dir.is_dir():
                        shutil.rmtree(uuid_dir, ignore_errors=True)
                n_deleted += 1

        # Pass 2: orphan UUID dirs, whose transcript is already gone.
        for sub in project_dir.iterdir():
            if not sub.is_dir() or sub.name in NOT_SESSION_DIRS:
                continue
            if sub.name in jsonl_stems:
                continue  # already handled in pass 1
            fam = _route(cache, f'{project}/{sub.name}', sub)
            route = routes.get(fam, dest)
            try:
                n_uploaded += route.upload_dir(sub, f'{project}/{sub.name}/data')
            except Exception as e:  # pylint: disable=broad-except
                route.log(f'upload failed: orphan uuid_dir={sub} err={e}')
                n_failed += 1
                continue
            if runtime.is_old(sub, cutoff):
                if route.dry_run:
                    route.log(f'  DRY rmtree {sub}')
                else:
                    shutil.rmtree(sub, ignore_errors=True)
                n_deleted += 1

    cache.save(dest.log, dest.dry_run)
    return n_uploaded, n_deleted, n_failed


def main():
    runtime.reconfigure_streams()
    ap = argparse.ArgumentParser(
        description=(__doc__ or '').split('\n\n', maxsplit=1)[0])
    ap.add_argument('--days', type=int, default=DAYS,
                    help=f'retention threshold for delete (default {DAYS})')
    ap.add_argument('--dry-run', action='store_true',
                    help='preview only — no uploads, no removals')
    args = ap.parse_args()

    log = runtime.Logger(LOG_FILE)
    cutoff = time.time() - args.days * 86400

    lock = runtime.acquire_lock(LOCK_FILE)
    if lock is None:
        log('skipped: previous run still holds the lock')
        return 0

    try:
        log(f'starting cleanup-sessions (DAYS={args.days}, dry_run={args.dry_run})')
        cleanup_local(cutoff, args.dry_run, log)

        client = store.client()
        dest = store.Store(client, BUCKET, log, args.dry_run)
        zai_dest = store.Store(client, ZAI_BUCKET, log, args.dry_run)
        llama_dest = store.Store(client, LLAMA_BUCKET, log, args.dry_run)
        stores = (dest, zai_dest, llama_dest)
        for each in stores:
            log(f'remote inventory: {len(each.inventory()):,} objects '
                f'in bucket {each.bucket!r}')
            log(f'manifest {store.manifest_key()}: '
                f'{len(each.load_manifest()):,} known compressed objects')

        n_up, n_del, n_fail = archive_projects(dest, zai_dest, cutoff,
                                               llama_dest=llama_dest)

        if not args.dry_run:
            for each in stores:
                try:
                    each.save_manifest()
                except Exception as e:  # pylint: disable=broad-except
                    log(f'manifest save failed: {type(e).__name__}: {e}')

        log(f'done — uploaded={n_up} deleted={n_del} failures={n_fail}')
        return 1 if n_fail else 0
    finally:
        try:
            lock.close()
        except Exception as e:  # pylint: disable=broad-except
            log(f'lock.close() failed: {type(e).__name__}: {e}')


if __name__ == '__main__':
    raise SystemExit(main())
