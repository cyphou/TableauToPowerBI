"""Sprint 132.3 — Hot-path profiling script.

Wraps cProfile around the extraction → generation pipeline and outputs:
    1. A text summary (top 40 cumulative-time functions)
    2. A .prof binary for snakeviz / flamegraph
    3. Optionally a flamegraph SVG (requires flameprof: pip install flameprof)
    4. Optionally peak Python allocation with --memory

Usage:
    py scripts/profile_migration.py                         # default large_500 fixture
    py scripts/profile_migration.py path/to/workbook.twb    # custom workbook
    py scripts/profile_migration.py --flamegraph             # also generate SVG
    py scripts/profile_migration.py --top 60                 # show top 60 functions
    py scripts/profile_migration.py --memory                 # add traced peak allocation
"""

import argparse
import cProfile
import os
import pstats
import sys
import time
import tracemalloc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tableau_export'))
sys.path.insert(0, os.path.join(ROOT, 'powerbi_import'))


def _run_pipeline(twb_path, extract_dir, gen_dir):
    """Run extraction + generation, return elapsed seconds."""
    from powerbi_import.import_to_powerbi import PowerBIImporter
    from tableau_export.extract_tableau_data import TableauExtractor

    t0 = time.perf_counter()

    ext = TableauExtractor(twb_path, output_dir=extract_dir)
    ext.extract_all()

    imp = PowerBIImporter(source_dir=extract_dir)
    imp.import_all(generate_pbip=True, report_name='Profile', output_dir=gen_dir)

    return time.perf_counter() - t0


def main():
    parser = argparse.ArgumentParser(description='Profile migration pipeline')
    parser.add_argument('twb', nargs='?', default=None,
                        help='Path to .twb file (default: generate synthetic fixture)')
    parser.add_argument('--top', type=int, default=40,
                        help='Number of top functions to display')
    parser.add_argument('--flamegraph', action='store_true',
                        help='Generate flamegraph SVG (requires flameprof)')
    parser.add_argument('--memory', action='store_true',
                        help='Also trace peak Python allocation (slows CPU profile)')
    parser.add_argument('--output', default='profile_results',
                        help='Output directory for profile data')
    parser.add_argument('--measures', type=int, default=500,
                        help='Number of measures for synthetic fixture')
    parser.add_argument('--worksheets', type=int, default=100,
                        help='Number of worksheets for synthetic fixture')
    parser.add_argument('--datasources', type=int, default=50,
                        help='Number of datasources for synthetic fixture')
    args = parser.parse_args()

    os.makedirs(args.output, exist_ok=True)

    # Resolve TWB path
    if args.twb:
        twb_path = args.twb
    else:
        from tests.large_workbook_generator import generate_large_twb
        twb_path = os.path.join(args.output, 'synthetic.twb')
        print(f'Generating synthetic fixture ({args.measures} measures, '
              f'{args.worksheets} worksheets, {args.datasources} datasources)...')
        generate_large_twb(
            twb_path,
            num_measures=args.measures,
            num_worksheets=args.worksheets,
            num_datasources=args.datasources,
            seed=42,
        )
        file_kb = os.path.getsize(twb_path) / 1024
        print(f'  Generated: {twb_path} ({file_kb:.0f} KB)')

    extract_dir = os.path.join(args.output, 'tableau_export')
    gen_dir = os.path.join(args.output, 'powerbi_output')
    os.makedirs(extract_dir, exist_ok=True)
    os.makedirs(gen_dir, exist_ok=True)

    # Profile
    prof_path = os.path.join(args.output, 'migration.prof')
    print('\nProfiling pipeline...')

    if args.memory:
        tracemalloc.start()
    profiler = cProfile.Profile()
    profiler.enable()

    elapsed = _run_pipeline(twb_path, extract_dir, gen_dir)

    profiler.disable()
    peak_bytes = None
    if args.memory:
        _, peak_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()

    print(f'\n{"="*70}')
    summary = f'Pipeline completed in {elapsed:.2f}s'
    if peak_bytes is not None:
        summary += f' | Peak traced allocation: {peak_bytes / (1024 * 1024):.1f} MB'
    print(summary)
    print(f'{"="*70}')

    # Save binary profile
    profiler.dump_stats(prof_path)
    print(f'\nBinary profile saved: {prof_path}')
    print(f'  View with: py -m snakeviz {prof_path}')

    # Text summary
    txt_path = os.path.join(args.output, 'migration_profile.txt')
    with open(txt_path, 'w') as f:
        stats = pstats.Stats(profiler, stream=f)
        stats.sort_stats('cumulative')
        stats.print_stats(args.top)
    print(f'  Text summary: {txt_path}')

    # Also print to console
    print(f'\nTop {args.top} by cumulative time:')
    print('-' * 70)
    stats = pstats.Stats(profiler)
    stats.sort_stats('cumulative')
    stats.print_stats(args.top)

    # Flamegraph SVG (optional)
    if args.flamegraph:
        svg_path = os.path.join(args.output, 'flamegraph.svg')
        try:
            import subprocess
            result = subprocess.run(
                [sys.executable, '-m', 'flameprof', prof_path],
                capture_output=True, text=True,
            )
            if result.returncode == 0:
                with open(svg_path, 'w') as f:
                    f.write(result.stdout)
                print(f'\nFlamegraph: {svg_path}')
            else:
                print(f'\nFlameprof error: {result.stderr}')
                print('  Install with: pip install flameprof')
        except Exception as e:
            print(f'\nCould not generate flamegraph: {e}')
            print('  Install with: pip install flameprof')


if __name__ == '__main__':
    main()
