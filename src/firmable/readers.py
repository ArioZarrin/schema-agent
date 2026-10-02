"""Prepare text encodings for generic readers without changing the raw file."""
from pathlib import Path
import csv
import hashlib
import shutil

import pandas as pd


def prepare_csv_input(path, cache_dir, encoding=None):
    path = Path(path)
    sample = path.open('rb')
    try:
        prefix = sample.read(65536)
    finally:
        sample.close()
    if prefix.startswith((b'\xff\xfe\x00\x00', b'\x00\x00\xfe\xff')):
        candidates = ['utf-32']
    elif prefix.startswith((b'\xff\xfe', b'\xfe\xff')):
        candidates = ['utf-16']
    elif prefix and prefix[1::2].count(0) > len(prefix[1::2]) * .4:
        candidates = ['utf-16-le']
    elif prefix and prefix[::2].count(0) > len(prefix[::2]) * .4:
        candidates = ['utf-16-be']
    else:
        candidates = ['utf-8-sig', 'cp1252', 'latin-1']
    if encoding:
        candidates.insert(0, encoding)
    selected = None
    for candidate in dict.fromkeys(candidates):
        try:
            # Check the whole file with bounded memory, not just the first rows.
            with path.open(encoding=candidate, errors='strict', newline='') as source:
                while source.read(1024 * 1024):
                    pass
            selected = candidate
            break
        except (UnicodeError, LookupError):
            continue
    if selected is None:
        raise ValueError(f'No usable text encoding for {path.name}')
    prepared = path
    if selected != 'utf-8-sig' or prefix.startswith(b'\xef\xbb\xbf'):
        cache_dir = Path(cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
        stamp = f'{path.resolve()}:{path.stat().st_mtime_ns}:{path.stat().st_size}:{selected}'
        key = hashlib.sha256(stamp.encode()).hexdigest()[:12]
        prepared = cache_dir / f'{key}_{path.name}'
        if not prepared.exists():
            temporary = prepared.with_suffix(prepared.suffix + '.tmp')
            try:
                with path.open(encoding=selected, errors='strict', newline='') as source:
                    with temporary.open('w', encoding='utf-8', newline='') as target:
                        shutil.copyfileobj(source, target, length=1024 * 1024)
                temporary.replace(prepared)
            finally:
                temporary.unlink(missing_ok=True)
    return {
        'original_path': str(path), 'file_path': str(prepared),
        'encoding': selected, 'converted_to_utf8': prepared != path,
    }


def read_csv_python(path, delimiter=None, header=True, skip=0, limit=None):
    """Retain ragged rows, padding missing cells and naming extra columns."""
    path = Path(path)
    with path.open(encoding='utf-8', newline='') as source:
        for _ in range(skip):
            next(source, None)
        sample = source.read(65536)
    if delimiter is None:
        try:
            delimiter = csv.Sniffer().sniff(sample, delimiters=',;\t|').delimiter
        except csv.Error:
            delimiter = ','
    try:
        with path.open(encoding='utf-8', newline='') as source:
            for _ in range(skip):
                next(source, None)
            parser = csv.reader(source, delimiter=delimiter, strict=True)
            rows = []
            for row in parser:
                rows.append(row)
                if limit is not None and len(rows) >= limit + int(header):
                    break
    except csv.Error:
        # The raw source remains available even when its quoting is malformed.
        with path.open(encoding='utf-8', newline='') as source:
            rows = source.readlines()[skip:]
        if limit is not None:
            rows = rows[:limit]
        return pd.DataFrame({'raw_record': rows}, dtype=str), {
            'parser': 'python_raw_lines', 'reason': 'Malformed CSV quoting; raw lines retained for review.'
        }
    names = rows.pop(0) if header and rows else []
    width = max([len(names), *(len(row) for row in rows)], default=0) or 1
    columns = []
    used = set()
    for index in range(width):
        name = names[index] if index < len(names) and names[index] else f'column_{index + 1}'
        unique, number = name, 1
        while unique.casefold() in used:
            unique = f'{name}_{number}'
            number += 1
        used.add(unique.casefold())
        columns.append(unique)
    padded = [row + [''] * (width - len(row)) for row in rows]
    return pd.DataFrame(padded, columns=columns, dtype=str), {
        'parser': 'python_csv', 'records': len(rows), 'delimiter': delimiter,
        'padded_rows': sum(len(row) < width for row in rows),
        'extra_columns': max(0, width - len(names)) if header else 0,
    }
