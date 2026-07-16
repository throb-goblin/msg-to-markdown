$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$source = Join-Path $projectRoot "LaunchMsgToMarkdown.cs"
$output = Join-Path $projectRoot "MSG to Markdown.exe"
$iconPng = Join-Path $projectRoot "icon_light.png"
$iconIco = Join-Path $projectRoot "icon_light.ico"
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (Test-Path -LiteralPath $iconPng) {
    & $python -c "from PIL import Image; from pathlib import Path; src=Path(r'$iconPng'); dst=Path(r'$iconIco'); im=Image.open(src).convert('RGBA'); im.save(dst, sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])"
}

$cscCandidates = @(
    (Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"),
    (Join-Path $env:WINDIR "Microsoft.NET\Framework\v4.0.30319\csc.exe")
)
$csc = $cscCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $csc) {
    throw "Could not find the .NET Framework C# compiler."
}

if (Test-Path -LiteralPath $output) {
    Remove-Item -LiteralPath $output -Force
}

$arguments = @(
    "/nologo",
    "/target:winexe",
    "/out:$output",
    "/reference:System.Windows.Forms.dll",
    "/reference:System.Core.dll"
)
if (Test-Path -LiteralPath $iconIco) {
    $arguments += "/win32icon:$iconIco"
}
$arguments += $source

& $csc @arguments
if ($LASTEXITCODE -ne 0) {
    throw "Launcher build failed with exit code $LASTEXITCODE."
}

Write-Host "Created $output"
