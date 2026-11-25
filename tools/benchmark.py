"""Measure Nook runtime smoke and durable queue throughput on disposable data."""
import argparse
import json
from pathlib import Path
import platform
import resource
import statistics
import subprocess
import sys
import tempfile
import time

import job_scheduler


def cgroup_memory_limit():
    try:
        for line in Path('/proc/self/cgroup').read_text().splitlines():
            if line.startswith('0::'):
                group = line[3:].lstrip('/')
                value = (Path('/sys/fs/cgroup') / group / 'memory.max').read_text().strip()
                return None if value == 'max' else int(value)
    except (OSError, ValueError):
        pass
    return None


def percentiles(samples):
    ordered = sorted(samples)
    return {'median_ms': round(statistics.median(ordered) * 1000, 3),
            'p95_ms': round(ordered[(95 * len(ordered) - 1) // 100] * 1000, 3)}


def runtime_sample(runs):
    elapsed = []
    for _ in range(runs):
        start = time.perf_counter()
        subprocess.run([sys.executable, '-m', 'tools.smoke_runtime'], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        elapsed.append(time.perf_counter() - start)
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return {**percentiles(elapsed), 'runs': runs, 'peak_rss_kib': usage.ru_maxrss,
            'cpu_seconds_total': round(usage.ru_utime + usage.ru_stime, 3)}


def queue_sample(jobs_per_batch, minimum_seconds):
    samples = []
    jobs_processed = 0
    with tempfile.TemporaryDirectory(prefix='nook-bench-') as directory:
        queue = Path(directory) / 'spool' / 'jobs.json'
        start, cpu = time.perf_counter(), time.process_time()
        deadline = start + minimum_seconds
        while not samples or time.perf_counter() < deadline:
            now = time.time()
            jobs = [{'id': f'q{i}', 'at': now, 'prompt': 'benchmark fixture'} for i in range(jobs_per_batch)]
            job_scheduler.modify_jobs(queue, lambda current: (current + jobs, None))
            remaining = len(jobs)
            while remaining:
                claim_start = time.perf_counter()
                claimed = job_scheduler.take_due(queue, now=now)
                if not claimed:
                    raise RuntimeError('Benchmark queue stopped making progress.')
                for job in claimed:
                    if not job_scheduler.complete(queue, job['claim_id']):
                        raise RuntimeError('Benchmark completion was refused.')
                samples.append((time.perf_counter() - claim_start) / len(claimed))
                remaining -= len(claimed)
                jobs_processed += len(claimed)
        elapsed = time.perf_counter() - start
        return {**percentiles(samples), 'processed': jobs_processed, 'seconds': round(elapsed, 3),
                'jobs_per_second': round(jobs_processed / elapsed, 2),
                'cpu_seconds': round(time.process_time() - cpu, 3),
                'peak_rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--startup-runs', type=int, default=3)
    parser.add_argument('--batch-size', type=int, default=64)
    parser.add_argument('--seconds', type=int, default=15, help='minimum seconds to sustain queue load')
    args = parser.parse_args()
    if not 1 <= args.startup_runs <= 20 or not 1 <= args.batch_size <= 1000 or not 1 <= args.seconds <= 600:
        parser.error('Use 1–20 startups, 1–1000 jobs per batch and 1–600 seconds.')
    print(json.dumps({
        'kernel': platform.release(),
        'python': platform.python_version(),
        'cgroup_memory_limit_bytes': cgroup_memory_limit(),
        'startup_and_three_charts': runtime_sample(args.startup_runs),
        'queue_only': queue_sample(args.batch_size, args.seconds),
    }, indent=2))


if __name__ == '__main__':
    main()
