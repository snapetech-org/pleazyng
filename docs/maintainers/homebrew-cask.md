# Homebrew Cask release preparation

The macOS product bundle is `Pleazyng.app`, and the release disk image is `pleazyng-macos.dmg`; the app uses the `org.snapetech.pleazyng` bundle identifier. The release workflow can produce a version-pinned Cask candidate for each stable macOS release. The candidate is generated only when `prepare_homebrew_cask` is enabled for a full release run. To validate the macOS bundle and Gatekeeper without creating a release, dispatch with action `build`, mode `macos-validation`, and `prepare_homebrew_cask` enabled; only the macOS build job runs.

## Release requirements

- The macOS app must be signed with a Developer ID Application certificate, notarized by Apple, stapled, and pass the workflow Gatekeeper assessment.
- Configure `MACOS_CERTIFICATE_BASE64`, `MACOS_CERTIFICATE_PASSWORD`, `APPLE_API_KEY_ID`, `APPLE_API_ISSUER_ID`, `APPLE_API_PRIVATE_KEY`, and `APPLE_TEAM_ID` as repository or organization secrets. The ordinary release path stays optional-signing; the Homebrew option fails closed if any signing or notarization credential is missing.
- Dispatch the workflow against a stable `vMAJOR.MINOR.PATCH` tag with action `release` and mode `release`, then enable `prepare_homebrew_cask`.
- The run attaches `snapetech-pleazyng.rb` to the draft release and attests that candidate. Publish the release before submitting or installing the Cask because Homebrew must be able to fetch the versioned DMG from the public release URL.
- Verify the published DMG checksum matches the `sha256` in the candidate and confirm the asset URL uses the `vMAJOR.MINOR.PATCH` release tag. Ordinary qualified releases use `pleazyng-macos.dmg` and the core `SHA256SUMS.txt` manifest.

## v3.0.51 notarization recovery

The v3.0.51 core release contains an unsigned universal macOS DMG. Recover its
Homebrew payload with the focused **Recover v3.0.51 Homebrew Cask** workflow on
`main`; do not rebuild the app or replace any existing release asset. The
workflow verifies the attested core checksum manifest, the exact released DMG,
the release assembly, and the original trusted macOS compilation checkpoint.
It checks that the app tree inside the published DMG exactly matches that
checkpoint, then signs and notarizes a working copy on the public hosted macOS
runner. The signed result is published as the distinct
`pleazyng-macos-notarized.dmg` asset with an attested
`pleazyng-homebrew-SHA256SUMS.txt` manifest and
`pleazyng-homebrew-recovery.json` receipt. The original
`pleazyng-macos.dmg` and `SHA256SUMS.txt` remain unchanged.

Before release upload, the workflow preserves the exact four-file bundle as a
90-day Actions artifact. If GitHub accepts only part of the release upload,
resume from that attempt with the **Resume v3.0.51 Homebrew asset upload**
workflow. It verifies the source recovery run and all four asset attestations,
then uploads only names that are absent and rejects any existing name whose
digest differs from the preserved bytes. Dispatch it with the source run ID and
attempt; do not rerun signing to replace a partially published bundle.

```sh
gh workflow run resume-homebrew-cask-v3051.yml \
  --repo snapetech-org/pleazyng --ref main \
  -f source_run_id="$RECOVERY_RUN_ID" -f source_attempt="$RECOVERY_ATTEMPT"
```

Set those variables to the recovery run and attempt that uploaded the preserved
bundle artifact.

Update the custom tap only after all supplemental assets are attached:

```sh
gh workflow run update-packages.yml --repo snapetech-org/pleazyng --ref main \
  -f action=update-packages -f runner_pool=public -f release_tag=v3.0.51
```

The package workflow verifies the supplemental asset attestations, recovery
receipt, manifest, signature, stapled ticket, and Gatekeeper assessment. Its
Cask points to the version-pinned supplemental DMG. Tap publication rejects a
version rollback and rejects different bytes for an already-published version.
Do not upload with `--clobber` or edit the original core checksum manifest.

The Developer ID Application certificate was issued on 2026-11-05 for team `DM9A59DC85`, and the two dedicated macOS certificate secrets are configured. Existing iOS distribution credentials are separate and remain unchanged. Notarization uses the existing App Store Connect API key; an Apple app-specific password and persistent keychain password are not required. Native import, signing, notarization and Gatekeeper still need to pass on the qualified release.

## Stable-release recovery

For a published stable release created by one successful direct full run of
`.github/workflows/build.yml`, use **Recover Stable Homebrew Cask** on `main`.
Supply the exact release tag, source run ID and attempt. The workflow requires
successful `build-macos` and `create-release` jobs in that same attempt.
It verifies the published tag resolves to that run's source commit, checks the
attested core checksum manifest and immutable DMG digest, and confirms the app
inside the published DMG has the same tree as the run's durable unsigned Mac
compilation checkpoint before signing a working copy. The original DMG and
core manifest remain unchanged.

```sh
gh workflow run recover-homebrew-cask.yml \
  --repo snapetech-org/pleazyng --ref main \
  -f release_tag=v3.0.52 \
  -f source_run_id="$RELEASE_RUN_ID" \
  -f source_attempt="$RELEASE_ATTEMPT"
```

If the published release was assembled from a separate source run, pass that
source run and checkpoint attempt as above, then also supply the successful
assembly run ID and attempt:

```sh
gh workflow run recover-homebrew-cask.yml \
  --repo snapetech-org/pleazyng --ref main \
  -f release_tag=v3.0.52 \
  -f source_run_id="$SOURCE_RUN_ID" \
  -f source_attempt="$SOURCE_ATTEMPT" \
  -f assembly_run_id="$ASSEMBLY_RUN_ID" \
  -f assembly_attempt="$ASSEMBLY_ATTEMPT"
```

In this mode, the source attempt must contain a successful `build-macos`
checkpoint and identify the versioned source commit. The assembly attempt must
have a successful `create-release` job, and its signed checksum manifest and
`platform-reuse-provenance.json` must bind the release assets to that exact
assembly and original Mac source run. The recovery receipt preserves both run
identities, the checkpoint digest and successful job IDs. Releases assembled
from platform reuse are rejected when the separate assembly identity or proof
is missing.

After all four attested recovery assets are attached, update the custom tap:

```sh
gh workflow run update-packages.yml --repo snapetech-org/pleazyng --ref main \
  -f action=update-packages -f runner_pool=public -f release_tag=v3.0.52
```

If the release accepts only part of the recovery bundle, resume the saved
attempt without signing again:

```sh
gh workflow run resume-homebrew-cask.yml \
  --repo snapetech-org/pleazyng --ref main \
  -f source_run_id="$RECOVERY_RUN_ID" -f source_attempt="$RECOVERY_ATTEMPT"
```

Keep the pinned v3.0.51 recovery workflow and its historical assembly evidence
unchanged; the reusable workflow handles direct releases and later platform
assemblies with explicit run identities and provenance.

## Release handoff

With `--publish`, the release controller updates the Pleazyng tap only when the
published release contains the attested `snapetech-pleazyng.rb` candidate and
all Developer ID, notarization, and tap credentials are configured. If the
candidate is absent, the controller records Homebrew Cask as skipped. A direct
`update-packages.yml` dispatch also reports a no-op when its release has no
Cask candidate or the publisher credentials are incomplete. When enabled, the
workflow verifies the candidate attestation, the selected checksum manifest,
and the signed, notarized DMG before writing the tap. For v3.0.51 recovery it
selects the separately attested supplemental DMG and manifest.

The Sparkle `appcast.xml` is generated and attached by the core release for
every version; skipping Cask publication does not affect updater-feed delivery.
The old mutable appcast branch writer remains retired.

## Official Homebrew submission

Use the generated Cask as `Casks/s/snapetech-pleazyng.rb` in `Homebrew/homebrew-cask`. The `snapetech-` prefix identifies our Cask, which installs the Pleazyng bundle as `Pleazyng.app`.

Before opening a submission, review the current Homebrew Cask acceptance policy and contribution guide. New Casks must pass Gatekeeper without removing quarantine, include the required metadata and zap stanza, and pass the current audit and style checks. Homebrew may decline self-submissions that do not meet its current repository age and popularity criteria. Disclose AI assistance if the current contribution process requests it, and review all generated content yourself.

Run the checks against the generated file in a Homebrew checkout:

```sh
brew audit --cask --new Casks/s/snapetech-pleazyng.rb
brew style --cask Casks/s/snapetech-pleazyng.rb
```

Submit the reviewed Cask to the official `Homebrew/homebrew-cask` repository using its current contribution process. Do not submit the template or a candidate that points to a draft release.

## Generate locally

After a release build has been verified as signed and notarized, the candidate can also be rendered from the release checksum manifest:

```sh
python3 scripts/generate_homebrew_cask.py \
  --version 3.0.1 \
  --checksums artifacts/SHA256SUMS.txt \
  --output artifacts/homebrew/snapetech-pleazyng.rb
```

Replace the example version with the exact stable release version. The generator requires exactly one checksum entry for `pleazyng-macos.dmg` and rejects prerelease or malformed versions.

## Custom tap option

The Update Packages workflow publishes the verified candidate to `Casks/snapetech-pleazyng.rb` in `snapetech-org/homebrew-pleazyng`. Configure `HOMEBREW_TAP_TOKEN` with Contents write access limited to that tap repository. This fine-grained token is configured, restricted to the dedicated Pleazyng tap with Contents read/write and required Metadata read; it expires November 6, 2027. The ordinary repository token cannot write across repositories. The tap now publishes v3.0.51, whose Cask points to `pleazyng-macos-notarized.dmg` with SHA-256 `0feb3cc8b60ccb27a77b5ccb2784b370b47fe1e5481df9dd86f7a2a03f0695d6`. The recovery [run 45570676360](https://github.com/snapetech-org/pleazyng/actions/runs/45570676360) signed and notarized the exact published app, and the tap publisher [run 45572337364](https://github.com/snapetech-org/pleazyng/actions/runs/45572337364) verified the mounted image and updated the tap. The mounted app passed signature, stapling, and Gatekeeper checks; a separate `brew install` test has not been run.

Install from the custom tap with `brew install --cask snapetech-org/pleazyng/snapetech-pleazyng`. Keep the version and checksum pinned to a published release; do not use a moving `latest` DMG URL.

There is no `plezy` Cask entry: Pleazyng is distributed under the `snapetech-pleazyng` token. Do not add a quarantine-removal postflight. Only publish signed and notarized Pleazyng builds that pass Gatekeeper.
