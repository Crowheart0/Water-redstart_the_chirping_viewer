# Release process

The repository builds release assets for Windows x64, Apple Silicon macOS, and
Intel macOS from the same commit.

## Pull requests

Every pull request runs the unit tests and creates downloadable workflow
artifacts for all three targets. These artifacts are for validation only and do
not create or modify a GitHub Release.

## Publish a release

Only a maintainer with tag permission performs these steps after the release PR
has merged:

1. Confirm `CURRENT_VERSION` in `photo_viewer.py` is the intended semantic
   version, for example `3.6.0`.
2. Create and push the matching tag from the merged commit:

   ```bash
   git tag v3.6.0
   git push origin v3.6.0
   ```

3. The `Build and release` workflow tests and builds all platforms, then creates
   the GitHub Release and uploads:

   - `Water-redstart_v3.6.0_windows_x64.exe`
   - `Water-redstart_v3.6.0_macos_arm64.zip`
   - `Water-redstart_v3.6.0_macos_x64.zip`
   - one SHA-256 file for each package

The workflow rejects a tag whose version does not match `CURRENT_VERSION`.

## macOS signing status

Without Apple Developer secrets, PyInstaller applies an ad-hoc signature. Users
may need to Control-click the downloaded app and choose **Open** the first time.
For a frictionless public release, add Developer ID signing, hardened runtime,
and Apple notarization before publishing the macOS packages.

