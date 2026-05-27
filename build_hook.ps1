# Build pub_streamer_hook.dll (x64) and pub_streamer_hook_x86.dll (x86).
# Run from any directory; output lands in Pub-Streamer/hook/.

$ErrorActionPreference = "Stop"

$vcvars64 = "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat"
$vcvars32 = "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars32.bat"
$hookDir  = "$PSScriptRoot\hook"
$src      = "$hookDir\hook_audio.cpp"

foreach ($entry in @(
    @{ vcvars = $vcvars64; out = "$hookDir\pub_streamer_hook.dll";     label = "x64" },
    @{ vcvars = $vcvars32; out = "$hookDir\pub_streamer_hook_x86.dll"; label = "x86" }
)) {
    if (-not (Test-Path $entry.vcvars)) {
        Write-Error "vcvars not found: $($entry.vcvars)"
        exit 1
    }
    if (-not (Test-Path $src)) {
        Write-Error "Source not found: $src"
        exit 1
    }

    Write-Host "Building $($entry.label): $($entry.out) ..."

    $cmd = "call `"$($entry.vcvars)`" >nul 2>&1 && cl /nologo /LD /O2 /EHsc /W3 `"$src`" /Fe:`"$($entry.out)`" /link ole32.lib uuid.lib /SUBSYSTEM:WINDOWS"

    $proc = Start-Process -FilePath "cmd.exe" `
        -ArgumentList "/c", $cmd `
        -NoNewWindow -Wait -PassThru

    if ($proc.ExitCode -ne 0) {
        Write-Error "$($entry.label) compilation failed (exit $($proc.ExitCode))"
        exit $proc.ExitCode
    }

    if (Test-Path $entry.out) {
        Write-Host "OK: $($entry.out)"
    } else {
        Write-Error "DLL not produced despite cl.exe success — check output above."
        exit 1
    }
}

Write-Host ""
Write-Host "Both DLLs built successfully."
