param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$root = [IO.Path]::GetFullPath($PSScriptRoot).TrimEnd("\")
$venv = Join-Path $root ".build-venv"
$buildDir = Join-Path $root "build-v21"
$distDir = Join-Path $root "release"

function Assert-ChildPath([string]$Path) {
    $resolved = [IO.Path]::GetFullPath($Path)
    if (-not $resolved.StartsWith($root + "\", [StringComparison]::OrdinalIgnoreCase)) {
        throw "Guvenli proje kokunun disinda islem reddedildi: $resolved"
    }
}

Assert-ChildPath $venv
Assert-ChildPath $buildDir
Assert-ChildPath $distDir

if (-not (Test-Path -LiteralPath (Join-Path $venv "Scripts\python.exe"))) {
    & $Python -m venv $venv
}

$venvPython = Join-Path $venv "Scripts\python.exe"
& $venvPython -m pip install --disable-pip-version-check -r (Join-Path $root "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "Uygulama bagimliliklari yuklenemedi." }
& $venvPython -m pip install --disable-pip-version-check -r (Join-Path $root "requirements-build.txt")
if ($LASTEXITCODE -ne 0) { throw "Derleme bagimliliklari yuklenemedi." }
& $venvPython -m pip install --disable-pip-version-check -r (Join-Path $root "requirements-dev.txt")
if ($LASTEXITCODE -ne 0) { throw "Test bagimliliklari yuklenemedi." }

& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root "tools\generate_payload_manifest.ps1")
if ($LASTEXITCODE -ne 0) { throw "Payload manifesti olusturulamadi." }
$testTempDir = Join-Path $buildDir "pytest-temp"
Assert-ChildPath $testTempDir
if (Test-Path -LiteralPath (Join-Path $root "tests") -PathType Container) {
    New-Item -ItemType Directory -Path $testTempDir -Force | Out-Null
    $previousTemp = $env:TEMP
    $previousTmp = $env:TMP
    try {
        # Some managed Windows images deny cleanup in the user's global Temp
        # folder. Keep pytest's temporary files under the build directory, which
        # is deleted immediately before PyInstaller creates the final package.
        $env:TEMP = $testTempDir
        $env:TMP = $testTempDir
        & $venvPython -m pytest -q --basetemp $testTempDir
    } finally {
        $env:TEMP = $previousTemp
        $env:TMP = $previousTmp
    }
    if ($LASTEXITCODE -ne 0) { throw "Testler basarisiz; release paketi uretilmedi." }
} else {
    Write-Host "Public kaynak tesliminde test fixture'lari yok; pytest atlandi."
}

if (Test-Path -LiteralPath $buildDir) {
    Remove-Item -LiteralPath $buildDir -Recurse -Force
}
if (Test-Path -LiteralPath $distDir) {
    # Operational credentials are deliberately stored beside, not inside, the
    # distributable application folder. Keep this protected directory while
    # refreshing the generated package.
    Get-ChildItem -LiteralPath $distDir -Force |
        Where-Object { $_.Name -ne "private_secrets" } |
        ForEach-Object {
            Remove-Item -LiteralPath $_.FullName -Recurse -Force
        }
}

& $venvPython -m PyInstaller --clean --noconfirm `
    --workpath $buildDir `
    --distpath $distDir `
    (Join-Path $root "ACIK_Kurulum.spec")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller EXE uretemedi." }

$releaseDir = Join-Path $distDir "ACIK-Kurulum-v5.21"
$releaseExe = Join-Path $releaseDir "ACIK-Kurulum.exe"
if (-not (Test-Path -LiteralPath $releaseExe -PathType Leaf)) {
    throw "Derleme tamamlandi ancak ana EXE bulunamadi: $releaseExe"
}
if (Test-Path -LiteralPath (Join-Path $releaseDir "app_config.local.json")) {
    throw "Guvenlik hatasi: app_config.local.json dagitim paketine girdi."
}

if ($env:ACIK_SIGN_CERT_SHA1) {
    $signtool = (Get-Command signtool.exe -ErrorAction Stop).Source
    & $signtool sign /sha1 $env:ACIK_SIGN_CERT_SHA1 /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 $releaseExe
    & $signtool verify /pa $releaseExe
}

$hashes = Get-ChildItem -LiteralPath $releaseDir -File -Recurse |
    Sort-Object FullName |
    ForEach-Object {
        [pscustomobject]@{
            path = $_.FullName.Substring($releaseDir.Length + 1).Replace("\", "/")
            sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
            size = $_.Length
        }
    }
$hashes | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $releaseDir "release_manifest.json") -Encoding UTF8
Write-Host "Temiz dagitim hazir: $releaseDir"
