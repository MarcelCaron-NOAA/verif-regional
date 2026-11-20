
#!/usr/bin/env python3
"""Compare generated MET STAT records with a reference STAT file."""

import argparse
from collections import Counter
from pathlib import Path


def records(path):
    with path.open() as stream:
        for line in stream:
            fields = line.split()
            if fields and fields[0] != 'VERSION':
                yield tuple(fields)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('reference', type=Path)
    parser.add_argument('generated', nargs='+', type=Path)
    args = parser.parse_args()

    reference = Counter(records(args.reference))
    files = []

    for path in args.generated:
        if path.is_file() and path.suffix == '.stat':
            files.append(path)
        elif path.is_dir():
            files.extend(sorted(path.rglob('*.stat')))
        else:
            parser.error(f'Not a STAT file or directory: {path}')

    if not files:
        parser.error('No generated STAT files found')

    generated = Counter(
        record
        for path in files
        for record in records(path)
    )

    if not generated:
        parser.error('Generated STAT files contained no records')

    unmatched = generated - reference

    print(f'Generated STAT files: {len(files)}')
    print(f'Generated records:    {sum(generated.values())}')
    print(f'Unmatched records:    {sum(unmatched.values())}')

    if unmatched:
        print('\nFirst unmatched records:')
        for record in list(unmatched.elements())[:5]:
            print(' '.join(record))
        return 1

    print('\nPASS: All generated records match reference EVS')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

