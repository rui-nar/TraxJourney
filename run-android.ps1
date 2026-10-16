<#
.SYNOPSIS
    Build, install and run the Android app on an emulator (issue #136).

.DESCRIPTION
    Everything needed to see the Android app running, in one command: resolve a
    JDK gradle can use, boot the AVD if nothing is attached, build with the
    right dart-defines, install, launch, and stream the logs.

    A native build needs API_BASE_URL baked in — unlike web, there is no origin
    to fall back on — so the wrong default here shows up as an app that starts
    and then fails every request.

.EXAMPLE
    .\run-android.ps1
    Debug build against production (https://traxjourney.com), with hot reload.

.EXAMPLE
    .\run-android.ps1 -Local
    Debug build against the dev server on this machine (http://10.0.2.2:8000 —
    the emulator's address for the host). Requires the server to be running.

.EXAMPLE
    .\run-android.ps1 -Release
    Release build, installed and launched (no hot reload). Signed with the debug
    key unless flutter_client/android/key.properties exists.

.EXAMPLE
    .\run-android.ps1 -Apk .\traxjourney-v0.48.0.apk
    Install and launch a prebuilt APK — e.g. the one CI attached to a release.

.EXAMPLE
    .\run-android.ps1 -DeepLink "https://traxjourney.com/share/abc123"
    Fire an App Link at the installed app and report its link-verification state.
#>
param(
    [string]$Avd = 'MyPixelEmulator',
    [switch]$Local,
    [switch]$Release,
    [string]$Apk,
    [string]$DeepLink,
    [string]$ApiBaseUrl,
    [string]$MapboxToken = $env:MAPBOX_TOKEN,
    [switch]$NoWindowsCertStore
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$PACKAGE = 'com.traxjourney.app'
$CLIENT = Join-Path $PSScriptRoot 'flutter_client'

function Step([string]$msg) { Write-Host "`n>> $msg" -ForegroundColor Cyan }
function Note([string]$msg) { Write-Host "   $msg" -ForegroundColor DarkGray }
function Warn([string]$msg) { Write-Host "   $msg" -ForegroundColor Yellow }
function Die([string]$msg) { Write-Host "`nERROR: $msg" -ForegroundColor Red; exit 1 }

# ── Android SDK tools ─────────────────────────────────────────────────────────
# ANDROID_HOME is not always set; local.properties (written by the Flutter tool)
# is the fallback the Gradle build itself uses.
function Get-AndroidSdk {
    foreach ($candidate in @($env:ANDROID_HOME, $env:ANDROID_SDK_ROOT)) {
        if ($candidate -and (Test-Path $candidate)) { return $candidate }
    }
    $localProps = Join-Path $CLIENT 'android\local.properties'
    if (Test-Path $localProps) {
        $line = Select-String -Path $localProps -Pattern '^sdk\.dir=(.+)$' | Select-Object -First 1
        if ($line) { return $line.Matches[0].Groups[1].Value -replace '\\\\', '\' }
    }
    return $null
}

$sdk = Get-AndroidSdk
function Sdk-Tool([string]$relative, [string]$onPath) {
    if ($sdk) {
        $full = Join-Path $sdk $relative
        if (Test-Path $full) { return $full }
    }
    $cmd = Get-Command $onPath -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    Die "Cannot find $onPath. Set ANDROID_HOME, or add the Android SDK's platform-tools and emulator to PATH."
}

$adb = Sdk-Tool 'platform-tools\adb.exe' 'adb'
$emulator = Sdk-Tool 'emulator\emulator.exe' 'emulator'

# ── JDK ───────────────────────────────────────────────────────────────────────
# Gradle 9.3.1 (flutter_client/android/gradle/wrapper) runs on JDK 17-25; AGP
# needs 17+. Revisit $JDK_MAX when the wrapper moves. The JDK CI builds with is
# flutter_client/android/.java-version (setup-java reads the same file), and is
# preferred here so a local build matches CI. A JAVA_HOME left pointing at an
# uninstalled JDK — what happens after a JDK update bumps the version in the
# path — fails the build with a message that says nothing about JAVA_HOME, so
# check it explicitly.
$JDK_MIN = 17
$JDK_MAX = 25
$JDK_CI = [int](Get-Content (Join-Path $CLIENT 'android\.java-version') -TotalCount 1).Trim()

function Get-JavaMajor([string]$javaHome) {
    $exe = Join-Path $javaHome 'bin\java.exe'
    if (-not (Test-Path $exe)) { return $null }
    $out = & $exe -version 2>&1 | Out-String
    if ($out -match 'version "(\d+)') { return [int]$Matches[1] }
    return $null
}

function Resolve-Jdk {
    $roots = @(
        'C:\Program Files\Microsoft',
        'C:\Program Files\Eclipse Adoptium',
        'C:\Program Files\Java',
        'C:\Program Files\Android\Android Studio'
    )
    $candidates = [System.Collections.Generic.List[string]]::new()
    if ($env:JAVA_HOME) { $candidates.Add($env:JAVA_HOME) }
    foreach ($root in $roots) {
        if (-not (Test-Path $root)) { continue }
        if (Test-Path (Join-Path $root 'jbr\bin\java.exe')) { $candidates.Add((Join-Path $root 'jbr')) }
        Get-ChildItem $root -Directory -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match 'jdk' } |
            ForEach-Object { $candidates.Add($_.FullName) }
    }

    $usable = @()
    foreach ($candidate in $candidates) {
        $major = Get-JavaMajor $candidate
        if ($major -ne $null -and $major -ge $JDK_MIN -and $major -le $JDK_MAX) {
            $usable += [pscustomobject]@{ Path = $candidate; Major = $major }
        }
    }
    if (-not $usable) { return $null }
    # Prefer what CI builds with, then the newest supported.
    $preferred = $usable | Where-Object { $_.Major -eq $JDK_CI } | Select-Object -First 1
    if ($preferred) { return $preferred }
    return $usable | Sort-Object Major -Descending | Select-Object -First 1
}

Step 'Resolving a JDK for Gradle'
if ($env:JAVA_HOME -and -not (Test-Path (Join-Path $env:JAVA_HOME 'bin\java.exe'))) {
    Warn "JAVA_HOME points at a JDK that is not there: $env:JAVA_HOME"
    Warn '  (a JDK update renames the directory; the variable keeps the old path)'
    Warn "  Fix permanently:  setx JAVA_HOME `"<path to a JDK $JDK_CI>`""
}
$jdk = Resolve-Jdk
if (-not $jdk) {
    Die "No JDK $JDK_MIN-$JDK_MAX found. Install Temurin/Microsoft JDK $JDK_CI and set JAVA_HOME."
}
$env:JAVA_HOME = $jdk.Path
Note "JDK $($jdk.Major) at $($jdk.Path)"

# A JDK ships its own certificate authorities and ignores the Windows store, so
# on a machine behind TLS inspection (corporate proxy, some antivirus) every
# Gradle dependency download dies with "PKIX path building failed" while every
# browser on the same machine is fine. Pointing the JVM at the Windows store
# gives Gradle exactly the trust the OS already grants — for this process only,
# nothing is written to gradle.properties. -NoWindowsCertStore opts out.
if (-not $NoWindowsCertStore) {
    $env:GRADLE_OPTS = (@($env:GRADLE_OPTS, '-Djavax.net.ssl.trustStoreType=WINDOWS-ROOT') |
        Where-Object { $_ }) -join ' '
    Note 'Gradle will use the Windows certificate store (-NoWindowsCertStore to skip)'
}

# ── Emulator ──────────────────────────────────────────────────────────────────
function Get-EmulatorSerial {
    $lines = & $adb devices | Select-Object -Skip 1
    foreach ($line in $lines) {
        if ($line -match '^(emulator-\d+)\s+device$') { return $Matches[1] }
    }
    return $null
}

Step 'Checking for a running emulator'
$serial = Get-EmulatorSerial
if ($serial) {
    Note "Already running: $serial"
} else {
    $known = & $emulator -list-avds
    if ($known -notcontains $Avd) {
        Die "AVD '$Avd' not found. Available: $($known -join ', ') — create one in Android Studio's Device Manager, or pass -Avd."
    }
    Note "Starting $Avd (this takes a minute on a cold boot)"
    Start-Process -FilePath $emulator -ArgumentList @('-avd', $Avd) -WindowStyle Minimized
    & $adb wait-for-device
    # wait-for-device returns as soon as adb can talk to it, which is well before
    # the system is usable; installs fail in that window.
    $deadline = (Get-Date).AddMinutes(5)
    while ((Get-Date) -lt $deadline) {
        $booted = (& $adb shell getprop sys.boot_completed 2>$null | Out-String).Trim()
        if ($booted -eq '1') { break }
        Start-Sleep -Seconds 3
    }
    $serial = Get-EmulatorSerial
    if (-not $serial) { Die 'Emulator did not finish booting within 5 minutes.' }
    Note "Booted: $serial"
}

# ── Build configuration ───────────────────────────────────────────────────────
if (-not $ApiBaseUrl) {
    # 10.0.2.2 is the host loopback as seen from inside the emulator; localhost
    # inside the emulator is the emulator itself.
    $ApiBaseUrl = if ($Local) { 'http://10.0.2.2:8000' } else { 'https://traxjourney.com' }
}
if (-not $MapboxToken) {
    Warn 'MAPBOX_TOKEN not set — the app runs, but the basemap will not render.'
    $MapboxToken = ''
}

$defines = @(
    "--dart-define=API_BASE_URL=$ApiBaseUrl",
    "--dart-define=MAPBOX_TOKEN=$MapboxToken"
)

function Start-App {
    Step 'Launching'
    & $adb -s $serial shell monkey -p $PACKAGE -c android.intent.category.LAUNCHER 1 | Out-Null
}

function Send-DeepLink([string]$url) {
    Step "Opening $url"
    # -a VIEW without -p would offer a browser chooser whenever App Links
    # verification has not succeeded (it cannot on a debug-signed build: the
    # debug key's fingerprint is not in assetlinks.json). Naming the package
    # tests the app's own routing regardless.
    & $adb -s $serial shell am start -a android.intent.action.VIEW -c android.intent.category.BROWSABLE -d "'$url'" -p $PACKAGE
    Step 'App Links verification state'
    Note '(a debug build always shows "unverified" — see docs/ANDROID.md)'
    & $adb -s $serial shell pm get-app-links $PACKAGE
}

# ── Install / run ─────────────────────────────────────────────────────────────
if ($Apk) {
    if (-not (Test-Path $Apk)) { Die "APK not found: $Apk" }
    Step "Installing $Apk"
    & $adb -s $serial install -r $Apk
    if ($LASTEXITCODE -ne 0) {
        Die "Install failed. A copy signed with a different key is usually the cause — `adb uninstall $PACKAGE` first."
    }
    Start-App
    if ($DeepLink) { Send-DeepLink $DeepLink }
    Step 'Logs (Ctrl-C to stop)'
    & $adb -s $serial logcat -s flutter:V ActivityManager:I
    exit 0
}

if ($DeepLink -and -not $Release) {
    # Deep-link only: assume the app is already installed, just drive it.
    $installed = (& $adb -s $serial shell pm list packages $PACKAGE | Out-String).Trim()
    if ($installed) {
        Send-DeepLink $DeepLink
        exit 0
    }
    Note 'App not installed yet — building first.'
}

Push-Location $CLIENT
try {
    if ($Release) {
        Step "Building release APK (API_BASE_URL=$ApiBaseUrl)"
        & flutter build apk --release @defines
        if ($LASTEXITCODE -ne 0) { Die 'flutter build apk failed.' }
        $apkPath = Join-Path $CLIENT 'build\app\outputs\flutter-apk\app-release.apk'
        Step 'Installing'
        & $adb -s $serial install -r $apkPath
        if ($LASTEXITCODE -ne 0) {
            Die "Install failed. A copy signed with a different key is usually the cause — ``adb uninstall $PACKAGE`` first."
        }
        Start-App
        if ($DeepLink) { Send-DeepLink $DeepLink }
        Step 'Logs (Ctrl-C to stop)'
        & $adb -s $serial logcat -s flutter:V ActivityManager:I
    } else {
        Step "Running debug build (API_BASE_URL=$ApiBaseUrl)"
        Note 'r = hot reload, R = hot restart, q = quit'
        & flutter run -d $serial @defines
    }
} finally {
    Pop-Location
}
