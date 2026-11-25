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

## Pull request workflow

`main` is a protected branch. Nobody pushes to it directly — not contributors, not the repo owner. Every change lands through a pull request that passes the required checks.

**Branch naming.** There is no pre-existing feature-branch convention here (only Dependabot's own branches exist), so use `<type>/<short-slug>`, where `<type>` is the same Conventional Commits type you will use in the commit subject: `docs/pr-workflow`, `fix/health-freshness`, `feat/media-enrichment`. Keep the slug lowercase, hyphenated, and short.

**Commands, in order:**

```bash
git checkout main && git pull          # start from the latest main
git checkout -b docs/pr-workflow       # create your branch
# ... edit, then:
git add CONTRIBUTING.md
git commit -m "docs(contributing): add the PR workflow"
git push -u origin docs/pr-workflow    # never push to main
gh pr create --base main \
  --title "docs(contributing): add the PR workflow" \
  --body "..."
gh pr checks --watch                   # wait for all five required checks
gh pr merge --squash --delete-branch   # squash-merge, then delete the branch
git checkout main && git pull          # resync locally
```

**The five required checks.** A PR to `main` must pass exactly these, and they are triggered by `pull_request` in `.github/workflows/ci.yml`:

- `lint-and-test (3.10)`
- `lint-and-test (3.11)`
- `lint-and-test (3.12)`
- `lint-and-test (3.13)`
- `docker-check`

If any of them is red, fix the cause in your branch. Do not edit the workflow to weaken or skip a check.

**Staying current.** `required_status_checks.strict = true`, so your PR must be based on the latest `main` before it can merge. If GitHub reports the branch is behind, merge `main` into your branch (`git checkout main && git pull && git checkout <branch> && git merge main`) and push — do not rebase a branch you have already pushed, and never force-push.

**No admin bypass.** Branch protection is the only thing that keeps an unreviewed, untested commit off `main`; using owner/admin rights to route around it defeats the entire check suite, so it is never acceptable here — if you see `Bypassed rule violations`, treat it as a failure, not a success.

## Code of conduct

Be respectful. Keep discussion focused on the engineering of *exporting your own data from your own account*.
