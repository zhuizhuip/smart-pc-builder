param(
    [ValidateSet("all", "latest-batch")]
    [string]$ExportMode = "all"
)

$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $false

$baseDir = Split-Path -Parent $PSScriptRoot
$projectDir = Resolve-Path $baseDir
$composeFile = Join-Path $projectDir "docker-compose.yml"

Write-Host "1/4 Export Flink input CSV"
python (Join-Path $projectDir "scripts\\export_market_snapshot.py") --mode $ExportMode

Write-Host "2/4 Start MySQL and Flink containers"
docker compose -f $composeFile up -d --force-recreate mysql jobmanager taskmanager

Write-Host "3/4 Wait for MySQL ready"
for ($i = 0; $i -lt 30; $i++) {
    try {
        docker compose -f $composeFile exec -T mysql mysqladmin ping -h 127.0.0.1 -proot123 | Out-Null
        Write-Host "MySQL is ready"
        break
    } catch {
        Start-Sleep -Seconds 5
    }

    if ($i -eq 29) {
        throw "MySQL was not ready in time"
    }
}

Write-Host "4/4 Execute Flink SQL pipeline"
docker compose -f $composeFile exec -T mysql mysql -uroot -proot123 -D school_market -e "TRUNCATE TABLE pc_part_market_snapshot;"
$sqlOutput = cmd /d /c "docker compose -f ""$composeFile"" exec -T jobmanager /opt/flink/bin/sql-client.sh -f /opt/project/flink/sql/market_pipeline.sql 2>&1"
$sqlOutput | Write-Host

if ($LASTEXITCODE -ne 0) {
    throw "Flink SQL client exited with code $LASTEXITCODE"
}

if (($sqlOutput | Out-String) -match '\[ERROR\]') {
    throw "Flink SQL pipeline reported an error"
}

if (($sqlOutput | Out-String) -notmatch 'Job ID:\s*([a-f0-9]+)') {
    throw "Flink SQL pipeline did not return a job id"
}

$jobId = $Matches[1]
Write-Host "Submitted Flink job: $jobId"

for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep -Seconds 5
    $overview = Invoke-RestMethod -Uri "http://127.0.0.1:8081/jobs/overview"
    $job = $overview.jobs | Where-Object { $_.jid -eq $jobId } | Select-Object -First 1

    if (-not $job) {
        continue
    }

    Write-Host "Flink job status: $($job.state)"

    if ($job.state -eq "FINISHED") {
        Write-Host "Flink job finished successfully"
        break
    }

    if ($job.state -in @("FAILED", "CANCELED", "CANCELLING")) {
        throw "Flink job ended with state $($job.state)"
    }

    if ($i -eq 59) {
        throw "Flink job did not finish in time"
    }
}

Write-Host "Flink to MySQL pipeline completed"
