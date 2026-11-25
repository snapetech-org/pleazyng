# Amazon Fire TV / Stick release

The signed universal `pleazyng-amazon-firetv.apk` is required for every versioned
release, alongside `pleazyng-amazon-firetv-provenance.json`. It includes ARMv7 and
ARM64 for Fire TV hardware plus x86_64 so the exact signed APK can run in the
Android TV form-factor emulator before an Amazon upload. This Android APK does not
establish compatibility with Amazon devices running a different operating system.

The release job uses the existing Android signing secrets and fails if they are
missing or incomplete. It selects the Amazon OpenIAP dependency and compiles
`SUPPORTER_MODE=iap`, `AMAZON_STORE=true`, and
`LIFETIME_UNLOCK_PRODUCT_ID=pleazyng_lifetime_unlock`. The owner's Amazon draft
SKU has that identifier, updated name/description/artwork, and the retained USD
$2.99 base price. Apple and Google store records must be reconciled separately.

`verify_firetv_apk.py` verifies the APK signature, rejects Android debug signing,
checks `org.snapetech.pleazyng`, the enabled/exported Leanback launcher activity or
alias in the compiled manifest, optional touchscreen, ARMv7/ARM64/x86_64 runtimes,
and the bundled Amazon authentication public key. Its receipt records the APK
hash, size, signer fingerprint, version and static qualification.

Every Amazon store-upload run installs the exact signed release APK into an
Android TV API 36 x86_64 emulator. `scripts/qa/test_firetv_emulator.py` verifies
the emulator ABI, installs the package, launches its Leanback entry point, checks
that the app remains in the foreground after remote D-pad input, scans app logcat
for fatal crashes, and retains a screenshot, log, and hash-bound JSON receipt.
This gate runs before Fastlane can upload anything to Amazon; emulator or app
failure stops the upload. The emulator is a repeatable TV-form-factor smoke test,
not a substitute for Fire OS device, playback, billing or purchase qualification.

To produce only the signed Amazon APK from current main, without creating or changing
a release or uploading to a store:

```sh
gh workflow run build.yml -R snapetech-org/pleazyng --ref main \
  -f action=build -f runner_pool=public -f execution_profile=hybrid \
  -f mode=android-validation -f include_webos_build=false \
  -f include_webos_release=false -f include_tizen_build=false
```

Download the successful run's `android-apk` artifact. In Android validation mode it contains the Amazon APK and receipt; generic Android APKs and the Play bundle are built only in full release mode. Full release mode includes
these files in the draft assets and SHA256SUMS; the coverage gate rejects an
omission even if signing secrets are absent. Never rename an older package to
represent a new build or attach it to a different source revision.

### Automatic Appstore updates

After the first version goes live in Amazon, set the repository variable
`AMAZON_APPSTORE_BOOTSTRAP_COMPLETE=true`. Both release-controller publication
modes then wake `deliver-amazon-appstore.yml` for the published release. Release
events also wake it, and the hourly catch-up checks the latest stable release.
The workflow reuses the signed GitHub APK after checking platform coverage,
source attestation, checksums, and the Android TV emulator smoke test. It
submits the draft for Amazon review and retains a version- and hash-bound
receipt. Amazon controls review and availability.

Amazon accepts one active edit per app. Automatic delivery waits when an
operator draft is open or Amazon is reviewing an update. It never replaces a
newer store binary with an older one. APKs above 300 MB use Amazon's large upload
and attach API. Before committing, the workflow verifies the replacement and
the existing device targeting. It fills missing required release notes and
retains the existing listing assets and IAP configuration. If a commit response is lost or an edit remains
open, check the receipt and the Amazon console for that edit before retrying.
For a fresh retry against an existing published release, run:

```sh
gh workflow run deliver-amazon-appstore.yml --repo snapetech-org/pleazyng \
  --ref main -f release_tag=vX.Y.Z
```

The explicit `publish-app-stores.yml` workflow also supports `amazon_mode=verify`
to qualify a published APK and confirm read-only Amazon API access. Its default
`amazon_mode=draft` preserves the legacy Fastlane draft upload for small APKs.
Use the Developer Console when a large APK needs a manual draft handoff.

Capture genuine TV screens at 1920×1080, phone screens in portrait, and tablet
screens in their native orientation. Record source SHA, APK SHA256, device/AVD,
screen dimensions, density and capture command. Emulator captures must say
emulator; Android TV emulation is not Fire OS device qualification. Keep mockups
outside store screenshot slots and label them as interface concepts. Do not
include account credentials, private libraries, or invented playback claims.

## Native captures and labeled previews

After installing the signed candidate on an explicitly selected test AVD/device,
open a public demo library or other screenshot-cleared content. Capture at least
three distinct TV views (for example home, library and details); record emulator
limits separately from Fire OS hardware testing. Use the signed universal APK
on the TV, phone and tablet AVDs so image provenance matches the candidate:

```sh
python3 scripts/capture_app_screenshot.py --serial emulator-5580 \
  --kind android-tv-emulator --apk /path/pleazyng-amazon-firetv.apk \
  --source-sha FULL_APK_SOURCE_SHA --name tv-home --output /path/native-captures
```

The tool checks that Pleazyng is resumed and that the installed base APK hash
matches the supplied file. Capture receipts include source, APK/image hashes,
model, OS SDK, density and dimensions. TV captures must already be 1920×1080;
the tool never stretches phone images into TV screenshots.

For concept review before a native capture is available, render the production
Flutter widgets with the repository's synthetic appearance fixtures:

```sh
PLEAZYNG_PREVIEW_OUTPUT=/path/raw-previews flutter test --no-pub \
  tools/store-assets/preview_capture_test.dart
python3 tools/store-assets/package_previews.py --input /path/raw-previews \
  --output /path/interface-previews --source-sha FULL_SOURCE_SHA
```

The packager requires Pillow. It adds a visible preview label, a contact sheet,
manifest and ZIP. These fixtures do not represent a complete running app session;
they include synthetic movie metadata and test/debug UI. They are not eligible
for store screenshot slots. Keep them separate from native captures.

Package native captures with `tools/store-assets/package_captures.py --input
/path/native-captures --output /path/store-sets --group amazon-firetv` (or
`google-play-phone` / `google-play-tablet`). It requires three distinct TV frames
or two phone/tablet frames from one build. It verifies image hashes, opacity and
dimensions before writing 24-bit RGB PNGs and a provenance manifest; it never
resizes or crops. Configure a phone emulator to 1080×1920 before launching the
app, rather than exporting a tall 1080×2400 frame that exceeds Google Play's
maximum 2:1 aspect ratio. Preserve raw captures alongside upload copies.

## Amazon submission identity and first-version handoff

`AMAZON_APPSTORE_APP_ID` is a non-secret GitHub repository variable containing the
Amazon console identifier (`amzn1.devportal.mobileapp.…`), separate from Android
package identity `org.snapetech.pleazyng`. Fastlane plugin 1.7.0 misleadingly calls
this option `package_name` but forwards it directly into the API's `app_id` URL.
The lane rejects an Android package name in this setting and explicitly skips
metadata, changelog, image and screenshot uploads to preserve console edits.

[Amazon requires the first version to be submitted through the Developer
Console](https://developer.amazon.com/docs/app-submission-api/overview.html).
The corrective 3.0.36 candidate followed the first-version console handoff.
Subsequent automatic API updates submit the signed binary for review. Use the console for listing
metadata, screenshots, and IAP changes. Avoid making console edits while an
automatic submission is running.

## Committed capture pack and next release

The [screenshot review pack](store-listing-assets/README.md) contains four
Amazon Fire TV and three Fire tablet captures from the signed 3.0.36 Amazon APK,
plus historical Google Play phone/tablet captures, a contact sheet and APK/image
provenance. The Google Play images are not current and must not be submitted as
proof for Play review. Synthetic interface previews are kept separately.

## Amazon 3.0.36 corrective resubmission

The current Amazon copy is in
[store-listing-copy.md](store-listing-copy.md). Fresh TV and tablet screenshots
now come from the exact signed 3.0.36 Amazon APK (source
`2388a06cc74d03e69ac7178fcd927a54460a5e3a`, APK SHA256
`4d07d84d29994f338cea9bb5ba7f764d2b248b89409ab6a1f64f9fefbdc0a344`). The four
1920×1080 TV captures show Home, Search, title details and playback controls;
the three 2560×1600 tablet captures show Home, Search and title details. The
tablet captures use a generic Android emulator and do not establish Fire OS
qualification. Review the icon and promotional artwork, inspect every Fire TV
and tablet image slot in the Developer Console, replace stale items, and verify
the exact text and assets saved before resubmitting. Do not use welcome-only
screens, mockups, another store's APK or screenshots from a different form factor
as substitutes.

Keep the existing product title, SKU and IAP identity unless the console review
specifically requires a change. The first version and its correction are
submitted through the Developer Console. After resubmission, update the channel
record with the console status; uploading an APK through the API does not submit
it for review.

For the next full release, retain `mode=release` and build a new signed Amazon
APK from the final release source together with all other required platforms.
Confirm the APK and matching provenance receipt appear in both the release
assets and `SHA256SUMS.txt`; the required-platform coverage check rejects either
missing file. The successful 3.0.0 capture candidate is evidence for screenshots,
not a replacement for the next release build or later runtime qualification.

Keep Amazon's initial console submission separate from subsequent API updates.
Do not set `AMAZON_APPSTORE_BOOTSTRAP_COMPLETE=true` until that initial submission
has completed. Automatic submissions also require a source-attested APK and emulator receipt. Verify physical Fire
TV navigation, playback and lifetime purchase/restore before claiming device or
billing qualification. Keep `pleazyng_lifetime_unlock` and its USD $2.99 price.
