"""Owned releases bind raw committed payloads, immutable tags and hub delivery."""
import hashlib
import hmac
import importlib.util
import json
import sys
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts' / 'owned_skill_release.py'


@pytest.fixture
def release():
    spec = importlib.util.spec_from_file_location('owned_skill_release', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def policy(**changes):
    data = {'schema': 1, 'owned': True, 'repository_role': 'skill-distribution',
            'repository': 'owner/skill', 'branch': 'main', 'slug': 'skill',
            'skill_path': 'skills/skill', 'payload_paths': ['skills/skill'],
            'license_paths': ['skills/skill/LICENSE'], 'initial_version': '0.1.0'}
    data.update(changes)
    return data


def test_policy_requires_owned_distribution_repo_and_bounded_paths(release):
    assert release.validate_policy(policy(), 'owner/skill')['slug'] == 'skill'
    for change in [{'owned': False}, {'repository_role': 'application'},
                   {'payload_paths': ['.']}, {'payload_paths': ['../foreign']},
                   {'repository': 'foreign/repo'}, {'license_paths': []}]:
        with pytest.raises(ValueError):
            release.validate_policy(policy(**change), 'owner/skill')


def test_semver_is_monotonic_and_retry_reuses_exact_payload(release):
    assert release.next_version([], '0.1.0') == '0.1.0'
    assert release.next_version(['v0.9.9', 'v1.0.0', 'v9.0.0-beta'], '0.1.0') == '1.0.1'


def test_raw_committed_inventory_ignores_working_tree_eol(release, tmp_path):
    (tmp_path / 'SKILL.md').write_bytes(b'LINE\r\n')
    first = release.inventory_digest({'SKILL.md': b'LINE\n'})
    assert first != release.inventory_digest({'SKILL.md': (tmp_path / 'SKILL.md').read_bytes()})
    assert first == release.inventory_digest({'SKILL.md': b'LINE\n'})


def test_hmac_uses_existing_git_webhook_raw_body_contract(release):
    body, headers = release.webhook_request('owner/skill', 'v0.1.1', 'a' * 40, 'fixture-secret')
    assert json.loads(body)['ref'] == 'refs/tags/v0.1.1'
    assert json.loads(body)['repository']['clone_url'] == 'https://github.com/owner/skill.git'
    assert headers['X-Hub-Signature-256'] == 'sha256=' + hmac.new(
        b'fixture-secret', body, hashlib.sha256).hexdigest()
    with pytest.raises(ValueError):
        release.webhook_request('owner/skill', 'v0.1.1', 'a' * 40, '')


def test_secret_and_safe_device_preflight_precedes_tag_mutation(release):
    for environment in [{}, {'SKILLERY_GIT_WEBHOOK_SECRET': 'fixture'},
                        {'SKILLERY_SAFE_UPDATES_READY': 'true'}]:
        with pytest.raises(ValueError):
            release.preflight_environment(environment)
    release.preflight_environment({'SKILLERY_GIT_WEBHOOK_SECRET': 'fixture',
                                  'SKILLERY_SAFE_UPDATES_READY': 'true'})


def test_delivery_requires_real_nonempty_accepted_job_ack(release):
    assert release.validate_ack(202, b'{"job_ids":[42]}') == [42]
    for status, body in [(200, b'{"job_ids":[42]}'), (202, b'{"job_ids":[]}'),
                         (401, b'{}'), (202, b'{"job_ids":["bad"]}')]:
        with pytest.raises(ValueError):
            release.validate_ack(status, body)


def test_payload_refuses_extra_skills_and_secret_paths(release):
    valid = {'skills/skill/SKILL.md': b'---\nname: skill\n---\n', 'skills/skill/LICENSE': b'MIT\n'}
    release.validate_inventory(policy(), valid)
    for path in ['skills/skill/.env', 'skills/skill/vendor/SKILL.md', 'skills/skill/token.pem']:
        with pytest.raises(ValueError):
            release.validate_inventory(policy(), {**valid, path: b'not-publishable'})


def test_real_git_payload_is_idempotent_under_windows_autocrlf(release, tmp_path):
    def git(*args):
        return subprocess.run(['git', *args], cwd=tmp_path, check=True, capture_output=True).stdout

    git('init')
    git('config', 'core.autocrlf', 'true')
    git('config', 'user.name', 'Fixture')
    git('config', 'user.email', 'fixture@example.invalid')
    (tmp_path / 'skills' / 'skill').mkdir(parents=True)
    skill = tmp_path / 'skills' / 'skill' / 'SKILL.md'
    skill.write_bytes(b'---\nname: skill\n---\nCanonical payload\n')
    (tmp_path / 'skills' / 'skill' / 'LICENSE').write_bytes(b'MIT fixture\n')
    git('add', 'skills')
    git('-c', 'core.hooksPath=/dev/null', 'commit', '-m', 'fixture payload')
    repository = release.Repository(tmp_path)
    first = repository.plan(policy())
    git('tag', first['tag'])
    skill.write_bytes(skill.read_bytes().replace(b'\n', b'\r\n'))
    second = repository.plan(policy())
    assert second['reused'] and second['tag'] == first['tag']
    assert second['payload_sha256'] == first['payload_sha256']
    (tmp_path / '.gitattributes').write_bytes(b'*.md eol=crlf\n')
    git('add', '.gitattributes')
    git('-c', 'core.hooksPath=/dev/null', 'commit', '-m', 'fixture bad attributes')
    with pytest.raises(ValueError, match='canonical Git blob'):
        repository.plan(policy())


def test_existing_remote_tag_cannot_be_retargeted(release, tmp_path):
    calls = []

    class Repository(release.Repository):
        def git(self, *args):
            calls.append(args)
            if args[:2] == ('remote', 'get-url'):
                return b'https://github.com/owner/skill.git\n'
            if args[0] == 'rev-parse':
                return b'a' * 40 + b'\n'
            if args[0] == 'ls-remote' and args[2].startswith('refs/heads/'):
                return b'a' * 40 + b'\trefs/heads/main\n'
            if args[0] == 'ls-remote':
                return b'b' * 40 + b'\trefs/tags/v0.1.0\n'
            raise AssertionError('unexpected mutation')

    with pytest.raises(ValueError, match='different content'):
        Repository(tmp_path).publish(policy(), {'tag': 'v0.1.0', 'commit': 'a' * 40, 'reused': False})
    assert not any(command[0] in {'tag', 'push'} for command in calls)


def test_real_publish_gate_blocks_injected_secret_before_tag_or_hook(release, tmp_path):
    files = {'skills/skill/SKILL.md': b'---\nname: skill\n---\n', 'skills/skill/LICENSE': b'MIT\n',
             'skills/skill/config.py': b'token = "ghp_' + b'A' * 36 + b'"\n'}
    calls = []
    with pytest.raises(ValueError, match='publish gate'):
        release.scan_then_publish(files, tmp_path / 'scan', lambda: calls.append('tag'),
                                  lambda: calls.append('hook'))
    assert calls == []
    assert not (tmp_path / 'scan').exists()
    receipt = json.loads((tmp_path / 'scan.receipt.json').read_text())
    assert receipt['state'] == 'terminal-failed'
    assert 'ghp_' not in json.dumps(receipt)


def test_unavailable_or_crashing_publish_gate_fails_closed(release, tmp_path, monkeypatch):
    calls = []

    def unavailable(path):
        raise ImportError('scanner unavailable')

    monkeypatch.setattr(release, '_canonical_gate', unavailable)
    with pytest.raises(ValueError, match='publish gate'):
        release.scan_then_publish({'SKILL.md': b'clean'}, tmp_path / 'scan',
                                  lambda: calls.append('tag'), lambda: calls.append('hook'))
    assert calls == []


def test_license_attribution_must_be_inside_installed_skill_payload(release):
    outside = policy(payload_paths=['skills/skill', 'LICENSE'], license_paths=['LICENSE'])
    with pytest.raises(ValueError, match='installed skill path'):
        release.validate_policy(outside, 'owner/skill')
    nested = policy(payload_paths=['skills/skill'], license_paths=['skills/skill/LICENSE'])
    assert release.validate_policy(nested, 'owner/skill')['license_paths'] == ['skills/skill/LICENSE']


def gitlab_policy():
    return policy(provider='gitlab', repository='owner/skills-hub/skill', project_id=42, native_hook_id='72')


def native_readiness():
    return {'schema': 1, 'provider': 'gitlab', 'repository': 'owner/skills-hub/skill',
            'project_id': 42, 'verified_at': 1000, 'expires_at': 1300,
            'fleet': {'cli_version': '0.5.88', 'sdk_version': '0.6.2',
                      'devices': ['DIMA', 'office-laptop'], 'copy_projection_accepted': True,
                      'protected_native_accepted': True},
            'hook': {'mode': 'auto', 'status': 'registered', 'external_hook_id': '72',
                     'callback_url': 'https://api.skillery.ru/webhooks/git/gitlab'},
            'project': {'ci_push_repository_for_job_token_allowed': True}}


def test_gitlab_accepts_owned_nested_namespace_and_real_clean_remote_forms(release):
    configured = release.validate_policy(gitlab_policy(), 'owner/skills-hub/skill')
    for remote in ['https://gitlab.com/owner/skills-hub/skill.git',
                   'git@gitlab.com:owner/skills-hub/skill.git',
                   'ssh://git@altssh.gitlab.com:443/owner/skills-hub/skill.git']:
        release.validate_remote(remote, configured)
    for remote in ['https://token:private@gitlab.com/owner/skills-hub/skill.git',
                   'https://foreign.example/owner/skills-hub/skill.git',
                   'ssh://git@altssh.gitlab.com:444/owner/skills-hub/skill.git',
                   'git@gitlab.com:foreign/skills-hub/skill.git']:
        with pytest.raises(ValueError):
            release.validate_remote(remote, configured)


def test_native_hook_readiness_requires_current_actual_registration_and_fleet(release):
    configured = release.validate_policy(gitlab_policy(), 'owner/skills-hub/skill')
    assert release.validate_native_readiness(native_readiness(), configured) == 42
    invalid = [dict(native_readiness(), project_id=43),
               dict(native_readiness(), repository='foreign/skills-hub/skill'),
               dict(native_readiness(), hook={**native_readiness()['hook'], 'external_hook_id':'73'}),
               dict(native_readiness(), hook={'mode': 'manual'}),
               dict(native_readiness(), fleet={'sdk_version': '0.4.1'}),
               dict(native_readiness(), project={'ci_push_repository_for_job_token_allowed': False})]
    for receipt in invalid:
        with pytest.raises(ValueError):
            release.validate_native_readiness(receipt, configured)


def test_gitlab_native_mode_does_not_construct_secret_delivery_request(release, monkeypatch):
    monkeypatch.setattr(release, 'webhook_request', lambda *args: pytest.fail('must use provider hook'))
    monkeypatch.setattr(release, 'deliver', lambda *args: pytest.fail('must use provider hook'))
    calls = []
    result = release.publish_native_tag(lambda: calls.append('tag'))
    assert calls == ['tag']
    assert result == {'delivery': 'provider-native', 'acceptance': 'tag-pushed-awaiting-provider-job'}


def test_native_retry_cannot_claim_an_existing_tag_emitted_a_new_provider_event(release):
    result = release.publish_native_tag(lambda: False)
    assert result['acceptance'] == 'immutable-tag-already-present-provider-delivery-unverified'


@pytest.mark.parametrize('injected_secret', [False, True])
def test_native_main_scans_exact_payload_before_tag_and_never_sends_fake_hook(
    release, tmp_path, monkeypatch, capsys, injected_secret
):
    configured = gitlab_policy()
    files = {'skills/skill/SKILL.md': b'---\nname: skill\n---\n',
             'skills/skill/LICENSE': b'MIT fixture\n'}
    if injected_secret:
        files['skills/skill/config.py'] = b'token = "ghp_' + b'A' * 36 + b'"\n'
    calls = []

    class Repository:
        def __init__(self, root):
            assert root == tmp_path

        def blobs(self, ref, paths):
            if paths == ['.skillery-release.json']:
                return {'.skillery-release.json': json.dumps(configured).encode()}
            assert paths == configured['payload_paths']
            return files

        def plan(self, selected):
            return {'tag': 'v0.1.0', 'commit': 'a' * 40,
                    'payload_sha256': release.inventory_digest(files), 'reused': False}

        def publish(self, selected, plan):
            calls.append('tag')

    monkeypatch.setattr(release, 'Repository', Repository)
    monkeypatch.setattr(release, 'deliver', lambda *args: pytest.fail('provider delivers native hook'))
    monkeypatch.setattr(release, 'webhook_request', lambda *args: pytest.fail('no synthetic HMAC'))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('SKILLERY_SAFE_UPDATES_READY', 'true')
    monkeypatch.setenv('CI_COMMIT_BRANCH', 'main')
    monkeypatch.setenv('CI_PROJECT_ID', '42')
    monkeypatch.delenv('SKILLERY_GIT_WEBHOOK_SECRET', raising=False)
    readiness = tmp_path / 'ready.json'
    readiness.write_text(json.dumps(native_readiness()))
    status = release.main(['--policy', '.skillery-release.json', '--repository', configured['repository'],
                           '--native-readiness', str(readiness), '--scan-root', str(tmp_path/'scan'),
                           '--publish'])
    assert status == (1 if injected_secret else 0)
    assert calls == ([] if injected_secret else ['tag'])
    output = capsys.readouterr()
    assert 'ghp_' not in output.out + output.err
    if not injected_secret:
        result = json.loads(output.out)
        assert result['acceptance'] == 'tag-pushed-awaiting-provider-job'
        assert 'job_ids' not in result


def test_job_credential_is_scoped_to_exact_own_project_and_provider():
    path = SCRIPT.with_name('gitlab_job_credential.py')
    spec = importlib.util.spec_from_file_location('gitlab_job_credential', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    environment = {'CI_SERVER_HOST': 'gitlab.com', 'CI_PROJECT_PATH': 'owner/skills-hub/skill',
                   'CI_JOB_TOKEN': 'synthetic-ephemeral'}
    fields = {'protocol': 'https', 'host': 'gitlab.com', 'path': 'owner/skills-hub/skill.git'}
    assert module.credential(fields, environment) == 'username=gitlab-ci-token\npassword=synthetic-ephemeral\n\n'
    for change in [{'host': 'foreign.example'}, {'protocol': 'http'}, {'path': 'foreign/skill.git'}]:
        with pytest.raises(ValueError):
            module.credential({**fields, **change}, environment)
