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

Invoke-WebRequest -Uri $installerUrl -OutFile $installerPath
$actualSha256 = (Get-FileHash -LiteralPath $installerPath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actualSha256 -ne $installerSha256) {
    throw "GnuPG checksum mismatch: expected $installerSha256, received $actualSha256."
}

New-Item -Path $installRoot -ItemType Directory -Force | Out-Null
# NSIS consumes the remaining command line after /D=. Keep it last and
# unquoted, including when RUNNER_TEMP contains spaces. Start-Process joins
# an argument array into a string, so supply the intended raw string here.
$installer = Start-Process -FilePath $installerPath `
    -ArgumentList "/S /D=$installRoot" -Wait -PassThru
if ($installer.ExitCode -ne 0) {
    throw "The GnuPG installer failed (exit code $($installer.ExitCode))."
}

$gnupgBinDirectory = Join-Path $installRoot 'bin'
foreach ($name in @('gpg-agent.exe', 'gpgconf.exe')) {
    $executable = Join-Path $gnupgBinDirectory $name
    if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
        throw "The verified GnuPG installer did not install $executable."
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
Write-Host "Installed native GnuPG $gnupgVersion for CI smoke tests at $gnupgBinDirectory"
