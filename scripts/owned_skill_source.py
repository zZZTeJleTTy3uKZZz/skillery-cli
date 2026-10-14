#!/usr/bin/env python3
"""Export explicitly owned product skill leaves without checking out source refs."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tomllib
from urllib.parse import urlsplit

import yaml

MAX_FILE = 1024 * 1024
MAX_TOTAL = 4 * 1024 * 1024


def relative(value: str) -> str:
    p = PurePosixPath(value)
    if not value or p.as_posix() == '.' or p.is_absolute() or '..' in p.parts or '\\' in value or ':' in value:
        raise ValueError('unbounded skill path')
    return p.as_posix()


def clean_url(value: str) -> str:
    p = urlsplit(value)
    if p.password or p.query or p.fragment:
        raise ValueError('credential-bearing or ambiguous remote')
    if p.scheme == 'ssh' and p.hostname == 'altssh.gitlab.com' and p.username == 'git' and p.port == 443:
        return value
    if p.scheme == 'https' and p.hostname in {'github.com', 'gitlab.com'} and not p.username:
        return value
    raise ValueError('unsupported source remote')


def validate(item: dict) -> dict:
    if item.get('owned') is not True or item.get('schema') != 1:
        raise ValueError('explicit owned source export required')
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]*', item.get('slug', '')):
        raise ValueError('invalid skill identity')
    for key in ('source_ref', 'distribution_branch'):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]*', item.get(key, '')) or '..' in item[key]:
            raise ValueError('invalid selected branch')
    for key in ('source_path', 'distribution_path'):
        relative(item[key])
    for key in ('source_url', 'distribution_url'):
        clean_url(item[key])
    for key in ('source_checkout', 'distribution_checkout'):
        if not Path(item[key]).is_absolute():
            raise ValueError('explicit absolute checkout required')
    return item


class Git:
    def __init__(self, root: Path, executable: str = 'git'):
        self.root = root
        self.executable = executable

    def run(self, *args: str) -> bytes:
        env = os.environ.copy()
        env.update(GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='never')
        p = subprocess.run([self.executable, '-C', str(self.root), *args],
                           capture_output=True, timeout=45, env=env)
        if len(p.stdout) > MAX_TOTAL or len(p.stderr) > 65536:
            raise ValueError('Git output cap exceeded')
        if p.returncode:
            raise ValueError('source export Git operation refused: ' + args[0])
        return p.stdout

    def text(self, *args: str) -> str:
        return self.run(*args).decode('utf-8').strip()

    def blobs(self, commit: str, path: str) -> dict[str, bytes]:
        files = {}
        for row in self.run('ls-tree', '-r', '-z', commit, '--', path).split(b'\0'):
            if not row:
                continue
            info, raw = row.split(b'\t', 1)
            mode, kind, oid = info.decode().split()
            if kind != 'blob' or mode not in {'100644', '100755'}:
                raise ValueError('non-file source payload')
            name = relative(raw.decode().removeprefix(path + '/'))
            if any(part.lower() in {'.git', '.env', '.venv', 'auth', 'sessions', '.secrets'} for part in PurePosixPath(name).parts):
                raise ValueError('account state cannot be exported')
            size = int(self.text('cat-file', '-s', oid))
            if size > MAX_FILE or len(files) >= 64 or sum(map(len, files.values())) + size > MAX_TOTAL:
                raise ValueError('source payload exceeds bounds')
            files[name] = self.run('cat-file', 'blob', oid)
        return files


def entry(body: bytes) -> tuple[dict, bytes]:
    match = re.fullmatch(rb'---\r?\n(.*?)\r?\n---(\r?\n.*)', body, re.S)
    if not match:
        raise ValueError('invalid skill frontmatter')
    front = yaml.safe_load(match[1])
    if not isinstance(front, dict) or not front.get('name') or not front.get('description'):
        raise ValueError('missing skill identity')
    return front, match[2]


def render(source: dict[str, bytes], current: dict[str, bytes], provenance: dict) -> dict[str, bytes]:
    """Preserve instructions and bounded original tooling declarations as documentation."""
    front, body = entry(source['SKILL.md'])
    previous, _ = entry(current['SKILL.md'])
    if front['name'] != previous['name']:
        raise ValueError('source skill identity changed')
    result = dict(source)
    original_meta = source.get('_skill_meta.toml', b'')
    baseline = {x['path']: x['sha256'] for x in provenance['source_leaf_files']}
    version = previous['version']
    if hashlib.sha256(original_meta).hexdigest() == baseline.get('_skill_meta.toml'):
        result['_skill_meta.toml'] = current['_skill_meta.toml']
        if 'references/source-tooling.toml' in current:
            result['references/source-tooling.toml'] = current['references/source-tooling.toml']
    elif original_meta:
        metadata = original_meta.decode()
        data = tomllib.loads(metadata)
        metadata, count = re.subn(r'(?m)^version\s*=\s*"[^"]+"', 'version = ' + json.dumps(version), metadata, count=1)
        if not count:
            raise ValueError('explicit top-level source metadata version required')
        if data.get('cli') or data.get('mcp') or data.get('runtime_dependencies'):
            reference = re.sub(r'(?m)^version\s*=\s*"[^"]+"', 'version = "0.1.0"', metadata, count=1)
            result['references/source-tooling.toml'] = reference.encode()
            adapted = tomllib.loads(current['_skill_meta.toml'].decode())
            adapted['description'] = data.get('description', str(front['description']).strip())
            # The distribution remains documentation; native artifacts are separate.
            lines = ['description = ' + json.dumps(adapted['description'], ensure_ascii=False),
                     'version = ' + json.dumps(version), 'kind = "comprehensive"',
                     'tags = ' + json.dumps(data.get('tags', []), ensure_ascii=False)]
            onboarding = adapted.get('onboarding', {})
            if onboarding:
                lines += ['', '[onboarding]'] + [k + ' = ' + json.dumps(v, ensure_ascii=False) for k, v in onboarding.items()]
            result['_skill_meta.toml'] = ('\n'.join(lines) + '\n').encode()
        else:
            metadata = re.sub(r'(?m)^kind\s*=\s*"(?:tooling|knowledge)"', 'kind = "comprehensive"', metadata)
            result['_skill_meta.toml'] = metadata.encode()
    else:
        result['_skill_meta.toml'] = current['_skill_meta.toml']
    # Preserve reviewed private attribution notices; do not invent new license rights.
    for name, content in current.items():
        if name.upper().startswith('LICENSE') or name.upper() == 'COPYING':
            result.setdefault(name, content)
    if front['name'] == 'crew' and 'references/cli.md' in result:
        result['references/cli.md'] = result['references/cli.md'].replace(b'C:/Users/79288/bin/crew.exe', b'%USERPROFILE%/bin/crew.exe')
    front['version'] = version
    front['files'] = sorted(result)
    result['SKILL.md'] = b'---\n' + yaml.safe_dump(front, allow_unicode=True, sort_keys=False).encode() + b'---' + body
    return result


def export(item: dict, *, publish: bool = False, git_executable: str = 'git', scan_root: Path | None = None) -> dict:
    item = validate(item)
    source = Git(Path(item['source_checkout']), git_executable)
    mirror = Git(Path(item['distribution_checkout']), git_executable)
    if source.text('remote', 'get-url', 'origin') != item['source_url']:
        raise ValueError('source remote identity differs')
    if mirror.text('remote', 'get-url', 'skillery') != item['distribution_url']:
        raise ValueError('distribution remote identity differs')
    if mirror.text('branch', '--show-current') != item['distribution_branch'] or mirror.text('status', '--porcelain'):
        raise ValueError('distribution is dirty or on another branch')
    head = mirror.text('rev-parse', 'HEAD')
    remote_head = mirror.text('ls-remote', 'skillery', 'refs/heads/' + item['distribution_branch'])
    if not remote_head or remote_head.split()[0] != head:
        raise ValueError('distribution diverged from the owned remote')
    selected = source.text('ls-remote', 'origin', 'refs/heads/' + item['source_ref'])
    if not selected or not re.fullmatch(r'[a-f0-9]{40}', selected.split()[0]):
        raise ValueError('selected source branch is not published')
    commit = selected.split()[0]
    # Store objects only: no FETCH_HEAD, local branch, tracking ref or checkout changes.
    source.run('fetch', '--no-tags', '--no-write-fetch-head', 'origin', commit)
    raw = source.blobs(commit, item['source_path'])
    source_leaf = dict(raw)
    root_names = source.text('ls-tree', '--name-only', commit).splitlines()
    for name in root_names:
        if name.upper() in {'LICENSE', 'LICENSE.MD', 'LICENSE.TXT', 'COPYING'}:
            content = source.run('show', commit + ':' + name)
            if len(content) > MAX_FILE:
                raise ValueError('source license exceeds bounds')
            raw.setdefault(name, content)
    current = mirror.blobs(head, item['distribution_path'])
    provenance = json.loads(mirror.run('show', head + ':SOURCE-ORIGIN.json'))
    payload = render(raw, current, provenance)
    changed = sorted(k for k in set(payload) | set(current) if payload.get(k) != current.get(k))
    result = {'slug': item['slug'], 'source_commit': commit, 'before': head, 'changed_paths': changed, 'published': False}
    if not changed or not publish:
        return result
    from skillgate import run_gate
    if scan_root is None or not scan_root.parent.is_dir() or scan_root.exists():
        raise ValueError('publish requires a new owned bounded scan directory')
    if scan_root.parent.is_symlink() or mirror.root.is_symlink():
        raise ValueError('linked export roots refused')
    receipt = scan_root.with_suffix('.receipt.json')
    if receipt.exists():
        raise ValueError('previous scan requires reconciliation')
    total = sum(map(len, payload.values()))
    if total > MAX_TOTAL:
        raise ValueError('rendered payload exceeds bounds')
    record = {'owner': 'skillery-source-export', 'slug': item['slug'], 'source_commit': commit,
              'paths': [str(scan_root)], 'max_bytes': MAX_TOTAL, 'state': 'reserved-before-scan'}
    receipt.write_text(json.dumps(record, indent=2) + '\n')
    scan_root.mkdir()
    try:
        skill_name = relative(str(entry(payload['SKILL.md'])[0]['name']))
        if '/' in skill_name:
            raise ValueError('skill name is not one directory')
        scan_leaf = scan_root / 'skills' / skill_name
        for name, content in payload.items():
            path = scan_leaf / relative(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        if run_gate(scan_leaf).verdict == 'fail':
            raise ValueError('source blocked by canonical scanner; mirror preserved')
        record['state'] = 'terminal-scanned'
    except Exception:
        record['state'] = 'terminal-failed'
        raise
    finally:
        receipt.write_text(json.dumps(record, indent=2) + '\n')
        # Exactly this newly created data-only tree; no auth, source or shared caches.
        shutil.rmtree(scan_root)
    # The mirror itself is the durable owned source; preserve unrelated paths.
    root = mirror.root / item['distribution_path']
    for name in set(current) - set(payload):
        (root / relative(name)).unlink()
    for name, content in payload.items():
        path = root / relative(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    provenance.update(source_commit=commit, source_ref=item['source_ref'], source_leaf_files=[{'path': n, 'sha256': hashlib.sha256(b).hexdigest(), 'size': len(b)} for n, b in sorted(source_leaf.items())])
    (mirror.root / 'SOURCE-ORIGIN.json').write_text(json.dumps(provenance, indent=2) + '\n')
    mirror.run('add', '--', item['distribution_path'], 'SOURCE-ORIGIN.json')
    mirror.run('commit', '-m', 'Sync ' + item['slug'] + ' from source ' + commit[:12])
    result['commit'] = mirror.text('rev-parse', 'HEAD')
    mirror.run('push', 'skillery', result['commit'] + ':refs/heads/' + item['distribution_branch'])
    result['published'] = True
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--policy', type=Path, required=True)
    parser.add_argument('--publish', action='store_true')
    parser.add_argument('--scan-parent', type=Path)
    args = parser.parse_args(argv)
    data = json.loads(args.policy.read_text(encoding='utf-8'))
    if data.get('schema') != 1 or not 1 <= len(data.get('sources', [])) <= 8:
        raise ValueError('explicit bounded source selection required')
    for item in data['sources']:
        import uuid
        scan = args.scan_parent / (item['slug'] + '-' + uuid.uuid4().hex) if args.scan_parent else None
        print(json.dumps(export(item, publish=args.publish, scan_root=scan)), flush=True)


if __name__ == '__main__':
    main()
