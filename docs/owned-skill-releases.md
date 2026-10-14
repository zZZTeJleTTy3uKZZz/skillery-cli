# Owned skill release delivery

The reusable workflow releases one owned skill from a dedicated distribution
repository. Application/PyPI repositories keep their package release pipeline;
automatic metadata tags must not consume their immutable package version names.
No caller is enabled in this change and no payload is published before fleet
acceptance on Skillkit 0.6.2 and Skillery CLI 0.5.88.

Commit a policy in each verified owned distribution repository:

```json
{
  "schema": 1,
  "owned": true,
  "repository_role": "skill-distribution",
  "repository": "OWNER/OWNED-SKILL",
  "branch": "main",
  "slug": "owned-skill",
  "skill_path": "skills/owned-skill",
  "payload_paths": ["skills/owned-skill", "LICENSE"],
  "license_paths": ["LICENSE"],
  "initial_version": "0.1.0"
}
```

Copy/add a caller with a push filter for exactly those paths and the policy.
Reference `.github/workflows/owned-skill-release.yml` at a reviewed immutable
commit SHA. Pass the accepted fleet state through the caller's
`vars.SKILLERY_SAFE_UPDATES_READY == 'true'` boolean. Map the secret
`SKILLERY_GIT_WEBHOOK_SECRET` from the registered skill git-hook. A missing
secret refuses before creating tags. This is not the `cli-published` API key.

```yaml
on:
  push:
    branches: [main]
    paths: [skills/owned-skill/**, LICENSE, .skillery-release.json]
jobs:
  skill:
    permissions:
      contents: write
    uses: zZZTeJleTTy3uKZZz/skillery-cli/.github/workflows/owned-skill-release.yml@REVIEWED_COMMIT_SHA
    with:
      policy: .skillery-release.json
      tooling_ref: REVIEWED_COMMIT_SHA
      runtime_ready: ${{ vars.SKILLERY_SAFE_UPDATES_READY == 'true' }}
    secrets:
      SKILLERY_GIT_WEBHOOK_SECRET: ${{ secrets.SKILLERY_GIT_WEBHOOK_SECRET }}
```

Use the same full 40-character SHA in `uses` and `tooling_ref`; the release
tool checkout is separate and cannot introduce files into the skill payload.

The workflow serializes release jobs and never force-pushes refs. The helper
loads the committed policy and hashes selected committed Git blobs with SHA256,
including license attribution. It refuses symlinks/submodules, extra skills,
credential/runtime paths and checkout attributes that change canonical bytes.
Before any tag write or hook delivery, exact committed blobs are materialized
in a new owned bounded scan directory and scanned with pinned `s-skillgate==0.1.7`
`run_gate(path, manifest=False)`, the engine used by Skillery's publish gate.
An unavailable runtime, exception, unverifiable verdict or blocking secret fails
closed. Finding contents are never printed. A durable receipt records the raw
payload SHA and sanitized outcome before removing exact owned scan data.
It increments the largest plain SemVer tag patch when payload changes and reuses
the exact previous tag when the payload is unchanged. It checks the current
remote branch tip and refuses a remote tag that points to different content.

Delivery uses the existing `POST /webhooks/git/github` contract:
`X-Hub-Signature-256 = sha256=HMAC(secret, raw JSON body)`, with repository clone
URL and `refs/tags/vX.Y.Z`. Only HTTP202 with nonempty integer `job_ids` is a
successful authenticated enqueue. A transient delivery failure retains the
immutable tag; rerunning redelivers the same version. Hub queued/running jobs
and imported immutable versions provide the existing downstream deduplication.
An enqueue is not proof of completed import or installation; acceptance must
check the final hub manifest SHA and safe device update results separately.

Obra superpowers and other vendor sources are excluded from owned release
policies. Use their pinned upstream source with its attribution and license.
Pure loose skills receive their own bounded canonical distribution source;
whole service projects and foreign capture/auth/runtime data are never payload.

## Existing catalog history

Current backend versions store commit SHA and manifest, but resolve installs
with the current skill-level repo URL and skill path. Snapshots are best-effort.
Do not repoint an existing skill to a new mirror unless all historical commit
objects/tags and the identical historical skill path remain fetchable there.
Otherwise use an explicit new slug migration/alias and preserve the old catalog
source for rollback and trusted old-manifest recovery. This workflow never
changes hub source bindings.
