param(
    [ValidateSet("all", "latest-batch")]
    [string]$ExportMode = "all",
    [switch]$SkipSavedHtmlImport,
    [switch]$StartApi
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding

$baseDir = Split-Path -Parent $PSScriptRoot
$projectDir = Resolve-Path $baseDir

Set-Location $projectDir

if (-not $SkipSavedHtmlImport) {
    Write-Host "1/6 Import saved Taobao HTML into SQLite"
    python (Join-Path $projectDir "pa1.py") --scan-saved-html --html-glob "**/*.html"
} else {
    Write-Host "1/6 Skip saved HTML import"
}

Write-Host "2/6 Archive old SQLite batches"
python (Join-Path $projectDir "scripts\\archive_old_market_data.py") --keep-batches 15

Write-Host "3/6 Run Flink to MySQL pipeline"
powershell -ExecutionPolicy Bypass -File (Join-Path $projectDir "scripts\\run_flink_mysql_pipeline.ps1") -ExportMode $ExportMode

Write-Host "4/6 Build subtype analytics and alerts"
python (Join-Path $projectDir "market_analytics_pipeline.py") --mode $ExportMode

Write-Host "5/6 Build smart build knowledge base"
python (Join-Path $projectDir "scripts\\build_smart_build_knowledge.py")

if ($StartApi) {
    Write-Host "6/6 Start dashboard API"
    powershell -ExecutionPolicy Bypass -File (Join-Path $projectDir "scripts\\run_market_api.ps1")
} else {
    Write-Host "6/6 Dashboard API not started"
}

Write-Host "One-click data refresh completed"
