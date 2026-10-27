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
   version, for example `4.0`.
2. Create and push the matching tag from the merged commit:

   ```bash
   git tag v4.0
   git push origin v4.0
   ```

3. The `Build and release` workflow tests and builds all platforms, then creates
   the GitHub Release and uploads:

   - `Water-redstart_v4.0_windows_x64.exe`
   - `Water-redstart_v4.0_macos_arm64.zip`
   - `Water-redstart_v4.0_macos_x64.zip`
   - one SHA-256 file for each package

The workflow rejects a tag whose version does not match `CURRENT_VERSION`.

## Local Windows v4.0 package

Use Python 3.12 and a separate build environment:

```powershell
python -m venv .venv-v4
$env:PYTHONIOENCODING = 'utf-8'
.\.venv-v4\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv-v4\Scripts\python.exe -B -m unittest -v test_photo_viewer
.\.venv-v4\Scripts\python.exe -B -m unittest discover -s tests -v
New-Item -ItemType Directory -Force -Path _local\releases | Out-Null
New-Item -ItemType Directory -Force -Path dist | Out-Null
$env:PYINSTALLER_CONFIG_DIR = Join-Path (Get-Location) '_local\build\pyinstaller-cache'
.\.venv-v4\Scripts\python.exe -m PyInstaller --noconfirm --clean --workpath _local\build\pyinstaller --distpath _local\dist Water-redstart.spec
Copy-Item -LiteralPath _local\dist\Water-redstart.exe -Destination _local\releases\v4.0.exe
Copy-Item -LiteralPath _local\releases\v4.0.exe -Destination dist\v4.0.exe
.\.venv-v4\Scripts\python.exe -B scripts\verify_release.py
Get-FileHash -LiteralPath _local\releases\v4.0.exe -Algorithm SHA256
```

The spec embeds `bird.ico` as both the Windows EXE icon and a runtime resource,
and derives Windows file/product version metadata from `CURRENT_VERSION`.
The local requested filename is `v4.0.exe`; the workflow retains its
platform-qualified asset naming. Local packaging does not publish a release.
Final local deliverables are placed in root `dist/`; build inputs, temporary
files and archived packages stay under `_local/`. Preserve existing packages
before replacing them. For a same-version re-release, record checksums and
verify that the embedded modules match the repaired source, not just the
unchanged version string.

The smooth-navigation and sharp-Space re-release requires fresh macOS builds
of the final repaired source for both architectures. Repackaging an older `.app` into a DMG cannot
add the fixes. Windows can package a supplied Mac-built application, but cannot
perform the native macOS PyInstaller build. Test startup, next/previous browsing,
RAW decoding, first-Space magnification, release-to-restore and trackpad behavior
on the matching Mac before publishing. A Mac build made before the sharp-Space
change is not sufficient, even if its version is also 4.0.

## Files to commit and local-only files

Keep source code, tests, icons, the current `Water-redstart.spec`, build/document
requirements, reusable scripts and project documentation in Git. The existing
`readme/README.html` and `readme/README.pdf` remain versioned documentation.

Use the ignored `_local/` directory for files that are not source-control inputs:

- `releases/`: EXE and SHA-256 files to upload as GitHub Release assets, not commits.
- `archive/`: older EXE files and superseded local packaging specs.
- `samples/`: personal RAW/photo samples.
- `build/` and `dist/`: temporary builds, previews, local checks and generated packages.
- `work-notes/`: session plans and working logs.
- `settings/`: archived local application preferences.

Existing `.venv` and `.venv-v4` environments stay in place and remain ignored:
moving a virtual environment can invalidate its embedded tool paths. Root-level
`build/`, `dist/` and EXE outputs also remain ignored as a fallback for direct
PyInstaller invocations. Do not use `git add -f` on these local-only files.

## Recompile README

The source is `README.md`; the checked-in exports are
`readme/README.html` and `readme/README.pdf`.

```powershell
python -m pip install -r requirements-docs.txt
python scripts/build_readme.py
```

The script uses Python Markdown and a local Chrome or Edge browser in headless
mode. It generates a self-contained HTML document and an A4 PDF. It preserves
relative repository links by adjusting them for the `readme/` directory.
Review the generated PDF visually before distribution. Back up uncommitted
exports before overwriting them. Temporary browser profiles are created under
`_local/build/readme-render/`.

## macOS signing status

Without Apple Developer secrets, PyInstaller applies an ad-hoc signature. Users
may need to Control-click the downloaded app and choose **Open** the first time.
For a frictionless public release, add Developer ID signing, hardened runtime,
and Apple notarization before publishing the macOS packages.

