"""Canonical rendering and explicit source identity guards."""
from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
import json
import subprocess

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts/owned_skill_source.py'
spec = importlib.util.spec_from_file_location('owned_source', SCRIPT)
source = importlib.util.module_from_spec(spec)
spec.loader.exec_module(source)


def example():
    raw = {'SKILL.md': b'---\nname: tool\ndescription: A tool\n---\n\nClaude OAuth and claude-print are product identifiers.\n',
           '_skill_meta.toml': b'description = "A tool"\nversion = "1.0.0"\nkind = "comprehensive"\n'}
    current = dict(raw)
    current['SKILL.md'] = b'---\nname: tool\ndescription: A tool\nversion: 0.1.1\nfiles:\n- LICENSE-INTERNAL.txt\n- SKILL.md\n- _skill_meta.toml\n---\n\nClaude OAuth and claude-print are product identifiers.\n'
    current['_skill_meta.toml'] = b'description = "A tool"\nversion = "0.1.1"\nkind = "comprehensive"\n'
    current['LICENSE-INTERNAL.txt'] = b'Private internal use; no new third-party rights granted.\n'
    origin = {'source_leaf_files': [{'path': n, 'sha256': hashlib.sha256(b).hexdigest()} for n, b in raw.items()]}
    return raw, current, origin


def test_unchanged_source_keeps_identical_distribution_bytes():
    raw, current, origin = example()
    assert source.render(raw, current, origin) == current


def test_changed_instruction_body_preserves_product_names_and_license():
    raw, current, origin = example()
    raw['SKILL.md'] += b'Use the existing Windows CLI bridge in WSL.\n'
    result = source.render(raw, current, origin)
    assert source.entry(result['SKILL.md'])[1] == source.entry(raw['SKILL.md'])[1]
    assert b'Claude OAuth and claude-print' in result['SKILL.md']
    assert b'Codex OAuth' not in result['SKILL.md']
    assert result['LICENSE-INTERNAL.txt'] == current['LICENSE-INTERNAL.txt']


def test_changed_skill_identity_is_not_rebound():
    raw, current, origin = example()
    raw['SKILL.md'] = raw['SKILL.md'].replace(b'name: tool', b'name: other')
    with pytest.raises(ValueError, match='identity changed'):
        source.render(raw, current, origin)


def test_new_crew_user_bin_examples_remain_portable():
    raw, current, origin = example()
    for files in (raw, current):
        files['SKILL.md'] = files['SKILL.md'].replace(b'name: tool', b'name: crew')
    raw['references/cli.md'] = b'C:/Users/testuser/.local/bin/crew.exe and C:/Users/example/.local/bin/crew.exe\n'
    rendered = source.render(raw, current, origin)
    assert rendered['references/cli.md'] == b'%USERPROFILE%/.local/bin/crew.exe and %USERPROFILE%/.local/bin/crew.exe\n'


@pytest.mark.parametrize('value', ['../outside', '/outside', r'a\b', 'C:/Users/test', ''])
def test_export_paths_remain_bounded(value):
    with pytest.raises(ValueError):
        source.relative(value)


@pytest.mark.parametrize('value', ['https://user:secret@gitlab.com/group/repo.git', 'https://gitlab.com/group/repo.git?token=secret', 'file:///tmp/repo', 'ssh://git@other.example/repo'])
def test_source_url_never_introduces_credentials_or_foreign_hosts(value):
    with pytest.raises(ValueError):
        source.clean_url(value)


def test_clean_owned_ssh_transport_is_supported():
    value = 'ssh://git@altssh.gitlab.com:443/owner/project.git'
    assert source.clean_url(value) == value


def test_real_git_export_preserves_dirty_source_branches_and_tags(tmp_path, monkeypatch):
    def git(root, *args):
        return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.DEVNULL).decode().strip()

    def initialize(root):
        root.mkdir()
        git(root, 'init', '-b', 'main')
        git(root, 'config', 'user.name', 'Source Export Test')
        git(root, 'config', 'user.email', 'source-export@example.invalid')

    product = tmp_path/'product'
    mirror = tmp_path/'mirror'
    product_remote = tmp_path/'product.git'
    mirror_remote = tmp_path/'mirror.git'
    product_remote.mkdir(); mirror_remote.mkdir()
    git(product_remote, 'init', '--bare'); git(mirror_remote, 'init', '--bare')
    initialize(product); initialize(mirror)
    raw, current, origin = example()
    for name, content in raw.items():
        p=product/'skills/tool'/name; p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(content)
    for name, content in current.items():
        p=mirror/'skills/tool'/name; p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(content)
    (mirror/'SOURCE-ORIGIN.json').write_text(json.dumps(origin))
    for root in (product, mirror):
        git(root, 'add', '.'); git(root, 'commit', '-m', 'Initial owned fixture')
    git(product, 'push', str(product_remote), 'HEAD:refs/heads/main')
    git(mirror, 'tag', '-a', 'v0.1.1', '-m', 'Immutable baseline')
    git(mirror, 'push', str(mirror_remote), 'HEAD:refs/heads/main', 'refs/tags/v0.1.1')
    tags_before=git(mirror, 'ls-remote', str(mirror_remote), 'refs/tags/*')
    skill=product/'skills/tool/SKILL.md'
    skill.write_bytes(skill.read_bytes()+b'Updated source instruction.\n')
    git(product, 'add', '.'); git(product, 'commit', '-m', 'Real upstream instruction update')
    git(product, 'push', str(product_remote), 'HEAD:refs/heads/main')
    git(product, 'checkout', '-b', 'active-feature')
    skill.write_bytes(skill.read_bytes()+b'Uncommitted user work must remain.\n')
    branches_before=git(product, 'for-each-ref', '--format=%(refname) %(objectname)', 'refs/heads')
    dirty_before=skill.read_bytes()
    source_url='ssh://git@altssh.gitlab.com:443/owner/product.git'
    distribution_url='ssh://git@altssh.gitlab.com:443/owner/distribution.git'
    git(product, 'remote', 'add', 'origin', source_url)
    git(mirror, 'remote', 'add', 'skillery', distribution_url)
    original=source.Git.run
    def local_transport(self, *args):
        values=list(args)
        if values[0] in {'ls-remote','push'}:
            values[1]=str(product_remote if self.root==product else mirror_remote)
        elif values[0]=='fetch':
            values[values.index('origin')]=str(product_remote)
        return original(self, *values)
    monkeypatch.setattr(source.Git, 'run', local_transport)
    policy={'schema':1,'owned':True,'slug':'tool-fleet','source_checkout':str(product),
            'source_url':source_url,'source_ref':'main','source_path':'skills/tool',
            'distribution_checkout':str(mirror),'distribution_url':distribution_url,
            'distribution_branch':'main','distribution_path':'skills/tool'}
    report=source.export(policy,publish=True,scan_root=tmp_path/'owned-scan')
    assert report['published'] and 'SKILL.md' in report['changed_paths']
    assert skill.read_bytes()==dirty_before
    assert git(product,'branch','--show-current')=='active-feature'
    assert git(product,'for-each-ref','--format=%(refname) %(objectname)','refs/heads')==branches_before
    assert git(mirror,'ls-remote',str(mirror_remote),'refs/tags/*')==tags_before
    delivered=(mirror/'skills/tool/SKILL.md').read_bytes()
    assert b'Updated source instruction.' in delivered and b'Uncommitted user work' not in delivered
    assert b'Claude OAuth and claude-print' in delivered
    assert not (tmp_path/'owned-scan').exists()
