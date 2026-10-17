#!/usr/bin/env python3
"""Export a standalone public reading snapshot, excluding stale versions and local process state."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil

HERE = Path(__file__).resolve().parent


def export(source, output, max_part_bytes=3 * 1024 * 1024):
    source, output = source.resolve(), output.resolve()
    if source == output or source in output.parents or output in source.parents:
        raise ValueError('Use a separate output directory')
    if max_part_bytes < 1:
        raise ValueError('Part size must be positive')
    manifest = json.loads((source / 'data/manifest.json').read_text())
    paths = {'data/runs.csv': ('data/runs.csv', 0, None)}
    for descriptor in manifest['files'].values():
        parts = []
        for path in descriptor['parts']:
            if PurePosixPath(path).is_absolute() or '..' in PurePosixPath(path).parts:
                raise ValueError('Asset path escapes the snapshot')
            relative = 'data/' + path
            original = (source / relative).resolve()
            if not original.is_relative_to(source):
                raise ValueError('Asset path escapes the snapshot')
            size = original.stat().st_size
            if size > max_part_bytes:
                for number, offset in enumerate(range(0, size, max_part_bytes)):
                    part = f'{path}.chunk-{number:04d}'
                    parts.append(part)
                    paths['data/' + part] = (relative, offset, min(max_part_bytes, size - offset))
            else:
                parts.append(path)
                paths[relative] = (relative, 0, None)
        descriptor['parts'] = parts
    output.mkdir(parents=True, exist_ok=True)
    for stale in (output/'data/models').rglob('*'):
        if stale.is_file() and str(stale.relative_to(output)) not in paths:
            stale.unlink()
    (output/'data/crawl-status.json').unlink(missing_ok=True)
    shutil.copytree(HERE / 'web', output, dirs_exist_ok=True)
    for relative, (source_path, offset, size) in sorted(paths.items()):
        original = (source / source_path).resolve()
        if not original.is_relative_to(source):
            raise ValueError('Asset path escapes the snapshot')
        destination = output / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if size is None:
            shutil.copy2(original, destination)
        else:
            with original.open('rb') as incoming, destination.open('wb') as outgoing:
                incoming.seek(offset)
                block = incoming.read(size)
                if len(block) != size:
                    raise ValueError('Source asset is truncated')
                outgoing.write(block)
    manifest['public_snapshot'] = True
    raw = json.dumps(manifest, ensure_ascii=False, separators=(',', ':'))
    (output / 'data/manifest.json').write_text(raw)
    (output / '.nojekyll').touch()
    files = [path for path in output.rglob('*') if path.is_file()]
    result = {'snapshot_sha256': hashlib.sha256(raw.encode()).hexdigest(),
              'files': len(files), 'bytes': sum(path.stat().st_size for path in files),
              'counts': manifest['counts']}
    print(json.dumps(result), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=HERE / 'dist')
    parser.add_argument('--output', type=Path, default=HERE / 'public-site')
    parser.add_argument('--max-part-bytes', type=int, default=3 * 1024 * 1024)
    args = parser.parse_args()
    if args.max_part_bytes < 1:
        parser.error('--max-part-bytes must be positive')
    export(args.source, args.output, args.max_part_bytes)


if __name__ == '__main__':
    main()
