param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = "Stop"
$payloadRoot = Join-Path $ProjectRoot "payloads"
$outputPath = Join-Path $ProjectRoot "payload_manifest.json"
$catalogPath = Join-Path $ProjectRoot "src\acik_onboarding\payload_catalog.py"
$rootPrefix = [IO.Path]::GetFullPath($ProjectRoot).TrimEnd("\") + "\"

if (-not (Test-Path -LiteralPath $payloadRoot -PathType Container)) {
    throw "Payload klasoru bulunamadi: $payloadRoot"
}

$files = [ordered]@{}
Get-ChildItem -LiteralPath $payloadRoot -File -Recurse |
    Where-Object { -not ($_.Extension -ieq ".txt" -and $_.Name -match "key") } |
    Sort-Object FullName |
    ForEach-Object {
        $relativePath = $_.FullName.Substring($rootPrefix.Length).Replace("\", "/")
        $files[$relativePath] = [ordered]@{
            sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
            size = $_.Length
        }
    }

$manifest = [ordered]@{
    schema_version = 1
    generated_at_utc = [DateTime]::UtcNow.ToString("o")
    files = $files
}

$json = $manifest | ConvertTo-Json -Depth 6
[IO.File]::WriteAllText($outputPath, $json + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
$catalogLines = [Collections.Generic.List[string]]::new()
$catalogLines.Add('"""Generated payload integrity catalog. Do not edit by hand."""')
$catalogLines.Add("")
$catalogLines.Add("PAYLOAD_CATALOG: dict[str, dict[str, object]] = {")
foreach ($relativePath in $files.Keys) {
    $entry = $files[$relativePath]
    $safePath = $relativePath.Replace("\", "\\").Replace("'", "\'")
    $catalogLines.Add("    '$safePath': {'sha256': '$($entry.sha256)', 'size': $($entry.size)},")
}
$catalogLines.Add("}")
[IO.File]::WriteAllLines($catalogPath, $catalogLines, [Text.UTF8Encoding]::new($false))
Write-Host "Payload manifesti yazildi: $outputPath"
Write-Host "Gomulu payload katalogu yazildi: $catalogPath"
