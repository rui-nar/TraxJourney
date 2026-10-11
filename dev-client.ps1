# Start Flutter web dev server (port 5500)
# Set MAPBOX_TOKEN in your environment or pass it as a parameter.
# -Perf runs a profile build with the frame-timing recorder + performance
#   overlay enabled (measure scroll/jank — see lib/src/core/perf_timing.dart).
param(
  [string]$MapboxToken = $env:MAPBOX_TOKEN,
  [string]$ApiBaseUrl = 'http://localhost:8000',
  [switch]$Perf,
  [switch]$NoMap,
  [switch]$RasterMap
)
if (-not $MapboxToken) { Write-Error "MAPBOX_TOKEN not set"; exit 1 }
Set-Location "$PSScriptRoot\flutter_client"

# -Perf builds in profile mode (release-grade rendering, no debug-mode JIT/assert
# pollution) with the frame-timing recorder + overlay on. Point -ApiBaseUrl at a
# real instance to measure against a heavy production trip's data.
$perfArgs = @()
if ($Perf -or $NoMap) {
  $perfArgs = @('--profile', '--dart-define=PERF_TIMING=true')
  Write-Host '[dev-client] PERF mode ON: profile build + frame-timing recorder + overlay.' -ForegroundColor Cyan
  Write-Host '[dev-client]   -> [perf] logs print to the browser DevTools console (F12), not here.' -ForegroundColor Cyan
  Write-Host '[dev-client]   -> scroll the activity panel; idle 2s windows are skipped.' -ForegroundColor Cyan
  if ($NoMap) {
    $perfArgs += '--dart-define=PERF_NO_MAP=true'
    Write-Host '[dev-client]   -> NO-MAP diagnostic: map replaced by a flat placeholder.' -ForegroundColor Yellow
  }
}

# -RasterMap switches the basemap to cached raster tiles (smooth scroll, soft
# mid-zoom) to A-B compare against the default per-frame vector rendering.
if ($RasterMap) {
  $perfArgs += '--dart-define=MAP_TILE_MODE=raster'
  Write-Host '[dev-client] MAP_TILE_MODE=raster (cached bitmap tiles).' -ForegroundColor Yellow
}

# web/flutter_bootstrap.js points the engine at fallback-fonts/ (#601, #602);
# keep a local copy in web/ (git-ignored) so emoji and other scripts render in
# dev too. Only missing files are fetched, so after the first run it is quick.
dart run tool/mirror_fallback_fonts.dart web
if ($LASTEXITCODE -ne 0) { Write-Host '[dev-client] Could not mirror the fallback fonts; emoji may not render.' -ForegroundColor Yellow }

flutter run -d chrome --web-port 5500 `
  --dart-define=API_BASE_URL=$ApiBaseUrl `
  --dart-define=MAPBOX_TOKEN=$MapboxToken `
  @perfArgs
