# Apple Store signing

The existing Apple Distribution identity passed native macOS qualification: the configured PKCS12 was imported, its private key verified against the certificate, and a small executable signed and verified strictly. No certificate was issued or revoked. The [2026-11-05 native preflight](https://github.com/snapetech-org/pleazyng/actions/runs/42169674435) passed the three host/Top Shelf profiles and both projects' signing bindings; its [bounded report](qualification/apple-signing-20261105.json) is historical evidence. It does not qualify the subsequently added iOS RommWorker profile, a complete app archive, App Store processing or purchase behavior.

The original PKCS12 required a compatible SHA1/3DES re-encoding for Security.framework. The isolated keychain also needs the checksum-pinned official Apple WWDR G3 intermediate and membership in the user keychain search list. `scripts/apple_signing_material.py` provides the same verified context for the preflight and release jobs, restores the original search list, deletes its keychain and removes task-owned temporary material. It never changes the default keychain or system trust.

## Required secrets

- `APPLE_DISTRIBUTION_CERTIFICATE_BASE64`
- `APPLE_CERTIFICATE_PASSWORD`
- `APPLE_TEAM_ID`
- `IOS_PROVISIONING_PROFILE_BASE64`
- `TVOS_PROVISIONING_PROFILE_BASE64`
- `TVOS_TOPSHELF_PROVISIONING_PROFILE_BASE64`
- `APPLE_API_ISSUER_ID` (iOS worker profile preflight)
- `APPLE_API_KEY_ID` (iOS worker profile preflight)
- `APPLE_API_PRIVATE_KEY` (iOS worker profile preflight)

Profiles must name `org.snapetech.pleazyng` for iOS/tvOS and `org.snapetech.pleazyng.TopShelfExtension` for Top Shelf, the correct platform, the imported certificate, and `group.org.snapetech.pleazyng`. Expired, development, ad-hoc, enterprise, other-team and old-bundle profiles fail validation. The old PlezyNG profiles are preserved for dependency review.

`apple-signing-preflight.yml` is manual, restricted to main and runs on hosted native macOS. Its artifact contains only bounded public certificate/profile fields. It checks the actual three profile bindings and the selected Release target settings. Do not upload keys, PKCS12 files, profile payloads or private working directories as artifacts.

## iOS worker profile preflight

The iOS extension also needs a distinct App Store profile for
`org.snapetech.pleazyng.RommWorker`. Enable its required Enhanced Security
capability through Apple's supported Developer portal before regenerating that
profile with the existing distribution certificate. Preserve the host and tvOS
identities, the certificate and the checked-in worker entitlements.

Use the current `build.yml` signing-only action to check this profile before
retrying a failed archive:

```sh
gh workflow run build.yml --repo snapetech-org/pleazyng --ref main \
  -f action=apple-signing-preflight -f runner_pool=public \
  -f execution_profile=hybrid -f mode=release
```

This action selects hosted `macos-26-intel` and separate concurrency; it does
not build an app or launch the release matrix. It validates the host profile
and creates or reuses a worker profile bound to the same team and existing
certificate. The worker must authorize all seven requirements from
`native/games/romm/apple/extension/RommWorker.entitlements`; an active profile
missing those grants is insufficient. The legacy three-profile check above
does not establish this requirement.

Require a successful run and its bounded `ios-profile-public-report` artifact
with both `profile_bootstrap_qualified` and `worker_security_qualified` true,
the expected entitlement names/source hash, and the expected certificate/team.
`app_build_qualified` remains false. An invalid fresh profile fails the check;
repair the capability before trying again. Keep profile payloads and signing
secrets out of artifacts. After qualification, retry only the failed original
archive and dependent release assembly, preserving successful platform outputs
and their original provenance.

## Store archive path

Use hosted `xcode-27` for focused iOS Store builds when the private Mac has only
Xcode 27.0. The iOS build checks that both device and simulator SDKs are 27.1 or
later. Choose private iOS signing when the host provides those SDKs. The focused
iOS workflow retries transient SwiftPM binary download errors and keeps each
artifact's checksum requirement in force.

Keep `include_apple_store_build=true` for the first coordinated signed qualification run. The unsigned developer IPA remains separate. `build_apple_store_archive.py` imports the signing identity in a scoped context, validates and temporarily installs profiles, applies distinct manual Release signing settings to Runner and TopShelfExtension, archives and exports with an explicit profile map. It restores the original project bytes and removes only profiles it created. Existing installed profiles are preserved. iOS Store compilation explicitly selects trial/IAP mode and disables the direct-download update check; tvOS's native AOT build already selects IAP mode.

The helper's public receipt records archive/export completion separately from purchase and application qualification. Run the exact intended release revision through the existing coordinated full build; do not dispatch a duplicate matrix just for these secrets. A signed IPA must still process in App Store Connect and pass actual device, StoreKit purchase/restore, reviewer-access and screenshot checks. Set `APPLE_IOS_STORE_UPLOAD_READY` or `APPLE_TVOS_STORE_UPLOAD_READY` only after verifying the corresponding signed build and upload path. Upload automation is separate from TestFlight tester invitations and App Review submission.

## macOS App Store package

`macos-app-store-signed.yml` is the manual, reusable signed Mac App Store lane.
It accepts an exact annotated stable version tag, checks that the tag and
`pubspec.yaml` version match, builds that immutable source with the existing
IAP build mode, and signs the universal app and installer for the exact bundle
`org.snapetech.pleazyng`. It uses the configured App Store Connect API key,
team ID and certificate-password secret; it does not need a new signing secret.

The lane inventories Apple certificates and requires an exact match for the
retained local App and Installer identities. It does not request replacement
certificates or generate new signing keys. The Installer CSR, private key and
CSR stay mode `0600` in the service user's recovery directory.
The existing App identity remains in
`~/Library/Keychains/Pleazyng-Mac-App-Store.keychain-db`. The Installer identity
uses `~/Library/Keychains/Pleazyng-Mac-App-Store-Installer-v2.keychain-db`;
the workflow imports the same retained Installer key and matching public
certificate into that new dedicated keychain. It leaves both identities in the
original keychain untouched. Both service keychains persist between signed
releases and are locked when each job ends. The workflow restores the user's
original keychain search list and preserves existing provisioning profiles.

Set the manual workflow's `preflight_only` input to verify the existing App and
Installer identities before an archive. It signs a temporary app bundle and
installer package locally, verifies both, and uploads only a public preflight
receipt. Preflight checks that the retained Installer key matches the
certificate in the original keychain, imports that same pair once into the new
Installer keychain with explicit `/usr/bin/productsign` and
`/usr/bin/productbuild` access, and signs a temporary app and package using
the separate keychains. The package signature must verify with `pkgutil` before
the receipt says `productsign_allowed`. It does not modify the original
keychain, delete or replace identities, create certificates or profiles, build
Pleazyng, or call App Store Connect. The normal workflow mode performs the
signed app build and upload after preflight succeeds. Before export, it also
checks that the embedded
`PlezyTorrent.framework` has its expected bundle identity and valid
`CFBundleShortVersionString` and `CFBundleVersion` metadata.

The workflow creates or reuses a `MAC_APP_STORE` profile only after checking the
exact team, bundle ID, macOS platform, selected certificate and expiration. It
temporarily changes only the macOS Runner Release signing settings, restores the
project file afterward, verifies the app and installer signatures, uploads the
package to App Store Connect app `6826955231`, and waits for the exact version
and build to reach `VALID`. It does not update listing metadata, create or edit
in-app purchase products, change `pleazyng_lifetime_unlock`, set prices, or
submit App Review. Sandbox/JIT runtime, StoreKit purchase and restore,
screenshots, and Apple review have separate qualification steps.

Apple's decoded `MAC_APP_STORE` profile payload uses the exact platform token
`OSX`; the profile validator requires that one-element platform list alongside
the App Store Connect API's `MAC_OS` profile platform.

For a profile-only diagnosis, dispatch the same workflow with
`profile_inspection_only=true`. That mode runs on public hosted Linux, validates
the immutable tag first, and makes only Apple API GET requests. Its bounded
artifact reports the API profile type/state/platform and the decoded profile's
platform, team, app identifier, certificate fingerprints and expiration; it
does not export profile contents, access keychains, create profiles, or build
the app. It verifies the CMS content signature with OpenSSL while explicitly
not evaluating the signer certificate chain; the API response itself arrives
over authenticated TLS. It does not use the private signing runner or queue
controller.

Xcode's `-resolvePackageDependencies` rewrites these tracked files while
refreshing the tagged Swift packages:

- `macos/Runner.xcodeproj/project.xcworkspace/xcshareddata/swiftpm/Package.resolved`
- `macos/Runner.xcworkspace/xcshareddata/swiftpm/Package.resolved`

The project resolver removes pins outside its package graph. The MAS lane
snapshots only these two known lockfiles, restores their exact release-tag bytes
after resolution and archive, and records each original SHA-256 in the signed
build receipt. Whole-checkout source validation remains strict before and after
those steps; any other tracked change fails the build.

The explicit `publish-app-stores.yml` Apple lanes inspect the checksum-verified,
attested IPA's bundle, platform, marketing version, and build number before
upload. They target the existing App Store Connect app ID `6826955231`, reuse
an already valid exact version/build without a duplicate upload, and wait up to
15 minutes for a new upload to reach `VALID`. `FAILED`, `INVALID`, duplicate,
or mismatched records fail closed. The workflow retains a public processing
receipt with the release IPA hash and GitHub run identity. App Store Connect
does not expose an IPA hash for an already existing build, so that receipt
marks reused builds as a version/build identity match. Upload success and
processing do not create a matching draft version, invite TestFlight users,
or submit App Review.

After Apple processes a build, run **Prepare an Apple review draft** on `main`
with its published tag, platform (`IOS`, `MAC_OS`, or `TV_OS`), and successful
processing run ID. The workflow resolves the build number from the tag and
attaches the exact valid build to its existing App Store version. It rechecks
the attachment and retains a receipt. Submit any first in-app purchases with
the intended version in the [App Store Connect review screen](https://developer.apple.com/help/app-store-connect/manage-submissions-to-app-review/submit-an-in-app-purchase).

The iOS archive uses an isolated, time-bounded signing keychain. Flutter's
native-asset signing call receives that same keychain and the iOS timestamp
setting used by the signing probe. A native probe verifies the route before
the archive starts. The archive reports log growth and free disk periodically
and includes a bounded excerpt on failure.

### v3.0.36 signed-store processing result

The frozen v3.0.36 iOS and tvOS IPAs were signed and exported by [run
43966734793](https://github.com/snapetech-org/pleazyng/actions/runs/43966734793).
The first iOS App Store Connect upload, [run
43997226541](https://github.com/snapetech-org/pleazyng/actions/runs/43997226541),
was rejected by Apple's Transporter before App Store Connect processing because
`PlezyTorrent.framework/Info.plist` lacked both `CFBundleShortVersionString`
and `CFBundleVersion`. Read-only inspection of the signed tvOS IPA found the
same missing keys, so tvOS was withheld from upload. Neither IPA reached
`VALID`; do not select either build for TestFlight or App Review. The published
v3.0.36 release assets are immutable and retained for provenance. New signed
Apple archives must carry framework version metadata and pass the embedded
bundle preflight before upload; qualify that correction on v3.0.37.

### Normal release and recovery upload manifests

A new signed release binds its IPA to the attested core `SHA256SUMS.txt` and the IPA's own build attestation. An append-only repair instead requires both `APPLE-STORE-SHA256SUMS.txt` and `APPLE-STORE-RECOVERY.json`, with their attestations and exact source/run/IPA binding. The release asset inventory selects the mode before download; partial sidecars fail closed. Recovery sidecars are not prerequisites for a normal new release.
