[CmdletBinding()]
param(
    [string]$Database = $env:MSDIAL_REPOSITORY_CATALOG,
    [int]$Port = 8771,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"

try {
    $appRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
    $sourceRoot = Join-Path $appRoot "src"
    $catalogData = Join-Path $appRoot "catalog-data"

    if ([string]::IsNullOrWhiteSpace($Database)) {
        $candidates = @(
            Get-ChildItem -LiteralPath $catalogData -Filter "*.sqlite" -File -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -notmatch "(?i)(backup|pre-schema|\.bak\.)" } |
                Sort-Object -Property @{ Expression = "Length"; Descending = $true },
                    @{ Expression = "LastWriteTimeUtc"; Descending = $true }
        )
        if ($candidates.Count -gt 0) {
            $Database = $candidates[0].FullName
        }
        else {
            New-Item -ItemType Directory -Path $catalogData -Force | Out-Null
            $Database = Join-Path $catalogData "catalog.sqlite"
        }
    }
    elseif (-not [IO.Path]::IsPathRooted($Database)) {
        $Database = Join-Path $appRoot $Database
    }
    $Database = [IO.Path]::GetFullPath($Database)

    $url = "http://127.0.0.1:$Port/"
    $runningDatabase = $null
    try {
        $health = Invoke-RestMethod -Uri "${url}api/health" -TimeoutSec 2
        if ($health.status -eq "ok") {
            if ($health.database) {
                $runningDatabase = [IO.Path]::GetFullPath([string]$health.database)
            }
            else {
                $status = Invoke-RestMethod -Uri "${url}api/status" -TimeoutSec 30
                $runningDatabase = [IO.Path]::GetFullPath([string]$status.database)
            }
        }
    }
    catch {
        # No compatible Catalog server is listening yet.
    }
    if ($runningDatabase) {
        if (-not [string]::Equals($runningDatabase, $Database, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Port $Port is running a Catalog for another database: $runningDatabase"
        }
        Write-Host "MS-DIAL Repository Catalog is already running."
        Write-Host "Database: $runningDatabase"
        Write-Host "URL: $url"
        if (-not $NoBrowser) {
            Start-Process $url
        }
        exit 0
    }

    $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($listener) {
        throw "Port $Port is already used by another application. Set a different port with -Port."
    }

    $pythonCommand = $null
    $pythonArguments = @()
    if (Get-Command py.exe -ErrorAction SilentlyContinue) {
        $pythonCommand = (Get-Command py.exe).Source
        $pythonArguments = @("-3")
    }
    elseif (Get-Command python.exe -ErrorAction SilentlyContinue) {
        $pythonCommand = (Get-Command python.exe).Source
    }
    if (-not $pythonCommand) {
        throw "Python 3 was not found. Install Python 3.10 or later, then double-click the launcher again."
    }

    $env:PYTHONPATH = if ($env:PYTHONPATH) {
        "$sourceRoot;$env:PYTHONPATH"
    }
    else {
        $sourceRoot
    }

    $serverArguments = @(
        "-m", "msdial_repository_catalog.gui_server",
        "--database", $Database,
        "--host", "127.0.0.1",
        "--port", "$Port"
    )
    if ($NoBrowser) {
        $serverArguments += "--no-browser"
    }

    Write-Host "Starting MS-DIAL Repository Catalog..."
    Write-Host "Database: $Database"
    Write-Host "URL: $url"
    Write-Host "Keep this window open while the Catalog is in use."
    & $pythonCommand @pythonArguments @serverArguments
    exit $LASTEXITCODE
}
catch {
    Write-Host ""
    Write-Host "Could not start MS-DIAL Repository Catalog." -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
