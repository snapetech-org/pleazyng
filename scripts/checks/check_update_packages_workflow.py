#!/usr/bin/env python3
"""Guard the release-tag flow in update-packages.yml against regressions."""

from pathlib import Path
import re
import sys

from workflow_yaml import job_block


WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/update-packages.yml"
RECOVERY_WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/recover-homebrew-cask-v3051.yml"
RESUME_WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/resume-homebrew-cask-v3051.yml"
DIRECT_RECOVERY_WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/recover-homebrew-cask.yml"
DIRECT_RESUME_WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/resume-homebrew-cask.yml"
text = WORKFLOW.read_text(encoding="utf-8")
recovery_text = RECOVERY_WORKFLOW.read_text(encoding="utf-8")
resume_text = RESUME_WORKFLOW.read_text(encoding="utf-8")
direct_recovery_text = DIRECT_RECOVERY_WORKFLOW.read_text(encoding="utf-8")
direct_resume_text = DIRECT_RESUME_WORKFLOW.read_text(encoding="utf-8")
root = Path(__file__).resolve().parents[2]
recovery_script = (root / "scripts/recover_macos_dmg_for_homebrew.py").read_text(encoding="utf-8")
upload_script = (root / "scripts/upload_homebrew_recovery_assets.py").read_text(encoding="utf-8")
resume_script = (root / "scripts/resume_homebrew_recovery_upload.py").read_text(encoding="utf-8")
direct_recovery_script = (root / "scripts/recover_macos_dmg_for_homebrew_direct.py").read_text(encoding="utf-8")
direct_resume_script = (root / "scripts/resume_homebrew_recovery_upload_direct.py").read_text(encoding="utf-8")
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
    'homebrew_recovery_kind: ${{ steps.release.outputs.homebrew_recovery_kind }}' in resolver
    and 'HOMEBREW_RECOVERY_KIND: ${{ needs.resolve-release.outputs.homebrew_recovery_kind }}' in homebrew
    and 'HOMEBREW_RECOVERY_KIND" == legacy-assembly' in homebrew
    and 'HOMEBREW_RECOVERY_KIND" == platform-assembly' in homebrew
    and 'HOMEBREW_RECOVERY_KIND" != direct-source' in homebrew
    and 'recover_macos_dmg_for_homebrew_direct.py" verify' in homebrew,
    "Homebrew publication must verify direct-source and platform-reuse recovery receipts separately",
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
    and recovery.count('${preserve[@]+"${preserve[@]}"}') == 2
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
    and "Preserve exact Homebrew upload bundle for recovery" in recovery
    and "retention-days: 90" in recovery
    and "upload_homebrew_recovery_assets.py" in recovery
    and '"release", "upload"' in upload_script
    and 'existing.get("digest") != digest' in upload_script
    and "--clobber" not in recovery,
    "v3.0.51 recovery must preserve its attested bundle and append only matching, missing assets",
)
require(
    "MAX_API_ATTEMPTS = 4" in recovery_script
    and "per_page=30&page={page}" in recovery_script
    and "HTTP 5\\d\\d" in recovery_script,
    "v3.0.51 source-job verification must page through smaller responses and retry transient API failures",
)
direct_recovery = job_block(direct_recovery_text, "recover-homebrew")
direct_resolver = job_block(direct_recovery_text, "resolve-direct-release")
require(bool(direct_recovery) and bool(direct_resolver), "missing direct stable recovery jobs")
require(
    "runs-on: macos-26-intel" in direct_recovery
    and "runs-on: ubuntu-24.04" in direct_resolver
    and "github.ref == 'refs/heads/main'" in direct_recovery_text
    and "source_run_id" in direct_recovery_text
    and "source_attempt" in direct_recovery_text,
    "direct stable recovery must resolve exact source identities and use hosted runners from canonical main",
)
for required in (
    "verify_direct_source", "create-release", "build-macos", "restore_portable_checkpoint",
    "platform-reuse-provenance.json", "app_tree_sha256", "verify_build_attestation",
    "hdiutil attach", "codesign --verify --deep --strict", "notarization_checkpoint.py verify",
    "xcrun stapler validate", "spctl --assess --type execute", "actions/attest-build-provenance",
    "retention-days: 90", "recover_macos_dmg_for_homebrew_direct.py upload",
):
    require(required in direct_recovery_text or required in direct_recovery_script,
            f"direct recovery is missing source/signing/publication check {required}")
require(
    "actions: read" in direct_recovery
    and "attestations: write" in direct_recovery
    and "contents: write" in direct_recovery
    and "id-token: write" in direct_recovery
    and "self-hosted" not in direct_recovery
    and "macos-compiled-app-" in direct_recovery_script
    and "source_run_id" in direct_recovery_script
    and "release_job_id" in direct_recovery_script
    and "assembly_run_id" in direct_recovery_script
    and "verify_platform_reuse" in direct_recovery_script
    and "verify_build_attestation" in direct_recovery_script
    and "assembly_run_id" in direct_recovery_text,
    "stable recovery must attest append-only assets and bind platform assemblies to their exact provenance",
)
require(
    "resume_homebrew_recovery_upload_direct.py" in direct_resume_text
    and "source_run_id" in direct_resume_text
    and "source_attempt" in direct_resume_text
    and "runInvocationURI" in direct_resume_script
    and "--clobber" not in direct_resume_text,
    "direct stable partial uploads must resume from their exact attested artifact without replacement",
)
require(
    "runs-on: ubuntu-24.04" in resume_text
    and "github.ref == 'refs/heads/main'" in resume_text
    and "actions: read" in resume_text
    and "contents: write" in resume_text
    and "source_run_id" in resume_text
    and "source_attempt" in resume_text
    and "resume_homebrew_recovery_upload.py" in resume_text
    and "runInvocationURI" in resume_script
    and "actions/runs/{source_run_id}/attempts/{source_attempt}" in resume_script
    and "only missing matching release assets" in resume_script,
    "v3.0.51 partial uploads must resume from a verified recovery-run artifact on a hosted runner",
)

if errors:
    for error in errors:
        print(f"ERROR: {error}", file=sys.stderr)
    sys.exit(1)

print("update-packages workflow release-tag checks passed")
