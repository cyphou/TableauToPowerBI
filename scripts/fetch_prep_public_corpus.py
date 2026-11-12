"""Fetch or verify the pinned public Tableau Prep corpus."""

import argparse
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import quote
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / 'tests' / 'prep_dialect_samples' / 'public_corpus_manifest.json'
MAX_BYTES = 64 * 1024 * 1024


def _validate_entries(entries):
    if not isinstance(entries, list) or not entries:
        raise ValueError('Corpus manifest must contain input entries')
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError('Corpus input must be an object')
        name = entry.get('filename', '')
        repository = entry.get('repository', '')
        commit = entry.get('commit', '')
        path = entry.get('path', '')
        digest = entry.get('sha256', '')
        if not isinstance(name, str) or not re.fullmatch(
                r'[A-Za-z0-9][A-Za-z0-9._-]*\.tflx?', name):
            raise ValueError('Invalid corpus filename')
        if name.casefold() in seen:
            raise ValueError('Colliding corpus filenames')
        seen.add(name.casefold())
        if not isinstance(repository, str) or not re.fullmatch(
                r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository) or any(
                    part in ('.', '..') for part in repository.split('/')):
            raise ValueError('Invalid corpus repository')
        if not isinstance(commit, str) or not re.fullmatch(r'[0-9a-f]{40}', commit):
            raise ValueError('Invalid corpus commit')
        if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
            raise ValueError('Invalid corpus SHA-256')
        if (not isinstance(path, str) or not path or '\\' in path
                or any(part in ('', '.', '..') for part in path.split('/'))
                or any(ord(char) < 32 or ord(char) == 127 for char in path)):
            raise ValueError('Invalid corpus source path')


def fetch_corpus(entries, output_dir, *, verify_only=False, opener=None):
    """Verify existing inputs and, unless offline, fetch missing pinned inputs."""
    _validate_entries(entries)
    destination = Path(output_dir)
    if verify_only:
        if not destination.is_dir():
            raise ValueError('Corpus directory does not exist')
    else:
        destination.mkdir(parents=True, exist_ok=True)
    pending = []
    for entry in entries:
        target = destination / entry['filename']
        if target.is_symlink():
            raise ValueError(f'Corpus file is a symlink: {target.name}')
        if target.exists():
            if not target.is_file():
                raise ValueError(f'Existing corpus file differs: {target.name}')
            with target.open('rb') as stream:
                existing_digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            if existing_digest != entry['sha256']:
                raise ValueError(f'Existing corpus file differs: {target.name}')
        elif verify_only:
            raise ValueError(f'Corpus file is missing: {target.name}')
        else:
            pending.append(entry)

    fetch = opener or urlopen
    for entry in pending:
        url = ('https://raw.githubusercontent.com/' + entry['repository'] + '/'
               + entry['commit'] + '/' + quote(entry['path'], safe='/'))
        temp_path = None
        try:
            with fetch(url, timeout=30) as response:
                with tempfile.NamedTemporaryFile(
                        mode='wb', dir=destination, prefix='.prep-download-',
                        delete=False) as stream:
                    temp_path = stream.name
                    digest = hashlib.sha256()
                    total = 0
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > MAX_BYTES:
                            raise ValueError(f'Corpus file exceeds size limit: {entry["filename"]}')
                        digest.update(chunk)
                        stream.write(chunk)
            if digest.hexdigest() != entry['sha256']:
                raise ValueError(f'Corpus SHA-256 mismatch: {entry["filename"]}')
            os.link(temp_path, destination / entry['filename'])
        finally:
            if temp_path is not None:
                os.unlink(temp_path)
    return {'verified': len(entries) - len(pending), 'downloaded': len(pending)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output_dir', help='Directory for downloaded Prep files')
    parser.add_argument('--check', action='store_true',
                        help='Verify existing files without network access')
    args = parser.parse_args(argv)
    with MANIFEST.open(encoding='utf-8') as stream:
        manifest = json.load(stream)
    try:
        result = fetch_corpus(manifest['inputs'], args.output_dir, verify_only=args.check)
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(1, f'Corpus verification failed: {exc}\n')
    print(f"Verified: {result['verified']}; downloaded: {result['downloaded']}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
