# Shared agent runtime

Skillery 0.5.88 requires Skillkit 0.6.2 for guarded updates, managed copy projections, explicit Codex homes and DeepSeek Harness targets.

Use `skillery config --projection copy` on a Windows device whose remote agents cannot read junctions. An individual installation can override the profile with `--projection auto|link|copy`. A managed copy retains its mode on subsequent updates. Local payload edits, missing baselines and conflicting directories cause a refusal; force does not bypass these protections.

Local installation needs no account:

```sh
skillery install example --path ./skills/example --agent deepseek --projection copy --scope project --project ./project
```

DeepSeek installs into `.dsh/skills` in the selected project or into `DSH_HOME/skills` globally. Codex honors an explicit absolute `CODEX_HOME` for global skills and configuration. Each operating system keeps its own executable runtimes and account configuration; copy projections exclude another environment's virtual environments, package sources and local state.

A skipped update reports its reason and conflict paths and preserves existing metadata, runtime state and project registration. Unknown legacy copies need a reviewed immutable prior release manifest before migration; current bytes are not an acceptable replacement baseline.

The release candidate is tested against an isolated SDK 0.6.2 artifact. Its public dependency lock remains pending SDK publication. The canonical consumer gate records the verified candidate wheel and its own hashed dependency graph; it does not certify the previous public lock as current. Regenerate and check the public lock after SDK publication and before releasing this CLI.

Tests isolate HOME, APPDATA, local state and kernel locks. Native autostart commands and detached daemon spawning are blocked by default; dedicated tests must supply explicit mocks. Test runs do not register real scheduler entries.
