# Throughline starter plugins

The connectors Throughline's onboarding offers by default: messages, email,
calendars, meetings, location, health, media, notes, AI assistants and more.

Each directory is one plugin (a Python package built on `throughline-sdk`).
`throughline-plugins.toml` describes them for onboarding: what each imports,
the credentials it needs, how to back fill history. See the plugin
onboarding contract, `docs/plugin-onboarding-contract.md` in the Throughline
repository.

Add your own repository of plugins the same way: put a
`throughline-plugins.toml` (several plugins) or `throughline-plugin.toml` (one)
at its root, and paste its GitHub link into Throughline's onboarding or
Settings → Plugins.
