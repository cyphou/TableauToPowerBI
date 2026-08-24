"""Run repeatable extraction and PBIP generation performance baselines.

The runner performs an optional warm-up followed by isolated measured runs. Each
run uses fresh temporary directories and validates the generated project. Wall
time is measured without tracemalloc; a separate run records peak Python
allocation so tracing overhead cannot distort timing statistics.
"""

import argparse
import contextlib
import json
import os
import platform
import shutil
import statistics
import sys
import tempfile
import time
import tracemalloc
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
for source_dir in ('tableau_export', 'powerbi_import'):
    source_path = os.path.join(ROOT, source_dir)
    if source_path not in sys.path:
        sys.path.insert(0, source_path)


def _percentile(values, percentile):
    """Return a linearly interpolated percentile for a non-empty sequence."""
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _summarize(values):
    """Build stable summary statistics for measured numeric values."""
    if not values:
        raise ValueError('Cannot summarize an empty measurement set')
    mean = statistics.fmean(values)
    return {
        'min': min(values),
        'median': statistics.median(values),
        'mean': mean,
        'p95': _percentile(values, 0.95),
        'max': max(values),
        'variation_percent': 0.0 if mean == 0 else (
            (max(values) - min(values)) / mean * 100.0
        ),
    }


def _measure(operation, trace_memory=False):
    """Return operation result, elapsed seconds, and optional traced peak MB."""
    if trace_memory:
        tracemalloc.start()
    started = time.perf_counter()
    try:
        result = operation()
        elapsed = time.perf_counter() - started
        peak_bytes = None
        if trace_memory:
            _, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        if trace_memory:
            tracemalloc.stop()
    peak_mb = None if peak_bytes is None else peak_bytes / (1024 * 1024)
    return result, elapsed, peak_mb


def _assert_pbip_output(output_dir, report_name):
    """Fail a fast but incomplete run when the expected PBIP project is absent."""
    project_dir = os.path.join(output_dir, report_name)
    expected = (
        os.path.join(project_dir, f'{report_name}.pbip'),
        os.path.join(project_dir, f'{report_name}.SemanticModel'),
        os.path.join(project_dir, f'{report_name}.Report'),
    )
    missing = [path for path in expected if not os.path.exists(path)]
    if missing:
        raise RuntimeError(
            'PBIP generation did not produce required artifacts: '
            + ', '.join(os.path.relpath(path, output_dir) for path in missing)
        )


def _generate_fabric(extract_dir, output_dir, report_name):
    """Generate and verify the six-artifact Fabric project."""
    from powerbi_import.fabric_project_generator import FabricProjectGenerator
    from powerbi_import.import_to_powerbi import PowerBIImporter

    started = time.perf_counter()
    input_started = time.perf_counter()
    extracted = PowerBIImporter(source_dir=extract_dir)._load_converted_objects()
    input_loading = time.perf_counter() - input_started
    generator = FabricProjectGenerator(output_dir=output_dir)
    result = generator.generate_project(report_name, extracted)
    validation_started = time.perf_counter()
    validation = result.get('validation', {})
    expected_artifacts = {
        'lakehouse', 'dataflow', 'notebook',
        'semantic_model', 'report', 'pipeline',
    }
    actual_artifacts = set(result.get('artifacts', {}))
    if (not validation.get('valid')
            or validation.get('artifacts_checked') != 6
            or actual_artifacts != expected_artifacts):
        raise RuntimeError(
            'Fabric generation did not produce a valid six-artifact bundle'
        )
    bundle_validation = time.perf_counter() - validation_started
    generator_report = result.get('phase_timings') or {}
    phases = {
        'input_loading': input_loading,
        **generator_report.get('phases', {}),
        'bundle_validation': bundle_validation,
    }
    total = time.perf_counter() - started
    phases['fabric_orchestration'] = max(
        0.0,
        total
        - input_loading
        - generator_report.get('total_seconds', 0.0)
        - bundle_validation,
    )
    measured = sum(phases.values())
    return {
        'total_seconds': total,
        'measured_seconds': measured,
        'coverage_percent': 0.0 if total == 0 else measured / total * 100.0,
        'phases': phases,
    }


def run_once(workbook_path, output_format='pbip', verbose=False,
             measure_memory=False):
    """Measure one extraction and generation run using isolated directories."""
    from powerbi_import.import_to_powerbi import PowerBIImporter
    from tableau_export.extract_tableau_data import TableauExtractor

    if output_format not in {'pbip', 'fabric'}:
        raise ValueError(f'Unsupported output format: {output_format}')

    report_name = os.path.splitext(os.path.basename(workbook_path))[0]
    run_dir = tempfile.mkdtemp(prefix='ttpbi_perf_')
    extract_dir = os.path.join(run_dir, 'extract')
    output_dir = os.path.join(run_dir, 'output')
    os.makedirs(extract_dir)
    os.makedirs(output_dir)

    try:
        generation_phase_timings = None
        output_stream = None
        if not verbose:
            output_stream = open(os.devnull, 'w', encoding='utf-8')

        def extract():
            extractor = TableauExtractor(workbook_path, output_dir=extract_dir)
            if not extractor.extract_all():
                raise RuntimeError(f'Extraction failed: {workbook_path}')

        output_context = contextlib.ExitStack()
        if output_stream is not None:
            output_context.enter_context(
                contextlib.redirect_stdout(output_stream))
            output_context.enter_context(
                contextlib.redirect_stderr(output_stream))
        with output_context:
            _, extraction_seconds, extraction_peak_mb = _measure(
                extract, trace_memory=measure_memory,
            )

        def generate():
            nonlocal generation_phase_timings
            if output_format == 'fabric':
                generation_phase_timings = _generate_fabric(
                    extract_dir, output_dir, report_name,
                )
                return
            importer = PowerBIImporter(source_dir=extract_dir)
            importer.import_all(
                generate_pbip=True,
                report_name=report_name,
                output_dir=output_dir,
            )
            generation_phase_timings = importer.last_generation_timings
            validation_started = time.perf_counter()
            _assert_pbip_output(output_dir, report_name)
            validation_seconds = time.perf_counter() - validation_started
            generation_phase_timings['phases']['output_validation'] = (
                validation_seconds
            )

        output_context = contextlib.ExitStack()
        if output_stream is not None:
            output_context.enter_context(
                contextlib.redirect_stdout(output_stream))
            output_context.enter_context(
                contextlib.redirect_stderr(output_stream))
        with output_context:
            _, generation_seconds, generation_peak_mb = _measure(
                generate, trace_memory=measure_memory,
            )
        measurement = {
            'extraction_seconds': extraction_seconds,
            'generation_seconds': generation_seconds,
            'total_seconds': extraction_seconds + generation_seconds,
            'generation_phase_timings': generation_phase_timings,
        }
        if measure_memory:
            measurement.update({
                'extraction_peak_mb': extraction_peak_mb,
                'generation_peak_mb': generation_peak_mb,
                'peak_mb': max(extraction_peak_mb, generation_peak_mb),
            })
        return measurement
    finally:
        if output_stream is not None:
            output_stream.close()
        shutil.rmtree(run_dir, ignore_errors=True)


def run_baseline(workbook_path, runs=3, warmup=1, output_format='pbip',
                 verbose=False):
    """Run warm and measured migrations, returning a serializable baseline."""
    if runs < 1:
        raise ValueError('runs must be at least 1')
    if warmup < 0:
        raise ValueError('warmup cannot be negative')
    if output_format not in {'pbip', 'fabric'}:
        raise ValueError(f'Unsupported output format: {output_format}')
    workbook_path = os.path.abspath(workbook_path)
    if not os.path.isfile(workbook_path):
        raise FileNotFoundError(workbook_path)

    for _ in range(warmup):
        run_once(
            workbook_path, output_format=output_format, verbose=verbose,
            measure_memory=False,
        )

    measurements = [
        run_once(
            workbook_path, output_format=output_format, verbose=verbose,
            measure_memory=False,
        )
        for _ in range(runs)
    ]
    memory_measurement = run_once(
        workbook_path, output_format=output_format, verbose=verbose,
        measure_memory=True,
    )
    timing_metrics = (
        'extraction_seconds', 'generation_seconds', 'total_seconds',
    )
    memory_metrics = (
        'extraction_peak_mb', 'generation_peak_mb', 'peak_mb',
    )
    summary = {
        name: _summarize([measurement[name] for measurement in measurements])
        for name in timing_metrics
    }
    summary.update({
        name: _summarize([memory_measurement[name]])
        for name in memory_metrics
    })
    phase_names = sorted({
        phase_name
        for measurement in measurements
        for phase_name in (
            measurement.get('generation_phase_timings') or {}
        ).get('phases', {})
    })
    generation_phases = {
        phase_name: _summarize([
            measurement['generation_phase_timings']['phases'].get(
                phase_name, 0.0,
            )
            for measurement in measurements
        ])
        for phase_name in phase_names
    }
    generation_phase_coverage = _summarize([
        sum((measurement.get('generation_phase_timings') or {}).get(
            'phases',
        ).values()) / measurement['generation_seconds'] * 100.0
        if measurement['generation_seconds'] else 0.0
        for measurement in measurements
    ])
    summary['generation_phases'] = generation_phases
    summary['generation_phase_coverage_percent'] = generation_phase_coverage
    return {
        'schema_version': 3,
        'created_at': datetime.now(timezone.utc).isoformat(),
        'workbook': workbook_path,
        'workbook_size_bytes': os.path.getsize(workbook_path),
        'output_format': output_format,
        'runs': runs,
        'warmup_runs': warmup,
        'memory_runs': 1,
        'timing_traced': False,
        'environment': {
            'python': platform.python_version(),
            'implementation': platform.python_implementation(),
            'platform': platform.platform(),
            'processor': platform.processor(),
        },
        'measurements': measurements,
        'memory_measurement': {
            name: memory_measurement[name] for name in memory_metrics
        },
        'summary': summary,
        'stable': summary['total_seconds']['variation_percent'] <= 15.0,
    }


def _build_parser():
    parser = argparse.ArgumentParser(
        description='Create a repeatable Tableau-to-Power-BI performance baseline.',
    )
    parser.add_argument('workbook', help='Path to a .twb or .twbx workbook')
    parser.add_argument('--runs', type=int, default=3, help='Measured runs (default: 3)')
    parser.add_argument('--warmup', type=int, default=1, help='Warm-up runs (default: 1)')
    parser.add_argument(
        '--format', choices=('pbip', 'fabric'), default='pbip',
        help='Generation format to benchmark (default: pbip)',
    )
    parser.add_argument(
        '--verbose', action='store_true',
        help='Show migration logs during measured runs',
    )
    parser.add_argument(
        '--output', default='artifacts/performance/baseline.json',
        help='JSON output path',
    )
    return parser


def main(argv=None):
    args = _build_parser().parse_args(argv)
    baseline = run_baseline(
        args.workbook,
        runs=args.runs,
        warmup=args.warmup,
        output_format=args.format,
        verbose=args.verbose,
    )
    output_path = os.path.abspath(args.output)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as handle:
        json.dump(baseline, handle, indent=2)
        handle.write('\n')

    total = baseline['summary']['total_seconds']
    peak = baseline['summary']['peak_mb']
    stability = 'stable' if baseline['stable'] else 'variable'
    print(
        f"{os.path.basename(args.workbook)}: median {total['median']:.3f}s, "
        f"p95 {total['p95']:.3f}s, peak {peak['max']:.1f} MB ({stability})"
    )
    print(f'Baseline written to: {output_path}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
