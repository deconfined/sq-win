[CmdletBinding()]
param(
    [switch]$SkipOpenSsl
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if ($env:OS -ne 'Windows_NT') {
    throw 'This setup script requires Windows.'
}

# The Rust capnpc build dependency invokes the upstream capnp compiler.
# These are build tools only; no Cap'n Proto DLLs need to be distributed.
$capnpVersion = '1.5.0'
$capnpSha256 = '21501909dc051347563d4e83394eb848204f558e959501c8061dbeaaf0651988'
$capnpUrl = "https://capnproto.org/capnproto-c++-win32-$capnpVersion.zip"
$taskTemp = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { [IO.Path]::GetTempPath() }
$installRoot = Join-Path $taskTemp "sequoia-build-tools/capnp-$capnpVersion"
$archivePath = Join-Path $taskTemp "capnproto-c++-win32-$capnpVersion.zip"

Invoke-WebRequest -Uri $capnpUrl -OutFile $archivePath
$actualSha256 = (Get-FileHash -Path $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actualSha256 -ne $capnpSha256) {
    throw "Cap'n Proto checksum mismatch: expected $capnpSha256, received $actualSha256."
}

New-Item -Path $installRoot -ItemType Directory -Force | Out-Null
Expand-Archive -Path $archivePath -DestinationPath $installRoot -Force
$capnpDirectory = Join-Path $installRoot "capnproto-tools-win32-$capnpVersion"
$capnpExecutable = Join-Path $capnpDirectory 'capnp.exe'
if (-not (Test-Path -LiteralPath $capnpExecutable -PathType Leaf)) {
    throw "The verified Cap'n Proto archive did not contain $capnpExecutable."
}

$env:PATH = "$capnpDirectory;$env:PATH"
if ($env:GITHUB_PATH) {
    Add-Content -Path $env:GITHUB_PATH -Value $capnpDirectory -Encoding utf8
}
& $capnpExecutable --version
if ($LASTEXITCODE -ne 0) {
    throw "Cap'n Proto failed to start (exit code $LASTEXITCODE)."
}

if (-not $SkipOpenSsl) {
    # sequoia-net 0.30.1 selects Hickory's dnssec-openssl feature,
    # independently of CNG. SOP, SQV and the keystore server do not need it.
    # Static OpenSSL with the dynamic MSVC runtime needs no OpenSSL DLLs.
    $vcpkgRoots = @($env:VCPKG_ROOT, $env:VCPKG_INSTALLATION_ROOT)
    $vcpkgCommand = Get-Command vcpkg.exe -ErrorAction SilentlyContinue
    if ($vcpkgCommand) {
        $vcpkgRoots += Split-Path -Parent $vcpkgCommand.Source
    }
    $vcpkgRoot = $null
    foreach ($candidate in $vcpkgRoots) {
        if ($candidate -and (Test-Path -LiteralPath (Join-Path $candidate 'vcpkg.exe') -PathType Leaf)) {
            $vcpkgRoot = $candidate
            break
        }
    }
    if (-not $vcpkgRoot) {
        throw 'vcpkg.exe was not found. Use a GitHub-hosted Windows runner or set VCPKG_ROOT.'
    }

    $vcpkgCommit = & git -C $vcpkgRoot rev-parse HEAD
    if ($LASTEXITCODE -ne 0 -or $vcpkgCommit -notmatch '^[0-9a-f]{40}$') {
        throw 'Could not identify the vcpkg registry commit.'
    }
    $vcpkgTriplet = 'x64-windows-static-md'
    # vcpkg automatically respects VCPKG_DEFAULT_BINARY_CACHE from CI.
    & (Join-Path $vcpkgRoot 'vcpkg.exe') install "openssl:$vcpkgTriplet" --clean-after-build
    if ($LASTEXITCODE -ne 0) {
        throw "The static OpenSSL build failed (exit code $LASTEXITCODE)."
    }
    $env:VCPKG_ROOT = $vcpkgRoot
    $env:VCPKGRS_TRIPLET = $vcpkgTriplet
    $env:OPENSSL_STATIC = '1'
    $env:SEQUOIA_VCPKG_COMMIT = $vcpkgCommit
    if ($env:GITHUB_ENV) {
        Add-Content -Path $env:GITHUB_ENV -Value "VCPKG_ROOT=$vcpkgRoot" -Encoding utf8
        Add-Content -Path $env:GITHUB_ENV -Value "VCPKGRS_TRIPLET=$vcpkgTriplet" -Encoding utf8
        Add-Content -Path $env:GITHUB_ENV -Value 'OPENSSL_STATIC=1' -Encoding utf8
        Add-Content -Path $env:GITHUB_ENV -Value "SEQUOIA_VCPKG_COMMIT=$vcpkgCommit" -Encoding utf8
    }
} else {
    Write-Host 'OpenSSL is not required for this component; skipping vcpkg setup.'
}

# Stage these DLLs alongside the utilities so the portable archive also
# works on computers without Visual Studio or the VC++ redistributable.
$vswherePath = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio/Installer/vswhere.exe'
if (-not (Test-Path -LiteralPath $vswherePath -PathType Leaf)) {
    throw 'vswhere.exe was not found in the Visual Studio Installer directory.'
}
$visualStudioPath = & $vswherePath -latest -products '*' `
    -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if ($LASTEXITCODE -ne 0 -or -not $visualStudioPath) {
    throw 'No Visual Studio installation with x64 C++ build tools was found.'
}
$redistRoot = Join-Path $visualStudioPath 'VC/Redist/MSVC'
$redistVersions = Get-ChildItem -LiteralPath $redistRoot -Directory |
    Where-Object { $_.Name -match '^\d+\.\d+\.\d+(\.\d+)?$' } |
    Sort-Object -Property @{ Expression = { [version]$_.Name }; Descending = $true }
$vcRedistDirectory = $null
foreach ($versionDirectory in $redistVersions) {
    $candidate = Join-Path $versionDirectory.FullName 'x64/Microsoft.VC143.CRT'
    if (Test-Path -LiteralPath (Join-Path $candidate 'vcruntime140.dll') -PathType Leaf) {
        $vcRedistDirectory = $candidate
        break
    }
}
if (-not $vcRedistDirectory) {
    throw "The x64 Microsoft.VC143.CRT redistributable was not found under $redistRoot."
}
$env:SEQUOIA_VC_REDIST_DIR = $vcRedistDirectory
if ($env:GITHUB_ENV) {
    Add-Content -Path $env:GITHUB_ENV -Value "SEQUOIA_VC_REDIST_DIR=$vcRedistDirectory" -Encoding utf8
}
Write-Host "Using the app-local VC++ runtime from $vcRedistDirectory"

# CNG itself does not use libclang. Make the runner's existing LLVM visible
# if a dependency needs bindgen; do not install a second compiler toolchain.
$clangDirectories = @()
if ($env:LIBCLANG_PATH) {
    $clangDirectories += $env:LIBCLANG_PATH
}
if ($env:ProgramFiles) {
    $clangDirectories += Join-Path $env:ProgramFiles 'LLVM/bin'
}
$clangCommand = Get-Command clang.exe -ErrorAction SilentlyContinue
if ($clangCommand) {
    $clangDirectories += Split-Path -Parent $clangCommand.Source
}

foreach ($directory in $clangDirectories) {
    if (Test-Path -LiteralPath (Join-Path $directory 'libclang.dll') -PathType Leaf) {
        $env:LIBCLANG_PATH = $directory
        if ($env:GITHUB_ENV) {
            Add-Content -Path $env:GITHUB_ENV -Value "LIBCLANG_PATH=$directory" -Encoding utf8
        }
        Write-Host "Using libclang from $directory"
        break
    }
}
