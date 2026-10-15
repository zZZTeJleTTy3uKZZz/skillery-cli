#!/usr/bin/env python3
"""Provide only this GitLab job's ephemeral credential to Git's private pipe."""
from __future__ import annotations

import os
import sys


def credential(fields: dict[str, str], environment: dict[str, str]) -> str:
    """Refuse another provider/project instead of exporting a reusable token."""
    expected = environment.get('CI_PROJECT_PATH', '')
    if (fields.get('protocol') != 'https' or fields.get('host') != 'gitlab.com'
            or fields.get('path', '').removesuffix('.git') != expected
            or environment.get('CI_SERVER_HOST') != 'gitlab.com'
            or not expected or not environment.get('CI_JOB_TOKEN')):
        raise ValueError('credential request does not belong to this job project')
    return 'username=gitlab-ci-token\npassword=' + environment['CI_JOB_TOKEN'] + '\n\n'


def main(argv: list[str]) -> int:
    if argv != ['get'] or sys.stdout.isatty():
        return 0
    body = sys.stdin.read(8193)
    if len(body) > 8192:
        return 1
    fields = dict(line.split('=', 1) for line in body.splitlines() if '=' in line)
    try:
        # Stdout is Git's credential pipe, never a release log or receipt.
        sys.stdout.write(credential(fields, dict(os.environ)))
        return 0
    except ValueError:
        return 1


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
