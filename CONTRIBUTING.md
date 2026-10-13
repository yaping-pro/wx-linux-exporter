# Contributing

Thanks for your interest. This repo is the **pipeline / consumer / ops** side of a two-part system. Please read the boundary rules below before opening a PR.

## Source-of-truth boundary (important)

The **device-side source is NOT in this repository.**

The WeChat pad/tablet Xposed module, the `wx_daemon` Go source, and the phone
`deploy/` scripts live in the sibling repo **[`yaping-pro/wx-pad-exporter`](https://github.com/yaping-pro/wx-pad-exporter)**
(alongside **[`yaping-pro/wx-tablet-mode`](https://github.com/yaping-pro/wx-tablet-mode)** for the Android module itself).

So:

- **Do not** open pull requests here that add or modify the WeChat Xposed module, the `wx_daemon` Go source, or the phone `deploy/` scripts. Those belong to **`yaping-pro/wx-pad-exporter`** and must be changed there first.
- This repo (**`yaping-pro/wx-linux-exporter`**) is for: the consumer clients (`clients/`), the homelab decrypt/serve pipeline (`bin/`), ops/deploy units (`deploy/`), the sing-box config, self-check tooling (`scripts/`), and documentation about the pipeline.
- If you fix something that originates in the device source, open the change against **`yaping-pro/wx-pad-exporter`** and, if needed, mirror the resulting documentation into `docs/` here.

## Commit message style

Follow the existing convention — read `git log` for live examples. In short:

- Conventional-Commit prefix scoped to the area: `feat(consumer):`, `fix(device):`, `docs(oss):`, `fix(deploy):`, etc.
- Imperative subject, lowercase first word, no trailing period.
- Body explains *why*, not just *what*, when the change is non-obvious.

## Before you open a PR

- Keep `.env`, `*.pem`, `*.key`, `config.local.json`, `*.log`, and `.wx_export_state.json` out of the diff (see `.gitignore`). Use placeholders, never real values.
- Do not commit chat content or credentials — the `.gitignore` already blocks `*_dump.json`, `WX_CONTENT_*`, `*.xlog`.
- Run `bash scripts/selfcheck.sh` if your change affects the link it exercises.

## Code of conduct

Be respectful. Keep discussion focused on the engineering of *exporting your own data from your own account*.
