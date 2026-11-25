#!/usr/bin/env python3
"""Guard the release-tag flow in update-packages.yml against regressions."""

from pathlib import Path
import re
import sys

from workflow_yaml import job_block


WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/update-packages.yml"
RECOVERY_WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/recover-homebrew-cask-v3051.yml"
text = WORKFLOW.read_text(encoding="utf-8")
recovery_text = RECOVERY_WORKFLOW.read_text(encoding="utf-8")
errors: list[str] = []


def require(condition: bool, message: str) -> None:
    if not condition:
        errors.append(message)


def job(name: str) -> str:
    block = job_block(text, name)
    require(bool(block), f"missing {name} job")
    return block


require(
    re.search(
        r"(?ms)^  workflow_dispatch:\n    inputs:\n.*?^      release_tag:\n"
        r".*?^        required: true$",
        text,
    )
    is not None,
    "workflow_dispatch must require release_tag",
)
require("github.ref_name" not in text, "release tags must never fall back to a branch")
require(
    text.count("github.event.release.tag_name") == 1,
    "only the resolver may read the release event tag",
)

resolver = job("resolve-release")
require("gh release view \"$REQUESTED_TAG\"" in resolver, "resolver must validate the tag")
require(
    "--json tagName,isDraft,isPrerelease,publishedAt,assets" in resolver,
    "resolver must validate a published release and inspect its Cask assets",
)
require(
    'HAS_CASK_CANDIDATE=$(jq -r \'[.assets[]?.name] | any(. == "snapetech-pleazyng.rb")\'' in resolver
    and 'echo "homebrew_enabled=false" >> "$GITHUB_OUTPUT"' in resolver
    and 'Sparkle appcast delivery is handled by the core release and is independent of this Cask no-op.' in resolver,
    "missing Homebrew candidates must be an explicit no-op without affecting appcast delivery",
)
require('if [ "$IS_PRERELEASE" != "false" ]; then' in resolver, "resolver must reject a pre-release tag")
require(
    "tag: ${{ steps.release.outputs.tag }}" in resolver,
    "resolver must expose one validated tag output",
)

homebrew = job("update-homebrew")
require(
    'hdiutil attach -readonly -nobrowse -mountpoint "$mountpoint" "$HOMEBREW_ASSET_NAME"' in homebrew
    and 'codesign --verify --deep --strict' in homebrew
    and 'identifier "org.snapetech.pleazyng"' in homebrew
    and 'xcrun stapler validate "$app"' in homebrew
    and 'spctl --assess --type execute --verbose "$app"' in homebrew,
    "Homebrew must verify the mounted app signature, stapled ticket and Gatekeeper acceptance",
)
require(
    'verify_release_attestation.py" snapetech-pleazyng.rb' in homebrew
    and 'verify_release_attestation.py" SHA256SUMS.txt' in homebrew
    and 'verify_release_attestation.py" "$HOMEBREW_ASSET_NAME"' in homebrew
    and 'verify_release_attestation.py" "$HOMEBREW_CHECKSUMS_NAME"' in homebrew
    and 'recover_macos_dmg_for_homebrew.py" verify' in homebrew
    and '--repository-output "$RUNNER_TEMP/cask-publisher-repository.txt"' in homebrew
    and '--publisher-repository-file "$RUNNER_TEMP/cask-publisher-repository.txt"' in homebrew,
    "Homebrew must verify its recovery asset chain and preserve the Cask publisher identity",
)
require(
    "repository: snapetech-org/homebrew-pleazyng" in homebrew,
    "Homebrew publisher must target the dedicated Pleazyng tap",
)
require("update-appcast-branch:" not in text, "legacy appcast branch publishing must stay retired")
require("refs/heads/appcast" not in text, "updater feeds must not recreate a mutable branch")
require(
    "update-winget:" not in text,
    "the inherited upstream WinGet publisher must stay removed",
)
for stale_upstream in ("WINGET_TOKEN", "edde746.Plezy", "winget-releaser"):
    require(
        stale_upstream not in text,
        f"obsolete upstream package reference remains: {stale_upstream}",
    )
for name, block in (
    ("update-homebrew", homebrew),
):
    require("needs: resolve-release" in block, f"{name} must depend on the resolver")
require(
    "homebrew_enabled: ${{ steps.release.outputs.homebrew_enabled }}" in resolver
    and "if: needs.resolve-release.outputs.homebrew_enabled == 'true'" in homebrew,
    "Homebrew Cask updates must wait for Developer ID signing and notarization credentials",
)

for name, block in (
    ("resolve-release", resolver),
    ("update-homebrew", homebrew),
):
    require(
        re.search(r"(?m)^    permissions:", block) is not None,
        f"{name} must declare its GITHUB_TOKEN permissions",
    )
resolved_output = "${{ needs.resolve-release.outputs.tag }}"
require(f"RELEASE_TAG: {resolved_output}" in homebrew, "Homebrew must use the resolved tag")
require(
    re.search(r'gh release download "\$RELEASE_TAG"\s+--repo "\$GITHUB_REPOSITORY"', homebrew) is not None,
    "Homebrew must download from this repository",
)
require(
    "pleazyng-macos.dmg" in text and "plezyng-macos.dmg" not in text and "plezy-macos.dmg" not in text,
    "Homebrew must retain the core DMG as the normal release asset",
)
require(
    'HOMEBREW_ASSET_NAME: ${{ needs.resolve-release.outputs.homebrew_asset }}' in homebrew
    and '--asset-name "$HOMEBREW_ASSET_NAME"' in homebrew
    and 'pleazyng-macos-notarized.dmg' in resolver
    and 'RECOVERY_ASSET_COUNT' in resolver,
    "Homebrew must pin and validate the distinct notarized recovery DMG when present",
)
require(
    'scripts/check_homebrew_cask_update.py' in homebrew
    and '--version "$RELEASE_VERSION"' in homebrew,
    "Homebrew tap updates must reject version rollbacks and same-version byte changes",
)
require(
    "github.com/edde746/plezy" not in text,
    "package workflow must not target upstream releases",
)
require(
    re.search(r'git commit -m "[^"\n]*\$RELEASE_TAG[^"\n]*"', homebrew) is not None,
    "Homebrew commit text must include the resolved tag",
)

recovery = job_block(recovery_text, "recover-homebrew")
require(bool(recovery), "missing focused v3.0.51 notarization recovery job")
require(
    "runs-on: macos-26-intel" in recovery
    and "github.ref == 'refs/heads/main'" in recovery_text
    and "RELEASE_TAG: ${{ inputs.release_tag }}" in recovery,
    "v3.0.51 recovery must run on hosted macOS from the canonical main branch",
)
require(
    "recover_macos_dmg_for_homebrew.py prepare" in recovery
    and "macos-compiled-app-45421474803-2" in (Path(__file__).resolve().parents[2] / "scripts/recover_macos_dmg_for_homebrew.py").read_text()
    and "--test-requirement='=identifier \"org.snapetech.pleazyng\"" in recovery
    and "notarization_checkpoint.py verify" in recovery
    and "xcrun stapler validate" in recovery
    and "spctl --assess --type execute" in recovery,
    "v3.0.51 recovery must verify the published payload, native compile checkpoint, signature and notarization",
)
require(
    "actions: read" in recovery
    and "attestations: write" in recovery
    and "contents: write" in recovery
    and "id-token: write" in recovery
    and "Attest versioned Homebrew recovery assets" in recovery
    and "gh release upload" in recovery
    and "--clobber" not in recovery,
    "v3.0.51 recovery must attest and append distinct release assets without clobbering",
)

if errors:
    for error in errors:
        print(f"ERROR: {error}", file=sys.stderr)
    sys.exit(1)

print("update-packages workflow release-tag checks passed")
