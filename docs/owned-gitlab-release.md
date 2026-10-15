# Owned GitLab skill releases

This route is for one explicitly owned bounded skill in a dedicated private
distribution repository. Product repositories keep their existing release
pipeline. A GitLab subgroup path is supported; upstream vendor skills do not
become owned by copying them here.

The source of future edits is this per-skill repository. A normal commit and push
to its declared branch runs the serialized release job. It scans exact committed
Git blobs with `s-skillgate==0.1.7`, creates or reuses an immutable SemVer tag, and
pushes the tag. GitLab then sends its real registered webhook to
`https://api.skillery.ru/webhooks/git/gitlab`. The helper never synthesizes a
GitHub HMAC event and does not need the hook secret in CI.

Before enabling a caller, the operator must have actual evidence for:

1. The installed safe runtime baseline on both devices and accepted copy/native
   projections. The initial baseline is Skillery CLI 0.5.88 and Skillkit 0.6.2.
2. A numeric Skillery skill created with the requested private access and bounded
   `skill_path`, with an initial immutable tag and canonical content gate.
3. `PUT /skills/{numeric_id}/webhook` returning `mode=auto`, the GitLab provider
   and an actual external hook id. Manual mode must not be treated as registered.
4. The target GitLab project's own job-token push permission. The service token
   stays in the service; CI uses only its automatically generated `CI_JOB_TOKEN`.
5. Available runner/compute budget and an approved pinned
   `python:3.13-alpine@sha256:<digest>` image. No image is pulled or CI enabled by
   adding this template.

Record the public identifiers in the committed `.skillery-release.json`, using
the existing policy fields plus `provider="gitlab"`, numeric `project_id`, and
the actual `native_hook_id`. Keep the license and attribution files physically
inside `skill_path`. The `runtime_baseline` object may explicitly declare the
accepted `cli_version` and `sdk_version`; the helper refuses versions below the
safe initial baseline.

Create a **nonsecret file-type GitLab project variable** named
`SKILLERY_NATIVE_RELEASE_READINESS` from the verified result, with this shape:

```json
{
  "schema": 1,
  "provider": "gitlab",
  "repository": "owner/skills-hub/skill",
  "project_id": 42,
  "fleet": {
    "cli_version": "0.5.88",
    "sdk_version": "0.6.2",
    "devices": ["DIMA", "office-laptop"],
    "copy_projection_accepted": true,
    "protected_native_accepted": true
  },
  "hook": {
    "mode": "auto",
    "status": "registered",
    "external_hook_id": "72",
    "callback_url": "https://api.skillery.ru/webhooks/git/gitlab"
  },
  "project": {
    "ci_push_repository_for_job_token_allowed": true
  }
}
```

The identifiers above are synthetic examples, not live registration evidence.
This records an already-tested immutable baseline rather than current device
availability. Passage of time alone does not expire it. A different runtime
baseline, project or registered hook requires matching reviewed evidence.
The managed `SKILLERY_SAFE_UPDATES_READY` flag remains the explicit hold/revoke
control; disabling it prevents tag writes.

The caller includes `ci/owned-skill-release.gitlab.yml` from an immutable reviewed
tooling commit, extends `.skillery-owned-gitlab-release`, and adds push/branch and
bounded-path `changes` rules. Declare a `release` stage in the caller. Set
`SKILLERY_TOOLING_REF` to that exact 40-character
commit, `SKILLERY_RELEASE_POLICY` to the committed policy path, and
`SKILLERY_RELEASE_IMAGE` to the reviewed pinned image. Keep the only release job
under the template's project resource group, five-minute timeout and
`interruptible=false`. The template is hidden and enables no caller by itself.

The caller shape is below. Replace the review placeholder with the actual
reviewed helper commit before committing any executable caller; all live
readiness and image values come from the operator's verified project variables.

```yaml
stages: [release]
include:
  - remote: https://raw.githubusercontent.com/zZZTeJleTTy3uKZZz/skillery-cli/REVIEWED_TOOLING_SHA/ci/owned-skill-release.gitlab.yml
release_skill:
  extends: .skillery-owned-gitlab-release
  variables:
    SKILLERY_TOOLING_REF: REVIEWED_TOOLING_SHA
    SKILLERY_RELEASE_POLICY: .skillery-release.json
  rules:
    - if: '$CI_PIPELINE_SOURCE == "push" && $CI_COMMIT_BRANCH == "main"'
      changes: [skills/skill/**/*, .skillery-release.json]
```

The credential helper validates HTTPS, `gitlab.com`, and the exact job project
path, and supplies the ephemeral job credential only to Git's credential pipe.
It refuses foreign projects and hosts. Do not run it as a normal command to
display credentials; it writes no credential files or receipts.

A successful job reports `tag-pushed-awaiting-provider-job`. This is not final
acceptance. Confirm the real GitLab webhook delivery, a nonempty authenticated
Skillery sync-job acknowledgement, the completed job's `new_versions` result,
the exact version manifest/snapshot, and raw content on the two device and
protected copy/native projections. Job-token pushes do not create another
GitLab CI pipeline, so webhook delivery must be tested directly; a green tag job
alone cannot prove it. See the [GitLab job-token push contract](https://docs.gitlab.com/ci/jobs/ci_job_token/#allow-git-push-requests-to-your-project-repository).

A retry that finds the same remote immutable tag reports
`immutable-tag-already-present-provider-delivery-unverified`. It does not claim a
new provider event was emitted. Preserve the tag; diagnose or retry the actual
provider delivery instead of moving the ref or synthesizing another provider's
authentication scheme.
