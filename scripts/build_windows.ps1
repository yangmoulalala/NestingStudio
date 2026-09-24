$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot

Write-Host "Installing build dependencies..."
python -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "Failed to install runtime dependencies with exit code $LASTEXITCODE" }
python -m pip install "pyinstaller==6.22.3"
if ($LASTEXITCODE -ne 0) { throw "Failed to install PyInstaller with exit code $LASTEXITCODE" }

Write-Host "Building NestingStudio..."
python -m PyInstaller --noconfirm --clean NestingStudio.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed with exit code $LASTEXITCODE" }

$exe = Join-Path $projectRoot "dist\NestingStudio\NestingStudio.exe"
if (-not (Test-Path -LiteralPath $exe)) { throw "Build output not found: $exe" }

Write-Host "Running packaged application smoke test..."
& $exe --smoke-test
if ($LASTEXITCODE -ne 0) { throw "Packaged application smoke test failed with exit code $LASTEXITCODE" }

$version = python -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])"
if ($LASTEXITCODE -ne 0) { throw "Failed to read the project version with exit code $LASTEXITCODE" }
$version = $version.Trim()
if (-not $version) { throw "Project version is empty" }

$distributionDir = Join-Path $projectRoot "dist\NestingStudio"
$archive = Join-Path $projectRoot "dist\NestingStudio-$version-windows-x64.zip"
$archiveScript = Join-Path $projectRoot "scripts\create_release_archive.py"

Write-Host "Creating release archive..."
python $archiveScript $distributionDir $archive
if ($LASTEXITCODE -ne 0) { throw "Failed to create the release archive with exit code $LASTEXITCODE" }
if (-not (Test-Path -LiteralPath $archive)) { throw "Release archive was not created: $archive" }
$archiveSize = (Get-Item -LiteralPath $archive).Length
if ($archiveSize -le 0) { throw "Release archive is empty: $archive" }

$hash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
$hashFile = "$archive.sha256"
$hashLine = "$hash  $([System.IO.Path]::GetFileName($archive))"
$utf8 = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($hashFile, $hashLine + [Environment]::NewLine, $utf8)

Write-Host ("Release package: {0} ({1:N1} MB)" -f $archive, ($archiveSize / 1MB))
Write-Host ("SHA256: {0}" -f $hash)