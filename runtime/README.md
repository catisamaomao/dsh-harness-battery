# Packaged assessment runtime

This directory contains the portable runtime and two unchanged frozen question packs.
Use the plugin UI or `bin/plugin_cli.py`; the frozen packs preserve the original research documentation and version references. They do not include historical model runs or the old research repository.

All generated batches, private memory snapshots and reports belong in the plugin's external data directory, never in this installed package. Real execution is macOS-only and requires a verified OS boundary. No tests or installation hooks call a model automatically.
