# build_native.ps1 — Build audio_hook32.dll, audio_hook64.dll, injector32.exe
#
# Requires:
#   - CMake in PATH (installed to C:\Program Files\CMake\bin)
#   - Visual Studio Build Tools 2022 with C++ workload
#
# Output lands in native\dist\:
#   audio_hook32.dll   injected into 32-bit (WOW64) target processes
#   audio_hook64.dll   injected into 64-bit target processes
#   injector32.exe     32-bit helper spawned by Python for WOW64 targets
#
# Usage: .\build_native.ps1

$ErrorActionPreference = "Stop"

$vcvars64 = "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat"
$vcvars32 = "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars32.bat"
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
call "$vcvars" >nul 2>&1 && cmake -S "$src" -B "$buildDir" -A $cmakeArch -DCMAKE_BUILD_TYPE=Release >nul && cmake --build "$buildDir" --config Release
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
