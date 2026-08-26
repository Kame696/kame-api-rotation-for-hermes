# KAME for Hermes — Publishing guide (GitHub + the Hermes plugin index)

> **This file is internal workspace documentation.** It lives in
> `04_plugins/kame/hermes/`, outside the published tree, and is never pushed.
>
> **Purpose.** Everything needed to put KAME 1.2.0 on GitHub and into the Hermes
> plugin index, written so it can be picked up cold. The sibling guide for the
> Agent Zero build is [`../agent-zero/PUBLISHING.md`](../agent-zero/PUBLISHING.md);
> the two hosts publish through completely different machinery, so read the one
> that matches what you are shipping.
>
> Last revised: 2026-08-23 — Code state: **1.2.0, LIVE.** The repository exists
> at `Kame696/kame-api-rotation-for-hermes`, `main` is at
> `f4e22346b262c2c8354b250177d9cefe1015824e`, and release `v1.2.0` is tagged
> (the tag still points at the earlier `70abb1b`; see the author-fix note below).
> **Sections 3 and 4 are both done.** Index PR **[NousResearch/hermes-agent#93257](https://github.com/NousResearch/hermes-agent/pull/93257)**
> is open, awaiting maintainer review.
>
> **2026-08-23, post-review fix:** the automated PR review on #93257 caught
> `plugin.yaml`'s `author:` field reading `"C0DEM4ST3R"` — a private working
> nickname (used only between the owner and the AI assistant in local
> workspace instructions) that leaked into a public manifest. Fixed to
> `"Kame696"` in commit `f4e2234`, tags also superset against the index entry's
> (`api, rotation, rate-limiting, reliability, keys` common; manifest keeps
> `quota, multi-key`). The index PR's `ref` was bumped to `f4e2234` in the same
> branch so the merged entry never pins the commit with the wrong author. The
> real deployed copy on the owner's machine was patched the same way via the
> UNC route (see `deploy-hermes-impossivel-daqui` in memory) — grep the whole
> repo for `C0DEM4ST3R` before any future publish to catch a repeat.
>
> **2026-08-24, the same leak found again, in the other spelling.** The grep
> above is spelled with an `E`; `LICENSE` at the repo root reads
> `Copyright (c) 2026 C0D3M4ST3R`, with a `3`, and is live in the public repo
> today. One check, one spelling, one file missed. Use a pattern that catches
> both and every case, and run it against the *repo*, not only the packaged
> zip — the zip does not contain `LICENSE`, so scanning the artifact says
> nothing about what is published:
>
> ```bash
> grep -rniE 'c0d[e3]m4st3r' .
> ```
>
> The sibling Agent Zero repo already carries the wording to copy:
> `Copyright (c) 2026 KAME (https://github.com/Kame696)`. Not yet fixed here —
> a copyright attribution is the owner's to decide, and pushing to a public
> repository needs their explicit go-ahead.
>
> **Correction to this file's earlier research:** `NousResearch/hermes-plugin-index`
> does **not exist** (404, confirmed both via the GitHub API and the raw content
> URL the CLI itself fetches). The real target is the bundled seed file at
> `hermes_cli/data/plugin_index.json` inside the **`NousResearch/hermes-agent`**
> monorepo — `plugin_index.py`'s `DEFAULT_INDEX_URL` points at the dead repo, so
> every install falls through the fetch failure straight to this seed file
> (`load_index()`'s remote → cache → seed chain). Section 4 below is kept as
> written for the mental model (fork → edit → PR is still correct), but every
> repo/path reference in it should read `NousResearch/hermes-agent` +
> `hermes_cli/data/plugin_index.json`, not a standalone index repo.

---

## 1. The map: two repositories, same as Agent Zero

### Repo A — the plugin CODE
- **On GitHub:** https://github.com/Kame696/kame-api-rotation-for-hermes — live.
- **On disk (source of truth):** `04_plugins/kame/hermes/`
- **On disk (staged for the push):** `C:\Users\davic\_kame_hermes_pub`
- **Contains:** `README.md`, `CHANGELOG.md`, `LICENSE`, `docs/internals.md`,
  `assets/`, `hermes-kame-api-rotation/` (the plugin itself), `tests/`, `tools/`.

Deliberately **not** published: `DESIGN.md` (internal, Portuguese),
`BLUEPRINT_1.0.8.md`, `PLAN_1.0.9.md`, `dist/`, `releases/`, and this file.

### Repo B — the INDEX (the storefront)
- **On GitHub:** `https://github.com/NousResearch/hermes-plugin-index` (owned by the Hermes maintainers)
- One file at the repo root: **`index.json`**
- We cannot push to it. The route is: fork → edit `index.json` → pull request.

### The mental model
> The index does not store the code. It stores one JSON object that **points at**
> Repo A. `hermes plugins install <name>` resolves the name through the index and
> then clones from Repo A.

---

## 2. Why the plugin lives in a subdirectory

The installable plugin is `hermes-kame-api-rotation/`, not the repo root. That is
deliberate and the index supports it through a `subdir` field.

The alternative — plugin files at the root — would force `tests/` (1416 of them),
`tools/` (the live harnesses) and `docs/` into every user's
`$HERMES_HOME/plugins/` directory. The subdir keeps the install to what actually
runs, while the repository keeps the evidence a reader needs to trust the README.

The install identifier is therefore `repo[/subdir]`:

```bash
hermes plugins install Kame696/kame-api-rotation-for-hermes/hermes-kame-api-rotation
```

`hermes plugins install` also accepts a full URL with `#subdir`, and GitHub
`tree/` URLs. Once the index entry is merged, the bare name works too:

```bash
hermes plugins install hermes-kame-api-rotation
```

---

## 3. Step 1 — create the code repository ✅ DONE (2026-08-23)

`gh` is installed and authenticated as **Kame696**. From anywhere:

```bash
gh repo create Kame696/kame-api-rotation-for-hermes --public --description "KAME API Rotation for Hermes - one API key per call, chosen for you. A failed call moves to the next key instead of ending your turn." --homepage "https://github.com/Kame696/kame-api-rotation-for-hermes"
```

Then push the staged tree (already committed, nothing to write):

```bash
git -C C:/Users/davic/_kame_hermes_pub remote add origin https://github.com/Kame696/kame-api-rotation-for-hermes.git
```

```bash
git -C C:/Users/davic/_kame_hermes_pub push -u origin main
```

Optional but recommended — a release tag, so the index entry can pin a commit
that has a human-readable name beside it:

```bash
gh release create v1.2.0 --repo Kame696/kame-api-rotation-for-hermes --title "KAME API Rotation for Hermes 1.2.0" --notes-file "C:/Users/davic/OneDrive/A0 - Dev/anti gravity/04_plugins/kame/hermes/CHANGELOG.md"
```

**Repository settings worth setting on the web page once:** topics
(`hermes`, `llm`, `api`, `rate-limiting`, `plugin`), and *Issues* enabled — the
plugin's own console output links there when something goes wrong.

---

## 4. Step 2 — the index entry

Fork `NousResearch/hermes-plugin-index`, edit `index.json` at the root, add this
object to the array, open a PR.

```json
{
  "name": "hermes-kame-api-rotation",
  "description": "One API key per call, chosen for you. A failed call moves to the next key instead of ending your turn, and a spent quota is waited out instead of given up on - with no provider allowlist anywhere in it.",
  "author": "Kame696",
  "tags": ["api", "rate-limiting", "reliability", "keys", "rotation"],
  "repo": "Kame696/kame-api-rotation-for-hermes",
  "ref": "70abb1baf472e3c209fa0d5abee94c29052a0923",
  "subdir": "hermes-kame-api-rotation",
  "homepage": "https://github.com/Kame696/kame-api-rotation-for-hermes",
  "capabilities": ["commands", "dashboard"],
  "api_version": 1,
  "added_at": "2026-08-23"
}
```

### The `ref` field — read this before filling it in

**`ref` pins the install only when it is an exact 40-character commit SHA.** A
tag name (`v1.2.0`) is accepted, but the installer prints an advisory and then
installs the **branch head** — which means the index would claim to pin a version
it does not pin.

The SHA above is the staged commit and will be the pushed one **as long as nothing
new is committed before the push**. Re-read it after pushing rather than trusting
this note:

```bash
git -C C:/Users/davic/_kame_hermes_pub rev-parse HEAD
```

Paste that value. Updating the plugin later means a new PR with a new SHA — that
is the trade for a reproducible install, and it is the right trade for something
that sits in the path of every model call.

### `capabilities` is not a permission request

Hermes gates seven things behind an explicit user grant (`tools.override`,
`llm.provider_override`, `llm.model_override`, `llm.agent_id_override`,
`llm.profile_override`, `llm.task_override`, `gateway.platform_actions`).
**KAME declares none of them**, and `plugin.yaml` says so. The
`capabilities` array in the *index entry* is a different, descriptive field —
what the plugin offers the user (`commands`, `dashboard`), not what it asks the
host for. Do not copy the registry names into it.

### Before opening the PR, confirm against the repo you are forking

The index is somebody else's file and its rules can move. Read the repository's
own README and any `CONTRIBUTING.md` or validation workflow **at the time of the
PR**, and match the surrounding entries in `index.json` for field order and
style. The shape above was read from the Hermes CLI's own `plugin_index.py` on
2026-08-23; if the file disagrees with this note, the file wins.

---

## 5. What must be true before either step

All of this is green as of 2026-08-23, in the staged tree itself:

```bash
python -m pytest tests/ -q                       # 1416 passed
hermes plugins doctor ./hermes-kame-api-rotation --ci
python tools/host_assumptions.py                 # every host fact still holding
node --check hermes-kame-api-rotation/desktop-ui/plugin.js
```

Manifest facts that the installer, not the loader, enforces:

- **`manifest_version: 1`.** Hermes' plugin *loader* accepts 2, but the
  *installer* raises on anything above 1. Publishing with 2 makes the plugin
  uninstallable while still loading fine from a manual copy — which is exactly
  how that gets missed. Leave it at 1.
- `version: "1.2.0"` in `plugin.yaml`, `__version__ = "1.2.0"` in
  `core/__init__.py`, and the CHANGELOG entry all agree. `tests/test_v1_2_0.py`
  asserts this.
- `homepage:` points at the new repository.

---

## 6. Screenshots — the one thing that cannot be automated

`README.md` currently shows the status chip and the panel as **ASCII sketches**,
labelled in the file as drawn rather than photographed. That label is there so
the README never claims to show something it does not.

Only the owner can replace them, because it takes a running Hermes Desktop:

1. **The status-bar chip** with at least two pools visible, one of them counting down.
2. **The panel's Overview tab** (`/kame`) during a rotation, showing pool health.
3. **The panel's Settings tab**, showing the three labelled shelves — this is the
   1.2.0 headline and it is the screen a new user meets first.

Save them under `assets/`, replace the code blocks in the README's *Screenshots*
section with `![...](assets/....png)`, and **delete the "drawn, not
photographed" note** — it becomes false the moment real captures land. Redact
nothing: KAME's screens carry fingerprints and counts only, which is the point.

---

## 7. Order of operations, and why

1. **Create the repo and push.** Everything else points at a URL that must exist.
2. **Tag the release** (optional, cosmetic — the index pins the SHA, not the tag).
3. **Open the index PR** with the SHA from step 1.
4. **Screenshots** whenever the owner can capture them — a README improvement, a
   new commit, and (only if you want the index to serve it) a new PR with the new
   SHA.

The Agent Zero rename is independent of all of the above and can happen in any
order; the two repositories only reference each other through prose links, which
GitHub redirects after a rename.

---

## 8. Security rules (same as the Agent Zero guide)

- **Never** put an API key in the repo, in a test fixture that looks real, or in
  a commit message. The fixtures in `tests/` are synthetic
  (`AIzaSyAAAA…`) by construction.
- `~/.hermes/.env` is a secrets file and is **not** in the published tree. KAME
  writes only `KAME_*` lines to it and preserves everything else byte-for-byte.
- Nothing KAME writes — snapshot, event log, control file, status line — ever
  contains key material. Fingerprints and counts only. That property is asserted
  by the test suite, not just claimed here.
- Use `gh` (already authenticated) rather than a pasted PAT. If a PAT is ever
  used, revoke it at `https://github.com/settings/tokens` immediately after.
