param(
    [ValidateSet("all", "latest-batch")]
    [string]$ExportMode = "all",
    [int]$TopN = 20
)

$ErrorActionPreference = "Stop"

$baseDir = Split-Path -Parent $PSScriptRoot
$projectDir = Resolve-Path $baseDir

Write-Host "1/2 Run Flink to MySQL pipeline"
powershell -ExecutionPolicy Bypass -File (Join-Path $projectDir "scripts\\run_flink_mysql_pipeline.ps1") -ExportMode $ExportMode

Write-Host "2/2 Build analytics and recommendations"
python (Join-Path $projectDir "market_analytics_pipeline.py") --mode $ExportMode --top-n $TopN

Write-Host "Full market pipeline completed"
