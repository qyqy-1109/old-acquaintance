# Windows location probe used by app/utils/geo.py (System.Device.Location).
#
# KEEP THIS FILE PURE ASCII. Windows PowerShell 5.1 decodes .ps1 as GBK, so any
# non-ASCII byte here breaks parsing (a test asserts this).
#
# Why not WinRT Geolocator: its projection in Windows PowerShell 5.1 has no
# RequestAccessAsync method (verified failure), so we go through the .NET
# GeoCoordinateWatcher, which sits on the same Windows location platform.
# Decimal places: 4 (about 10 m) is what geo.py rounds to before sending to a
# third-party reverse geocoder; this script prints full precision, and geo.py
# keeps it local only.
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$out = @{ ok = $false; error = ''; lat = $null; lon = $null; acc = $null; source = '' }
try {
    Add-Type -AssemblyName System.Device | Out-Null
} catch {
    $out.error = "cannot load System.Device: $($_.Exception.Message)"
    $out | ConvertTo-Json -Compress
    exit 0
}
try {
    $watcher = New-Object System.Device.Location.GeoCoordinateWatcher
    $started = $watcher.TryStart($false, [TimeSpan]::FromSeconds(15))
    $deadline = (Get-Date).AddSeconds(20)
    while ((Get-Date) -lt $deadline) {
        if ($watcher.Position.Location.IsUnknown -eq $false) { break }
        Start-Sleep -Milliseconds 400
    }
    $loc = $watcher.Position.Location
    if ($loc.IsUnknown) {
        $out.error = "position unknown (TryStart=$started permission=$($watcher.Permission))"
    } else {
        $out.ok = $true
        $out.lat = $loc.Latitude
        $out.lon = $loc.Longitude
        $out.acc = [math]::Round($loc.HorizontalAccuracy, 0)
        $out.source = "System.Device.Location"
    }
    $watcher.Stop()
} catch {
    $out.error = $_.Exception.Message
}
$out | ConvertTo-Json -Compress
