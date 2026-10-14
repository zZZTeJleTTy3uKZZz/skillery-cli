# Managed agent scope updates

Configure the agents explicitly:

```sh
skillery config --sync-agents claude_code,codex,deepseek --projection copy
```

The `sync_agents` array is persisted in the active profile's configuration.
An omitted or empty array preserves the historical primary-agent behavior.
Supported canonical names are `claude_code`, `codex`, `antigravity`, `deepseek`,
and `opencode`. A successful explicit `install --all-agents` enrolls the
successfully installed agents in this set. An empty `--sync-agents` value resets
the explicit set. Configuration alone never creates an agent directory.

Successful requested project installs and enables register their absolute root
in profile-local `managed-scopes.json`. This atomic registry has a schema,
a maximum of 256 roots, and a 64 KiB limit. A missing registry is empty; malformed
or oversized state is refused and preserved. No repository discovery or recursive
filesystem search is performed. Existing project registrations are considered
only while their `.skillery/skills.toml` still declares the skill. Foreground
callers may also supply the current project explicitly.

Requested source updates, foreground automatic updates, and the daemon's latest
update pass reconcile existing managed global and registered project scopes.
Cooldown retries reconcile already materialized source changes without another
download. The automatic-update switch still disables background reconciliation.
Reconciliation calls the public SDK `link_existing` API, which verifies scope
ownership, its source binding, and both immutable byte baselines before replacing
a copy. Current verified copies incur no writes. Missing agent directories and
missing scope entries are never created by reconciliation; foreign paths and
unknown legacy baselines are never adopted. Runtime state remains in its scope.
Reconciliation does not run tooling installers, register MCP servers, or change
disabled MCP flags.

Source materialization and each agent copy are independent transactions. If the
source advances while a secondary copy has a local edit, the requested source
result remains successful and includes `partial: true` plus
`scope_reconciliation.refused` with the agent, skill, project and conflict reason.
The daemon exposes scoped results and logs refusals. Foreground commands warn on
stderr while keeping JSON stdout parseable. Other clean copies may still update.
After resolving the conflicting edit, a later reconciliation retries that copy.

A baseline-less legacy directory requires a verified pinned prior release
manifest/bundle or explicit manual review and export before replacement. `--force`
does not bypass this requirement or the local-edit gate.
