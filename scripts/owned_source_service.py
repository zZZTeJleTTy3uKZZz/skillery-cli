"""Bounded Windows scheduler caller for the owned skill-source exporter."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

OWNER = 'skillery-owned-source-service-v1'


def read(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 262144:
        raise ValueError('bounded regular service metadata required')
    return json.loads(path.read_text(encoding='utf-8'))


def save(path, data):
    body = (json.dumps(data, indent=2) + '\n').encode()
    if len(body) > 262144:
        raise ValueError('service metadata cap exceeded')
    temp = path.with_suffix('.next')
    with temp.open('xb') as stream:
        stream.write(body)
    os.replace(temp, path)


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate(policy):
    if policy.get('owner') != OWNER or policy.get('schema') != 1:
        raise ValueError('foreign source service')
    root = Path(policy['root'])
    if not root.is_dir() or root.is_symlink() or not 1 <= len(policy.get('sources', [])) <= 8:
        raise ValueError('invalid source service roots or selection')
    for name, sha in policy['source_files'].items():
        if '/' in name or '\\' in name or hashlib.sha256((root/name).read_bytes()).hexdigest() != sha:
            raise ValueError('source generation changed')
    return root


def worker(policy, result_path):
    root = validate(policy)
    exporter = load('owned_exporter', root/'owned_skill_source.py')
    state_path = root/'source-state.json'
    state = read(state_path) if state_path.exists() else {'owner': OWNER, 'sources': {}}
    if state.get('owner') != OWNER:
        raise ValueError('foreign source cache')
    rows = []
    for item in policy['sources']:
        try:
            source = exporter.Git(Path(item['source_checkout']))
            mirror = exporter.Git(Path(item['distribution_checkout']))
            ref = source.text('ls-remote', 'origin', 'refs/heads/'+item['source_ref'])
            if not ref:
                raise ValueError('source branch missing')
            commit = ref.split()[0]
            prior = state['sources'].get(item['slug'], {})
            head = mirror.text('rev-parse', 'HEAD')
            remote = mirror.text('ls-remote', 'skillery', 'refs/heads/'+item['distribution_branch'])
            if (prior.get('source_commit') == commit and prior.get('distribution_commit') == head
                    and remote and remote.split()[0] == head and not mirror.text('status', '--porcelain')):
                rows.append({'slug': item['slug'], 'source_commit': commit, 'state': 'unchanged-cached-source'})
                continue
            import uuid
            result = exporter.export(item, publish=policy['publish'], scan_root=root/'scans'/(item['slug']+'-'+uuid.uuid4().hex))
            rows.append({**result, 'state': 'source-export-accepted'})
            if not result['changed_paths'] or result['published']:
                state['sources'][item['slug']] = {'source_commit': result['source_commit'], 'distribution_commit': result.get('commit', result['before'])}
                save(state_path, state)
        except Exception as error:
            rows.append({'slug': item['slug'], 'state': 'source-refused', 'error_type': type(error).__name__})
        save(result_path, {'owner': OWNER, 'rows': rows})
    save(result_path, {'owner': OWNER, 'rows': rows})
    return 1 if any(x['state']=='source-refused' for x in rows) else 0


def run(policy, policy_path):
    root = validate(policy)
    ledger_path = root/'service-ledger.json'
    ledger = read(ledger_path) if ledger_path.exists() else {'owner': OWNER, 'sequence': 0, 'records': []}
    if ledger.get('owner') != OWNER or len(ledger['records']) > 64:
        raise ValueError('foreign service ledger')
    for name in ledger['records']:
        prior = read(root/name)
        if prior.get('owner') != OWNER or prior.get('state') != 'terminal' or not prior.get('physical_pid0'):
            raise ValueError('previous source run needs reconciliation')
    if len(ledger['records']) == 64:
        name = ledger['records'][0]
        old = read(root/name)
        number = old['sequence']
        if name != 'run-'+str(number)+'.json':
            raise ValueError('foreign run identity')
        log = root/('log-'+str(number)+'.txt')
        if log.exists() and hashlib.sha256(log.read_bytes()).hexdigest() != old['log_sha256']:
            raise ValueError('owned log changed')
        result = root/('result-'+str(number)+'.json')
        if result.exists() and read(result).get('owner') != OWNER:
            raise ValueError('foreign source result')
        ledger['records'] = ledger['records'][1:]
        save(ledger_path,ledger)
        for path in (root/name,log,result):
            if path.exists():path.unlink()
    sequence = ledger['sequence']+1
    receipt_path = root/('run-'+str(sequence)+'.json')
    result_path = root/('result-'+str(sequence)+'.json')
    if receipt_path.exists() or result_path.exists():
        raise ValueError('ambiguous run identity')
    record = {'owner': OWNER, 'task': policy['task'], 'session': policy['session'], 'sequence': sequence,
              'state': 'reserved-before-owned-child', 'deadline_seconds': 300,
              'memory_bytes': 536870912, 'cpu': 2, 'pid_limit': 128,
              'temporary_cap_bytes': 16777216, 'log_cap_bytes': 65536, 'expiry_seconds': 86400}
    save(receipt_path, record)
    ledger.update(sequence=sequence, records=ledger['records']+[receipt_path.name]); save(ledger_path, ledger)
    jobs = load('bounded_windows_job', root/'fleet-windows-job.py')
    if jobs.available_memory() < 1073741824:
        record.update(state='terminal', original_exit_status=1, physical_pid0=True, reason='memory-gate'); save(receipt_path, record); return 1
    child = subprocess.Popen([policy['python'], '-B', str(root/'owned_source_service.py'), '--policy', str(policy_path), '--worker-result', str(result_path)],
                             cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             creationflags=0x4|0x08000000)
    job = None; body = bytearray(); stop = threading.Event(); errors=[]; start=time.monotonic()
    try:
        job = jobs.capped_source_job(jobs.OwnedJob, 536870912)(child)
        record.update(state='running', pid=child.pid, kernel_caps=job.caps); save(receipt_path, record)
        def capture():
            while chunk := child.stdout.read1(8192):
                if len(body)+len(chunk)>65536:
                    errors.append('log-cap');job.stop();return
                body.extend(chunk)
        def deadline():
            if not stop.wait(300):errors.append('deadline');job.stop()
        reader=threading.Thread(target=capture,daemon=True);timer=threading.Thread(target=deadline,daemon=True)
        reader.start();timer.start();child.wait()
    finally:
        stop.set()
        if job:
            if child.poll() is None:job.stop();child.wait(timeout=10)
            zero=job.reap();peak=job.peak_memory();job.close()
        else:
            child.kill();child.wait(timeout=10);zero=True;peak=None
        if 'reader' in locals():reader.join(timeout=5)
        if 'timer' in locals():timer.join(timeout=5)
        log=root/('log-'+str(sequence)+'.txt');log.write_bytes(body)
        record.update(state='terminal',original_exit_status=child.returncode,physical_pid0=zero,
                      peak_job_memory_bytes=peak,elapsed_seconds=round(time.monotonic()-start,3),events=errors,log_sha256=hashlib.sha256(body).hexdigest())
        save(receipt_path,record)
    print(json.dumps(record))
    return child.returncode if not errors else 1


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--policy',type=Path,required=True)
    parser.add_argument('--worker-result',type=Path)
    args=parser.parse_args();policy=read(args.policy)
    raise SystemExit(worker(policy,args.worker_result) if args.worker_result else run(policy,args.policy))
