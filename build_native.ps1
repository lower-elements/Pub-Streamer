# build_native.ps1 — Build audio_hook32.dll, audio_hook64.dll, injector32.exe
#
# Requires:
#   - Visual Studio 2022 (any edition: BuildTools/Community/Professional/
#     Enterprise) with the "Desktop development with C++" workload.
#   - CMake — either standalone in PATH, or the copy bundled with the VS
#     install (found automatically via the "C++ CMake tools" component).
#
# VS install location and CMake are located dynamically via vswhere.exe,
# which ships with every VS 2022 edition at a fixed path. This avoids
# hardcoding a specific edition's install path.
#
# Output lands in native\dist\:
#   audio_hook32.dll   injected into 32-bit (WOW64) target processes
#   audio_hook64.dll   injected into 64-bit target processes
#   injector32.exe     32-bit helper spawned by Python for WOW64 targets
#
# Usage: .\build_native.ps1

$ErrorActionPreference = "Stop"

$vswhere = "C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe"
if (-not (Test-Path $vswhere)) {
    Write-Error "vswhere.exe not found at $vswhere. Install Visual Studio 2022 (any edition) with the 'Desktop development with C++' workload."
    exit 1
}

$vsInstallPath = & $vswhere -latest -products * `
    -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 `
    -property installationPath
if (-not $vsInstallPath) {
    Write-Error "No Visual Studio 2022 install found with the C++ workload (Microsoft.VisualStudio.Component.VC.Tools.x86.x64)."
    exit 1
}
Write-Host "Found Visual Studio at: $vsInstallPath"

$vcvars64 = Join-Path $vsInstallPath "VC\Auxiliary\Build\vcvars64.bat"
$vcvars32 = Join-Path $vsInstallPath "VC\Auxiliary\Build\vcvars32.bat"
if (-not (Test-Path $vcvars64) -or -not (Test-Path $vcvars32)) {
    Write-Error "vcvars64.bat/vcvars32.bat not found under $vsInstallPath. C++ workload may be incomplete."
    exit 1
}

# Locate cmake.exe: prefer PATH, fall back to the copy bundled with VS
# (installed via the "C++ CMake tools for Windows" component).
$cmakeCmd = Get-Command cmake.exe -ErrorAction SilentlyContinue
if ($cmakeCmd) {
    $cmakeExe = $cmakeCmd.Source
} else {
    $bundledCMake = Join-Path $vsInstallPath "Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe"
    if (-not (Test-Path $bundledCMake)) {
        Write-Error "cmake.exe not found in PATH or under $vsInstallPath. Install CMake or the 'C++ CMake tools for Windows' VS component."
        exit 1
    }
    $cmakeExe = $bundledCMake
}
Write-Host "Using CMake at: $cmakeExe"

$nativeDir = "$PSScriptRoot\native"
$distDir   = "$nativeDir\dist"

if (-not (Test-Path $distDir)) {
    New-Item -ItemType Directory -Path $distDir | Out-Null
}

function Build-CMake {
    param([string]$vcvars, [string]$arch, [string]$label, [string]$subdir)

    $buildDir = "$nativeDir\$subdir\build_$arch"
    if (-not (Test-Path $buildDir)) {
        New-Item -ItemType Directory -Path $buildDir | Out-Null
    }

    $cmakeArch = if ($arch -eq "x64") { "x64" } else { "Win32" }
    $src = "$nativeDir\$subdir"

    $cmd = @"
call "$vcvars" >nul 2>&1 && "$cmakeExe" -S "$src" -B "$buildDir" -A $cmakeArch -DCMAKE_BUILD_TYPE=Release >nul && "$cmakeExe" --build "$buildDir" --config Release
"@
    Write-Host "Building $label ($arch)..."
    $proc = Start-Process -FilePath "cmd.exe" -ArgumentList "/c", $cmd -NoNewWindow -Wait -PassThru
    if ($proc.ExitCode -ne 0) {
        Write-Error "$label ($arch) build failed"
        exit $proc.ExitCode
    }
    Write-Host "OK: $label ($arch)"
}

# audio_hook64.dll  (x64)
Build-CMake -vcvars $vcvars64 -arch "x64" -label "audio_hook64.dll" -subdir "audio_hook"

# audio_hook32.dll  (x86)
Build-CMake -vcvars $vcvars32 -arch "x86" -label "audio_hook32.dll" -subdir "audio_hook"

# injector32.exe    (x86, standalone)
Build-CMake -vcvars $vcvars32 -arch "x86" -label "injector32.exe"   -subdir "injector"

Write-Host ""
Write-Host "Build complete. Outputs in: $distDir"
Get-ChildItem $distDir | Select-Object Name, Length
