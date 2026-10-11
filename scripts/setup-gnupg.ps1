[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if ($env:OS -ne 'Windows_NT') {
    throw 'This setup script requires Windows.'
}

# The native GnuPG agent is used only for the Chameleon integration smoke.
# Its installer and installed files are excluded from the Sequoia archive.
$gnupgVersion = '2.5.24'
$installerName = "gnupg-w32-${gnupgVersion}_20260923.exe"
$installerSha256 = 'ea6f9dee2cccd83287432625b8b399173805add81d27067fcaf019c0c4da1ad8'
$installerUrl = "https://gnupg.org/ftp/gcrypt/binary/$installerName"
$taskTemp = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { [IO.Path]::GetTempPath() }
$installerPath = Join-Path $taskTemp $installerName
$installRoot = Join-Path $taskTemp "sequoia-ci-gnupg/$gnupgVersion"

Write-Host "Downloading the checksum-pinned GnuPG $gnupgVersion CI runtime"
Invoke-WebRequest -Uri $installerUrl -OutFile $installerPath -TimeoutSec 180
$actualSha256 = (Get-FileHash -LiteralPath $installerPath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actualSha256 -ne $installerSha256) {
    throw "GnuPG checksum mismatch: expected $installerSha256, received $actualSha256."
}

New-Item -Path $installRoot -ItemType Directory -Force | Out-Null
# The official NSIS installer can wait for an existing installation's
# uninstaller even in silent mode. Extract its verified runtime payload for
# CI without running the system installer or modifying machine-wide GnuPG.
$sevenZip = Join-Path $env:ProgramFiles '7-Zip/7z.exe'
if (-not (Test-Path -LiteralPath $sevenZip -PathType Leaf)) {
    $sevenZip = (Get-Command 7z.exe -ErrorAction Stop).Source
}
Write-Host "Extracting the verified GnuPG runtime to $installRoot"
& $sevenZip x $installerPath "-o$installRoot" -y 'bin/*' 'share/gnupg/*'
if ($LASTEXITCODE -ne 0) {
    throw "GnuPG runtime extraction failed (exit code $LASTEXITCODE)."
}

$gnupgBinDirectory = Join-Path $installRoot 'bin'
foreach ($name in @('gpg.exe', 'gpg-agent.exe', 'gpgconf.exe')) {
    $executable = Join-Path $gnupgBinDirectory $name
    if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
        throw "The verified GnuPG installer did not contain $executable."
    }
    & $executable --version
    if ($LASTEXITCODE -ne 0) {
        throw "$name failed to start (exit code $LASTEXITCODE)."
    }
}

$env:SEQUOIA_GNUPG_BIN = $gnupgBinDirectory
if ($env:GITHUB_ENV) {
    Add-Content -Path $env:GITHUB_ENV -Value "SEQUOIA_GNUPG_BIN=$gnupgBinDirectory" -Encoding utf8
}
Write-Host "Prepared native GnuPG $gnupgVersion for CI smoke tests at $gnupgBinDirectory"
