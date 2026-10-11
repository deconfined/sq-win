# Sequoia utilities for Windows

This repository builds the latest stable upstream Sequoia releases with GitHub Actions and provides native Windows x64 downloads. The complete bundle contains:

| Upstream project | Executables |
| --- | --- |
| [sequoia-sq](https://gitlab.com/sequoia-pgp/sequoia-sq) | `sq.exe` |
| [sequoia-chameleon-gnupg](https://gitlab.com/sequoia-pgp/sequoia-chameleon-gnupg) | `gpg-sq.exe`, `gpgv-sq.exe` |
| [sequoia-sqv](https://gitlab.com/sequoia-pgp/sequoia-sqv) | `sqv.exe` |
| [sequoia-sop](https://gitlab.com/sequoia-pgp/sequoia-sop) | `sqop.exe`, `sqopv.exe` |
| [sequoia-wot](https://gitlab.com/sequoia-pgp/sequoia-wot) | `sq-wot.exe` |
| [sequoia-cryptoki](https://gitlab.com/sequoia-pgp/sequoia-cryptoki) | `sq-cryptoki.exe` |
| [sequoia-keystore](https://gitlab.com/sequoia-pgp/sequoia-keystore) | `sequoia-keystore.exe` (optional server) |

## READ THIS

I made this pipeline so that I have easy access to Windows-compatible binaries. Anyone is welcome to download these, but...

**You have no reason to trust these builds!**

This build pipeline has no association whatsoever with the Sequoia project.

It may not select the right version (it might grab an old version, or it might grab an unstable version), the builds may not work correctly for some reason, or I could have done something nefarious in the build pipeline. (I didn't, but that's exactly what someone who did would say!)

Use at your own risk.

## Run the pipeline

Push this repository to GitHub with `main` as its default branch. No personal access token or additional repository secret is required.

1. Open **Actions → Windows stable releases → Run workflow**.
2. Select `main` and leave **Publish the tested downloads as a GitHub Release** enabled.
3. Download `sequoia-all-windows-x86_64.zip` from **Releases** when the workflow completes. Individual project ZIPs, `SHA256SUMS`, and `sources.json` are also attached.

The workflow runs builds on pushes to `main` and pull requests, providing downloads under the run's **Artifacts → windows-downloads**. These events do not publish a release. Manual runs can also turn publishing off.

A daily schedule checks for new stable upstream versions. It skips an already complete published release and publishes a new collection when the source set or this pipeline's commit changes. Publication runs only from the default branch. GitHub Release assets are persistent downloads; Actions artifacts have a retention period. A public repository makes Release downloads available to everyone.

## How releases are selected and built

[projects.json](projects.json) specifies the seven projects, Cargo packages, feature sets and expected executables. The resolver reads all pages of GitLab tags and chooses the highest numeric `major.minor.patch` release. It excludes prereleases and experimental suffixes. It understands both plain and component-specific tags, including `sequoia-sq/v1.5.0`, and chooses `server/v…` for the keystore executable rather than a library/backend tag. `sq-cryptoki` selects its own `sq-cryptoki/v…` tool releases rather than the `sequoia-cryptoki/v…` library releases. `sq-wot` is built from the latest stable WOT source tag; its tools package has a separate version.

Resolution pins each tag to its exact commit. Each Windows job fetches that tag and rejects a changed commit. The jobs build on `windows-2022` for `x86_64-pc-windows-msvc`, using stable Rust, Windows CNG, bundled SQLite, and upstream Cargo lockfiles. Cap'n Proto's compiler is downloaded from its official site with a pinned SHA256. Hickory DNSSEC makes OpenSSL necessary even with CNG; vcpkg provides static OpenSSL libraries with the Microsoft dynamic C runtime. The ZIPs include the corresponding app-local Microsoft runtime DLLs.

The action definitions are pinned to the latest stable releases verified on October 11, 2026: checkout 7.0.1, setup-python 7.0.0, upload-artifact 7.0.2, download-artifact 8.0.2, and cache 6.1.0. The workflow uses their exact commit SHAs and current input definitions. Rust build caches are separated by project, runner image, compiler and upstream source; static OpenSSL's binary cache is also retained. Projects that do not use OpenSSL skip that installation.

Each component's executables are launched from its packaged directory with development directories removed from `PATH`. The assembly job requires every component and checks their provenance and binary hashes. It then tests:

- SOP key generation, signing, encryption and decryption.
- Detached signatures created by SOP and `gpg-sq`, verified through `sq`, `sqop`, `sqopv`, `sqv`, `gpgv-sq`, and native GnuPG.
- Rejection of a modified signed message through every verifier.
- Web-of-trust authentication and DOT output with `sq-wot`.
- Version reporting and token enumeration with `sq-cryptoki` and an empty module configuration.
- Native GnuPG-agent key import and signing through `gpg-sq`, including Windows file-descriptor options.

The integration job installs a checksum-pinned native GnuPG runtime for testing. GnuPG itself is not redistributed in these ZIPs.

Only a successful complete assembly reaches the publishing job. It verifies the download checksums, creates or resumes a draft Release, uploads every asset, and then publishes the Release. An incomplete published release causes an error instead of being overwritten.

## Windows compatibility patches

The latest stable Chameleon release, `v0.13.1`, contains unguarded Unix code and incomplete Windows descriptor handling. [chameleon-windows.patch](patches/chameleon-windows.patch) supplies Windows handle conversion, guards Unix-only code and daemonization, fixes Windows console streams, and uses `%APPDATA%\gnupg` by default.

[gpg-agent-windows.patch](patches/gpg-agent-windows.patch) fixes the locked `sequoia-gpg-agent` dependency's unnecessary use of MSYS2 `cygpath` for native Windows paths. The build copies the exact locked crate source into a local path override, applies this patch, and permits only that dependency's source/checksum to change in the build lockfile. Unrelated package changes fail the job. Original and build lockfiles, dependency source, and patches are included in the downloads.

These are downstream Windows builds, with the modifications recorded in each project's `share/<project>/build.json`. If a future stable release no longer matches a patch, the pipeline stops for review; it never falls back to an older version or publishes a partial collection.

`gpg-sq` secret-key operations require a separately installed native Windows GnuPG with `gpg-agent`, `gpgconf`, and pinentry for interactive prompts. Put GnuPG's `bin` directory on `PATH`. `gpgv-sq` verification does not need an agent. Chameleon's Parcimonie can run in the foreground; Unix daemonization/autostart is unavailable on Windows. The development recorder is excluded. `sq` can use its embedded keystore without installing the optional server.

`sq-cryptoki` manages PKCS#11 tokens. Token operations require a separately installed vendor PKCS#11 DLL, selected with `--module`. Vendor drivers and token hardware are not included. Upstream currently disables its Windows CI jobs; this pipeline builds and probes the Windows CLI, while hardware interoperability remains untested. The bundled `sq` and keystore server use the default software-key and GnuPG-agent backends; their optional PKCS#11, OpenPGP-card and TPM backends are not enabled.

## Download contents and provenance

Extract the ZIP and add its `bin` directory to `PATH`, keeping the DLLs beside the executables. No Rust installation is needed to run them. The bundle includes an end-user [README](WINDOWS-README.txt), exact source tags/commits, upstream source archives, licenses, patches, dependency metadata, Cargo lockfiles, compiler versions and SHA256 hashes. Binaries are not Authenticode-signed. The target is Windows x64; ARM64 and 32-bit downloads are not built.

Release tags are a fingerprint of the pinned upstream source set, target/build configuration, and pipeline commit. Published assets are retained; unchanged scheduled checks do not rebuild them. Stable Rust and the hosted runner's vcpkg/SDK versions can advance on later builds, so provenance records should be used when comparing builds; bit-for-bit reproducibility across different runner images is not promised.

## Local validation and builds

The resolver and packaging tests run with Python 3.11 or newer and have no third-party Python dependencies:

```sh
python -m unittest discover -s tests -v
```

On Windows with Rust, Visual Studio C++ tools and vcpkg installed, resolve the sources and run a component build from a fresh working directory:

```powershell
python scripts/resolve.py --pipeline-commit (git rev-parse HEAD)
./scripts/setup-windows.ps1
rustup default stable
rustup target add x86_64-pc-windows-msvc
python scripts/build_project.py --project sq
```

Project identifiers are `sq`, `chameleon`, `sqv`, `sop`, `wot`, `cryptoki`, and `keystore`. The setup script exports dependency variables into the current PowerShell session and, in CI, into subsequent Actions steps. The workflow is [windows-release.yml](.github/workflows/windows-release.yml).
