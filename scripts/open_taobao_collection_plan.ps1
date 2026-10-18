param(
    [ValidateSet("minimal", "extended")]
    [string]$Plan = "minimal",
    [int]$PagesPerKeyword = 10,
    [switch]$OpenBrowser
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding

$projectDir = Resolve-Path (Join-Path $PSScriptRoot "..")
$outputDir = Join-Path $projectDir "data"
$planPath = Join-Path $outputDir "taobao_collection_plan.csv"

if (-not (Test-Path $outputDir)) {
    New-Item -ItemType Directory -Path $outputDir | Out-Null
}

$minimalPlan = @(
    @{ category = "cpu"; query = "7500F CPU"; slug = "cpu_7500f"; priority = 1; note = "Prefer standalone CPU items, not combo kits"; },
    @{ category = "cpu"; query = "12400F CPU"; slug = "cpu_12400f"; priority = 1; note = "Prefer standalone CPU items, not combo kits"; },
    @{ category = "cpu"; query = "9600X CPU"; slug = "cpu_9600x"; priority = 2; note = "Add mid-high AMD CPU samples"; },
    @{ category = "gpu"; query = "RTX 5060 GPU"; slug = "gpu_rtx5060"; priority = 1; note = "Prefer graphics cards, exclude full PC listings"; },
    @{ category = "gpu"; query = "RTX 5060 Ti GPU"; slug = "gpu_rtx5060ti"; priority = 1; note = "Prefer graphics cards, exclude full PC listings"; },
    @{ category = "gpu"; query = "RTX 5070 GPU"; slug = "gpu_rtx5070"; priority = 2; note = "Add upper-mid gaming tier"; },
    @{ category = "motherboard"; query = "B650M motherboard"; slug = "mb_b650m"; priority = 1; note = "Prefer board-only items, not CPU bundles"; },
    @{ category = "motherboard"; query = "B760M motherboard"; slug = "mb_b760m"; priority = 1; note = "Prefer board-only items, not CPU bundles"; },
    @{ category = "memory"; query = "DDR5 16G RAM"; slug = "ram_ddr5_16g"; priority = 1; note = "Need clear capacity tags"; },
    @{ category = "memory"; query = "DDR5 32G RAM"; slug = "ram_ddr5_32g"; priority = 2; note = "Used in higher-budget builds"; },
    @{ category = "memory"; query = "DDR4 16G RAM"; slug = "ram_ddr4_16g"; priority = 1; note = "Needed for DDR4 platform builds"; },
    @{ category = "ssd"; query = "512G NVMe SSD"; slug = "ssd_512g_nvme"; priority = 1; note = "Current build accuracy depends on SSD samples"; },
    @{ category = "ssd"; query = "1TB NVMe SSD"; slug = "ssd_1tb_nvme"; priority = 1; note = "Current build accuracy depends on SSD samples"; },
    @{ category = "ssd"; query = "2TB NVMe SSD"; slug = "ssd_2tb_nvme"; priority = 2; note = "Add larger-capacity storage"; },
    @{ category = "psu"; query = "650W PSU"; slug = "psu_650w"; priority = 1; note = "Prefer clear rated-power items"; },
    @{ category = "psu"; query = "750W PSU"; slug = "psu_750w"; priority = 1; note = "Needed for 5060/5070 class builds"; },
    @{ category = "case"; query = "ATX PC case"; slug = "case_atx"; priority = 2; note = "Need ATX/MATX-compatible cases"; },
    @{ category = "case"; query = "MATX PC case"; slug = "case_matx"; priority = 2; note = "Need smaller mainstream cases"; },
    @{ category = "cooler"; query = "CPU air cooler"; slug = "cooler_air"; priority = 2; note = "Need mainstream air coolers"; },
    @{ category = "cooler"; query = "240 CPU liquid cooler"; slug = "cooler_240_aio"; priority = 2; note = "Need mainstream 240 AIO coolers"; }
)

$extendedPlan = $minimalPlan + @(
    @{ category = "cpu"; query = "7800X3D CPU"; slug = "cpu_7800x3d"; priority = 3; note = "Add high-end gaming CPU"; },
    @{ category = "gpu"; query = "RX 9060 XT GPU"; slug = "gpu_rx9060xt"; priority = 3; note = "Add AMD GPU route"; },
    @{ category = "motherboard"; query = "B850 motherboard"; slug = "mb_b850"; priority = 3; note = "Add newer AM5 boards"; },
    @{ category = "memory"; query = "DDR4 32G RAM"; slug = "ram_ddr4_32g"; priority = 3; note = "Add higher-capacity DDR4"; },
    @{ category = "case"; query = "ITX PC case"; slug = "case_itx"; priority = 4; note = "Add compact builds"; },
    @{ category = "psu"; query = "850W PSU"; slug = "psu_850w"; priority = 4; note = "Add higher GPU power tier"; }
)

$selectedPlan = if ($Plan -eq "extended") { $extendedPlan } else { $minimalPlan }

$rows = foreach ($item in $selectedPlan) {
    $searchUrl = "https://s.taobao.com/search?q=$([uri]::EscapeDataString($item.query))"
    [PSCustomObject]@{
        priority = $item.priority
        category = $item.category
        query = $item.query
        pages_to_save = $PagesPerKeyword
        suggested_filename_pattern = "$($item.slug)_taobao_search_p{page}.html"
        search_url = $searchUrl
        note = $item.note
    }
}

$csvLines = $rows |
    Sort-Object priority, category, query |
    ConvertTo-Csv -NoTypeInformation
[System.IO.File]::WriteAllLines(
    $planPath,
    $csvLines,
    [System.Text.UTF8Encoding]::new($true)
)

Write-Host "Collection plan generated: $planPath"
Write-Host "Recommended save directory: $projectDir\\淘宝数据"
Write-Host ""
Write-Host "Priority guide:"
Write-Host "1 = collect first, directly improves smart-build accuracy"
Write-Host "2 = strongly recommended, improves candidate coverage"
Write-Host "3 or 4 = optional expansion"
Write-Host ""
Write-Host "Example filenames:"
Write-Host "ssd_1tb_nvme_taobao_search_p1.html"
Write-Host "ssd_1tb_nvme_taobao_search_p2.html"
Write-Host "ssd_1tb_nvme_taobao_search_p3.html"

if ($OpenBrowser) {
    Write-Host ""
    Write-Host "The script will open search pages one by one."
    Write-Host "Save the first $PagesPerKeyword pages manually, then return to this terminal and press Enter."
    foreach ($row in ($rows | Sort-Object priority, category, query)) {
        Write-Host ""
        Write-Host "Category: $($row.category)"
        Write-Host "Query: $($row.query)"
        Write-Host "Suggested filename: $($row.suggested_filename_pattern)"
        Write-Host "Note: $($row.note)"
        Start-Process $row.search_url
        Read-Host "Press Enter after saving the first $PagesPerKeyword pages for this query"
    }
}
