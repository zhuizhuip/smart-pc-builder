$ErrorActionPreference = "Stop"

$baseDir = Split-Path -Parent $PSScriptRoot
$targetDir = Join-Path $baseDir "docker\\flink\\lib"

New-Item -ItemType Directory -Force -Path $targetDir | Out-Null

$files = @(
    @{
        Url = "https://repo1.maven.org/maven2/org/apache/flink/flink-connector-jdbc/3.2.0-1.18/flink-connector-jdbc-3.2.0-1.18.jar"
        Name = "flink-connector-jdbc-3.2.0-1.18.jar"
    },
    @{
        Url = "https://repo1.maven.org/maven2/com/mysql/mysql-connector-j/8.4.0/mysql-connector-j-8.4.0.jar"
        Name = "mysql-connector-j-8.4.0.jar"
    }
)

foreach ($file in $files) {
    $output = Join-Path $targetDir $file.Name
    if (Test-Path $output) {
        Write-Host "已存在，跳过: $($file.Name)"
        continue
    }
    Write-Host "下载: $($file.Name)"
    Invoke-WebRequest -Uri $file.Url -OutFile $output
}

Write-Host "运行时依赖已准备到: $targetDir"
