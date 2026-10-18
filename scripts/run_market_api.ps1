$ErrorActionPreference = "Stop"

$baseDir = Split-Path -Parent $PSScriptRoot
$projectDir = Resolve-Path $baseDir

Set-Location $projectDir
python (Join-Path $projectDir "school_market_web.py")
