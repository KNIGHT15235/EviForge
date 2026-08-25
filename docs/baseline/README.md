# Runnable baseline audit

Audit date: 2026-08-12 (Asia/Shanghai)

## Revisions

- `raw-import`: imported source tree plus the implementation-plan document.
- `runnable-baseline`: locked dependencies plus the smallest compatibility fix
  needed for the complete test suite to collect.

The imported source scope recorded at `raw-import` contains 163 files,
1,072,746 bytes, with aggregate SHA-256
`6d7c322e9ef9fb87a3adf48e89a0a369dc7b280d3d7f2d34934d50e0449c0a6c`
at `raw-import`.

The same verification reports all four removed watermark strings at zero in
the original source scope. (The verification script contains those strings as
test markers and is outside that imported scope.)

The canonical 163-row list is stored in
[`source_manifest.tsv`](./source_manifest.tsv). Recheck the original tree and
the Git tag without writing either tree:

```powershell
python scripts/verify_source_snapshot.py `
  --source "$env:EVIFORGE_RAW_SOURCE" `
  --repo . --revision raw-import `
  --manifest docs/baseline/source_manifest.tsv
```

## Environment

- Python: 3.12.7
- uv: 0.11.8
- pytest: 9.0.3
- pytest-asyncio: 1.3.0
- install: `uv sync --frozen --group dev`

## Collection result

Command:

```powershell
uv run pytest -p no:cacheprovider
```

Result after the compatibility fix: 548 tests collected, zero collection
errors. The run is not a green functional baseline: the imported suite contains
known platform assumptions and source/test drift. A 60-second audit reached the
first half of the suite and already showed failures, so this revision must not
be described as “all tests passing”.

Confirmed pre-existing failure families include:

- POSIX `/tmp` paths used on Windows;
- MCP config mapping/list contract drift;
- Plan-mode `ask` versus `deny` contract drift;
- WorktreeManager `file_cache` constructor drift;
- Team backend detection intentionally returning in-process while tests expect
  tmux/iTerm2 probing;
- prompt constants removed from source but still imported by tests.

These behavior-changing fixes belong to candidate commits or explicit
ablations, not to the runnable baseline.

## Compatibility fix

`mewcode.memory.session.build_time_gap_message` was restored because its absence
prevented collection of the complete suite. Its focused test file passes:

```text
uv run pytest -p no:cacheprovider tests/test_memory.py -q
52 passed
```
