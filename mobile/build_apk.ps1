$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$BuildRoot = if ($env:ANDROID_BUILD_ROOT) { $env:ANDROID_BUILD_ROOT } else { "C:\AndroidBuild" }
$Sdk = Join-Path $BuildRoot "sdk"
$BuildTools = Join-Path $Sdk "build-tools\34.0.0"
$Platform = Join-Path $Sdk "platforms\android-34"
$AndroidJar = Join-Path $Platform "android.jar"
$Py = if ($env:ART_RADAR_PYTHON) { $env:ART_RADAR_PYTHON } else { "python" }
$KsPass = $env:ART_RADAR_KS_PASS
$KsAlias = if ($env:ART_RADAR_KS_ALIAS) { $env:ART_RADAR_KS_ALIAS } else { "artradar" }

if (-not (Test-Path $AndroidJar)) { throw "缺少 android.jar: $AndroidJar" }
if (-not (Test-Path (Join-Path $BuildTools "aapt2.exe"))) { throw "缺少 build-tools" }

$Jdk = $null
foreach ($d in Get-ChildItem $BuildRoot -Directory) {
    if (Test-Path (Join-Path $d.FullName "bin\javac.exe")) { $Jdk = $d.FullName; break }
}
if (-not $Jdk) { throw "缺少 JDK（需要 bin\javac.exe）" }
$env:JAVA_HOME = $Jdk
$env:PATH = "$Jdk\bin;$BuildTools;$env:PATH"

$Work = Join-Path $Root "build"
Remove-Item $Work -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path (Join-Path $Work "gen"), (Join-Path $Work "classes"), (Join-Path $Work "dex") | Out-Null

Write-Host "1/7 aapt2 compile"
& (Join-Path $BuildTools "aapt2.exe") compile --dir (Join-Path $Root "res") -o (Join-Path $Work "res.zip")
if ($LASTEXITCODE -ne 0) { throw "aapt2 compile failed" }

Write-Host "2/7 aapt2 link"
& (Join-Path $BuildTools "aapt2.exe") link -o (Join-Path $Work "app-unsigned.apk") `
    -I $AndroidJar --manifest (Join-Path $Root "AndroidManifest.xml") `
    --java (Join-Path $Work "gen") --auto-add-overlay `
    --min-sdk-version 23 --target-sdk-version 34 --version-code 1 --version-name 1.0 `
    (Join-Path $Work "res.zip")
if ($LASTEXITCODE -ne 0) { throw "aapt2 link failed" }

Write-Host "3/7 javac"
$sources = @(Get-ChildItem (Join-Path $Root "src") -Recurse -Filter *.java | ForEach-Object { $_.FullName })
$sources += (Join-Path $Work "gen\com\artradar\app\R.java")
& (Join-Path $Jdk "bin\javac.exe") -encoding UTF-8 -source 8 -target 8 `
    -classpath $AndroidJar -d (Join-Path $Work "classes") $sources
if ($LASTEXITCODE -ne 0) { throw "javac failed" }

Write-Host "4/7 d8"
$classes = @(Get-ChildItem (Join-Path $Work "classes") -Recurse -Filter *.class | ForEach-Object { $_.FullName })
& (Join-Path $BuildTools "d8.bat") --lib $AndroidJar --min-api 23 --output (Join-Path $Work "dex") $classes
if ($LASTEXITCODE -ne 0) { throw "d8 failed" }

Write-Host "5/7 add classes.dex"
& $Py -c "import zipfile,sys; z=zipfile.ZipFile(sys.argv[1],'a'); z.write(sys.argv[2],'classes.dex'); z.close()" `
    (Join-Path $Work "app-unsigned.apk") (Join-Path $Work "dex\classes.dex")

Write-Host "6/7 zipalign"
& (Join-Path $BuildTools "zipalign.exe") -f 4 (Join-Path $Work "app-unsigned.apk") (Join-Path $Work "app-aligned.apk")
if ($LASTEXITCODE -ne 0) { throw "zipalign failed" }

Write-Host "7/7 sign"
$Ks = Join-Path $Root "artradar-release.keystore"
if (-not $KsPass) { throw "请先设置 keystore 密码：`$env:ART_RADAR_KS_PASS = '你的密码'" }
if (-not (Test-Path $Ks)) {
    & (Join-Path $Jdk "bin\keytool.exe") -genkeypair -keystore $Ks -storepass $KsPass `
        -keypass $KsPass -alias $KsAlias -keyalg RSA -keysize 2048 -validity 10000 `
        -dname "CN=ArtRadar, O=ArtRadar, C=CN"
    if ($LASTEXITCODE -ne 0) { throw "keytool failed" }
}
$Out = Join-Path $Root "ArtRadarMobile.apk"
& (Join-Path $BuildTools "apksigner.bat") sign --ks $Ks --ks-pass "pass:$KsPass" `
    --key-pass "pass:$KsPass" --out $Out (Join-Path $Work "app-aligned.apk")
if ($LASTEXITCODE -ne 0) { throw "apksigner failed" }
& (Join-Path $BuildTools "apksigner.bat") verify $Out
if ($LASTEXITCODE -ne 0) { throw "apksigner verify failed" }

Get-Item $Out | Select-Object FullName, Length | Format-List
