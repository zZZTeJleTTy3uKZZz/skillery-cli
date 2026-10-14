#!/usr/bin/env python3
"""Release one owned bounded skill from a dedicated distribution repository.

Only Git blobs define payload bytes. Application/package repositories must use
their existing package release pipeline instead of sharing the SemVer namespace
with automatic skill patch tags. No Git ref is ever moved or force-pushed.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse
from pathlib import Path, PurePosixPath

HOOK_URL = 'https://api.skillery.ru/webhooks/git/github'
SEMVER = re.compile(r'^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$')


def _relative(value: str) -> str:
    path = PurePosixPath(value)
    if (not value or '\\' in value or path.is_absolute() or ':' in value
            or '..' in path.parts or path.as_posix() in {'.', ''}):
        raise ValueError('release paths must be bounded relative paths')
    return path.as_posix()


def validate_policy(data: dict, repository: str) -> dict:
    if (data.get('schema') != 1 or data.get('owned') is not True
            or data.get('repository_role') != 'skill-distribution'):
        raise ValueError('only explicitly owned dedicated skill repositories may auto-release')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('invalid repository identity')
    if data.get('repository', '').lower() != repository.lower():
        raise ValueError('release policy repository identity mismatch')
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]*', data.get('slug', '')):
        raise ValueError('invalid skill identity')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]*', data.get('branch', '')):
        raise ValueError('invalid release branch')
    skill_path = _relative(data['skill_path'])
    payload = [_relative(item) for item in data.get('payload_paths') or []]
    licenses = [_relative(item) for item in data.get('license_paths') or []]
    if not payload or skill_path not in payload or not licenses:
        raise ValueError('skill path and explicit license paths must be declared')
    for license_path in licenses:
        if not license_path.startswith(skill_path + '/'):
            raise ValueError('license attribution must be inside the installed skill path')
        if not any(license_path == item or license_path.startswith(item + '/') for item in payload):
            raise ValueError('license attribution must be included in the bounded payload')
    if (not SEMVER.fullmatch(data.get('initial_version', ''))
            or data['initial_version'].startswith('v')):
        raise ValueError('initial version must be immutable plain SemVer')
    return {**data, 'skill_path': skill_path, 'payload_paths': payload, 'license_paths': licenses}


def next_version(tags: list[str], initial: str) -> str:
    versions = [tuple(map(int, match.groups())) for tag in tags if (match := SEMVER.fullmatch(tag))]
    if not versions:
        return initial
    major, minor, patch = max(versions)
    return f'{major}.{minor}.{patch + 1}'


def inventory_digest(files: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for name, body in sorted(files.items()):
        digest.update(name.encode('utf-8') + b'\0' + hashlib.sha256(body).digest())
    return digest.hexdigest()


def validate_inventory(policy: dict, files: dict[str, bytes]) -> None:
    expected = policy['skill_path'] + '/SKILL.md'
    if [path for path in files if PurePosixPath(path).name == 'SKILL.md'] != [expected]:
        raise ValueError('one bounded skill per release repository is required')
    if any(not files.get(path) for path in policy['license_paths']):
        raise ValueError('declared license attribution is missing or empty')
    for path in files:
        name = PurePosixPath(path).name.lower()
        if (name == '.env' or name.startswith('.env.') or name.endswith(('.pem', '.key'))
                or any(part in {'.git', '.venv', 'auth', 'sessions', '.secrets'}
                       for part in PurePosixPath(path).parts)):
            raise ValueError('runtime or credential paths cannot be released')


def preflight_environment(environment: dict) -> None:
    if environment.get('SKILLERY_SAFE_UPDATES_READY') != 'true':
        raise ValueError('devices have not been accepted on the safe update runtime')
    if not environment.get('SKILLERY_GIT_WEBHOOK_SECRET'):
        raise ValueError('registered skill git-hook secret is required before creating any tag')


def _canonical_gate(path: Path):
    """Use the same pinned secret/denylist engine as Skillery publish."""
    if importlib.metadata.version('s-skillgate') != '0.1.7':
        raise ValueError('pinned content publish gate runtime is unavailable')
    from skillgate import run_gate
    return run_gate(path, manifest=False)


def scan_then_publish(files: dict[str, bytes], directory: Path, tag_write, hook_delivery):
    """Materialize exact blobs, scan fail-closed, then allow ref/hook mutations."""
    receipt_path = directory.with_name(directory.name + '.receipt.json')
    if directory.exists() or receipt_path.exists() or not directory.parent.is_dir():
        raise ValueError('publish scan needs a new owned directory and receipt')
    receipt = {'owner': 'skillery-release', 'run': os.environ.get('GITHUB_RUN_ID', 'local'),
               'session': os.environ.get('GITHUB_RUN_ATTEMPT', '1'),
               'max_bytes': 64 * 1024 * 1024, 'scanner': 's-skillgate==0.1.7',
               'payload_sha256': inventory_digest(files), 'paths': [str(directory)],
               'expiry_epoch': int(time.time()) + 86400, 'state': 'prepared'}
    receipt_path.write_text(json.dumps(receipt, sort_keys=True) + '\n')
    directory.mkdir()
    try:
        if sum(len(body) for body in files.values()) > receipt['max_bytes']:
            raise ValueError('payload publish gate data cap exceeded')
        for name, body in files.items():
            path = directory / _relative(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        report = _canonical_gate(directory)
        if report.verdict not in {'ok', 'warn', 'fail'}:
            raise ValueError('payload publish gate returned an unverifiable verdict')
        receipt['gate_verdict'] = report.verdict
        if report.verdict == 'fail':
            raise ValueError('payload publish gate blocked content')
        # Never print finding contents; scanners may include matched secrets.
        receipt['state'] = 'terminal-scanned'
        receipt_path.write_text(json.dumps(receipt, sort_keys=True) + '\n')
        tag_write()
        result = hook_delivery()
        receipt.update({'state': 'terminal-delivered', 'exit_status': 0})
        return result
    except Exception:
        receipt.update({'state': 'terminal-failed', 'exit_status': 1})
        raise ValueError('payload publish gate or immutable delivery failed; no findings printed') from None
    finally:
        receipt_path.write_text(json.dumps(receipt, sort_keys=True) + '\n')
        if receipt['state'].startswith('terminal'):
            shutil.rmtree(directory)
            receipt['cleanup'] = 'exact owned blob materialization removed'
            receipt_path.write_text(json.dumps(receipt, sort_keys=True) + '\n')


def webhook_request(repository: str, tag: str, commit: str, secret: str) -> tuple[bytes, dict]:
    if not secret or not SEMVER.fullmatch(tag) or not re.fullmatch(r'[a-f0-9]{40,64}', commit):
        raise ValueError('invalid immutable release event or missing hook secret')
    body = json.dumps({'ref': 'refs/tags/' + tag, 'after': commit,
                       'repository': {'clone_url': 'https://github.com/' + repository + '.git'}},
                      separators=(',', ':')).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return body, {'Content-Type': 'application/json', 'X-GitHub-Event': 'push',
                  'X-Hub-Signature-256': 'sha256=' + signature}


def validate_ack(status: int, body: bytes) -> list[int]:
    if status != 202:
        raise ValueError('hub did not accept the git-hook event')
    jobs = json.loads(body).get('job_ids')
    if not isinstance(jobs, list) or not jobs or any(type(item) is not int for item in jobs):
        raise ValueError('hub accepted no authenticated skill sync jobs')
    return jobs


class Repository:
    def __init__(self, root: Path):
        self.root = root

    def git(self, *args: str) -> bytes:
        result = subprocess.run(['git', *args], cwd=self.root, stdin=subprocess.DEVNULL,
                                capture_output=True, timeout=60, check=False)
        if result.returncode != 0:
            raise ValueError('git release operation failed; refs were not forced')
        return result.stdout

    def blobs(self, ref: str, paths: list[str]) -> dict[str, bytes]:
        inventory = {}
        tree = self.git('ls-tree', '-r', '-z', ref, '--', *paths)
        for row in tree.split(b'\0'):
            if not row:
                continue
            identity, path = row.split(b'\t', 1)
            mode, kind, oid = identity.decode().split()
            name = path.decode('utf-8')
            if kind != 'blob' or mode not in {'100644', '100755'}:
                raise ValueError('linked or foreign payload trees cannot be released')
            size = int(self.git('cat-file', '-s', oid))
            if size > 16 * 1024 * 1024 or sum(len(body) for body in inventory.values()) + size > 64 * 1024 * 1024:
                raise ValueError('bounded release payload exceeds its finite data cap')
            inventory[name] = self.git('cat-file', 'blob', oid)
        return inventory

    def attributes(self, paths: list[str]) -> None:
        values = self.git('check-attr', '--cached', '-z', 'eol', 'filter',
                          'working-tree-encoding', '--', *paths).split(b'\0')
        for offset in range(0, len(values) - 2, 3):
            _path, attribute, value = values[offset:offset + 3]
            if ((attribute == b'eol' and value == b'crlf')
                    or (attribute in {b'filter', b'working-tree-encoding'}
                        and value not in {b'unspecified', b'unset'})):
                raise ValueError('payload checkout attributes change canonical Git blob bytes')

    def plan(self, policy: dict) -> dict:
        commit = self.git('rev-parse', 'HEAD^{commit}').decode().strip()
        files = self.blobs(commit, policy['payload_paths'])
        validate_inventory(policy, files)
        self.attributes(list(files))
        digest = inventory_digest(files)
        tags = [tag for tag in self.git('tag', '--list').decode().splitlines() if SEMVER.fullmatch(tag)]
        latest = max(tags, key=lambda tag: tuple(map(int, SEMVER.fullmatch(tag).groups()))) if tags else None
        if latest is not None:
            previous = self.git('rev-parse', latest + '^{commit}').decode().strip()
            self.git('merge-base', '--is-ancestor', previous, commit)
            if inventory_digest(self.blobs(latest, policy['payload_paths'])) == digest:
                return {'tag': latest, 'commit': previous, 'payload_sha256': digest, 'reused': True}
        return {'tag': 'v' + next_version(tags, policy['initial_version']), 'commit': commit,
                'payload_sha256': digest, 'reused': False}

    def publish(self, policy: dict, plan: dict) -> None:
        remote = self.git('remote', 'get-url', 'origin').decode().strip()
        if remote.startswith('git@github.com:'):
            identity = remote[len('git@github.com:'):].removesuffix('.git')
        else:
            parsed = urlparse(remote)
            if (parsed.scheme != 'https' or parsed.hostname != 'github.com'
                    or parsed.username or parsed.password or parsed.query or parsed.fragment):
                raise ValueError('release remote is not the owned GitHub repository')
            identity = parsed.path.lstrip('/').removesuffix('.git')
        if identity.lower() != policy['repository'].lower():
            raise ValueError('release remote repository identity mismatch')
        current = self.git('ls-remote', 'origin', 'refs/heads/' + policy['branch']).decode().split()
        head = self.git('rev-parse', 'HEAD^{commit}').decode().strip()
        if not current or current[0] != head:
            raise ValueError('source branch advanced; retry against its current committed tip')
        existing = self.git('ls-remote', 'origin', 'refs/tags/' + plan['tag'],
                            'refs/tags/' + plan['tag'] + '^{}').decode().splitlines()
        if existing:
            resolved = dict(line.split('\t', 1)[::-1] for line in existing)
            actual = resolved.get('refs/tags/' + plan['tag'] + '^{}') or resolved['refs/tags/' + plan['tag']]
            if actual != plan['commit']:
                raise ValueError('immutable remote tag already points to different content')
            return
        if not plan['reused']:
            message = json.dumps({'schema': 'skillery.release.v1', 'skill': policy['slug'], **plan},
                                 sort_keys=True)
            self.git('-c', 'user.name=Skillery release', '-c', 'user.email=skillery-release@users.noreply.github.com',
                     'tag', '-a', plan['tag'], plan['commit'], '-m', message)
        self.git('push', 'origin', 'refs/tags/' + plan['tag'] + ':refs/tags/' + plan['tag'])


def deliver(body: bytes, headers: dict) -> list[int]:
    for attempt in range(3):
        request = urllib.request.Request(HOOK_URL, data=body, headers=headers, method='POST')
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return validate_ack(response.status, response.read(65537))
        except urllib.error.HTTPError as error:
            if error.code not in {429, 500, 502, 503, 504} or attempt == 2:
                raise ValueError('hub delivery rejected; immutable tag retained for retry') from None
        except (TimeoutError, urllib.error.URLError):
            if attempt == 2:
                raise ValueError('hub delivery unavailable; immutable tag retained for retry') from None
        time.sleep(2 ** attempt)
    raise ValueError('hub delivery incomplete')


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--policy', required=True)
    parser.add_argument('--repository', required=True)
    parser.add_argument('--publish', action='store_true')
    parser.add_argument('--scan-root', type=Path)
    args = parser.parse_args(argv)
    try:
        repository = Repository(Path.cwd())
        config_path = _relative(args.policy)
        config = repository.blobs('HEAD', [config_path]).get(config_path)
        if config is None:
            raise ValueError('release policy must be committed before use')
        policy = validate_policy(json.loads(config), args.repository)
        if os.environ.get('GITHUB_REF_NAME', policy['branch']) != policy['branch']:
            raise ValueError('event is not on the declared release branch')
        plan = repository.plan(policy)
        if args.publish:
            preflight_environment(os.environ)
            if args.scan_root is None:
                raise ValueError('publish needs an explicit bounded owned scan directory')
            body, headers = webhook_request(policy['repository'], plan['tag'], plan['commit'],
                                            os.environ['SKILLERY_GIT_WEBHOOK_SECRET'])
            files = repository.blobs(plan['commit'], policy['payload_paths'])
            if inventory_digest(files) != plan['payload_sha256']:
                raise ValueError('immutable payload changed before its publish gate')
            plan['job_ids'] = scan_then_publish(
                files, args.scan_root, lambda: repository.publish(policy, plan),
                lambda: deliver(body, headers),
            )
        print(json.dumps({'skill': policy['slug'], 'published': args.publish, **plan}, sort_keys=True))
        return 0
    except (ValueError, OSError, json.JSONDecodeError, subprocess.TimeoutExpired):
        print('owned skill release refused; check source/policy, safe-runtime acceptance and hook setup',
              file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
