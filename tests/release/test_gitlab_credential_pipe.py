"""Run the template's actual shell command through Git's credential protocol."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="GitLab Linux runner shell regression")

ROOT=Path(__file__).resolve().parents[2]

def test_template_configures_executable_scoped_credential_helper(tmp_path):
    project=tmp_path/'runner project with spaces'
    scripts=project/'.release-tooling/scripts';scripts.mkdir(parents=True)
    shutil.copyfile(ROOT/'scripts/gitlab_job_credential.py',scripts/'gitlab_job_credential.py')
    environment={**os.environ,'CI_PROJECT_DIR':str(project),'CI_PROJECT_PATH':'owner/skills-hub/skill','CI_SERVER_HOST':'gitlab.com','CI_JOB_TOKEN':'synthetic-test-only','GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':os.devnull,'GIT_TERMINAL_PROMPT':'0','PATH':str(Path(sys.executable).parent)+os.pathsep+'/usr/bin:/bin'}
    git='/usr/bin/git'
    subprocess.run([git,'init','-q'],cwd=project,env=environment,check=True,timeout=5)
    subprocess.run([git,'config','credential.helper',''],cwd=project,env=environment,check=True,timeout=5)
    subprocess.run([git,'config','credential.useHttpPath','true'],cwd=project,env=environment,check=True,timeout=5)
    text=(ROOT/'ci/owned-skill-release.gitlab.yml').read_text()
    command=next(line.strip()[2:] for line in text.splitlines() if line.strip().startswith('- git config --local --add credential.helper'))
    subprocess.run(['/bin/sh','-c',command],cwd=project,env=environment,check=True,timeout=5)
    response=subprocess.run([git,'credential','fill'],cwd=project,env=environment,input='protocol=https\nhost=gitlab.com\npath=owner/skills-hub/skill.git\n\n',capture_output=True,text=True,timeout=5)
    assert response.returncode==0,response.stderr
    fields=dict(line.split('=',1) for line in response.stdout.splitlines() if '=' in line)
    assert fields['username']=='gitlab-ci-token' and fields['password']=='synthetic-test-only'


def test_template_keeps_only_owned_remote_clean_under_runner_url_rewrite(tmp_path):
    project = tmp_path / "runner checkout"
    project.mkdir()
    owned = "https://gitlab.com/owner/skills-hub/skill.git"
    foreign = "https://gitlab.com/foreign/other.git"
    environment = {**os.environ, "CI_PROJECT_DIR": str(project), "CI_PROJECT_PATH": "owner/skills-hub/skill", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "PATH": str(Path(sys.executable).parent) + os.pathsep + "/usr/bin:/bin"}

    def git(*arguments):
        return subprocess.run(["/usr/bin/git", *arguments], cwd=project, env=environment, check=True, capture_output=True, text=True, timeout=5).stdout.strip()

    git("init", "-q")
    git("remote", "add", "origin", owned)
    git("remote", "add", "foreign", foreign)
    runner_prefix = "https://gitlab-ci-token:synthetic-test-only@gitlab.com/"
    git("config", "url." + runner_prefix + ".insteadOf", "https://gitlab.com/")
    assert git("remote", "get-url", "origin") != owned
    template = (ROOT / "ci/owned-skill-release.gitlab.yml").read_text()
    for line in template.splitlines():
        if line.strip().startswith("- git config --local "):
            subprocess.run(["/bin/sh", "-c", line.strip()[2:]], cwd=project, env=environment, check=True, timeout=5)
    assert git("remote", "get-url", "origin") == owned
    assert git("remote", "get-url", "foreign") == runner_prefix + "foreign/other.git"
