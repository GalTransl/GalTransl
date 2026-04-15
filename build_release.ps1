# GalTransl Windows 发布版构建脚本 (PowerShell)
# 
# 用法:
#   .\build_release.ps1                # 构建发布版
#   .\build_release.ps1 -SkipFrontend  # 跳过前端构建
#   .\build_release.ps1 -SkipBackend   # 跳过后端构建
#   .\build_release.ps1 -Clean         # 构建前清理旧产物
#   .\build_release.ps1 -NoZip         # 不创建 zip 压缩包
#
# 前提:
#   - Node.js + npm
#   - Rust + Cargo (Tauri 2.x 需要)
#   - Python 3.11+ + pip
#   - PyInstaller (pip install pyinstaller)

param(
    [switch]$SkipFrontend,
    [switch]$SkipBackend,
    [switch]$Clean,
    [switch]$NoZip
)

$ErrorActionPreference = "Stop"

# ─── 配置 ───────────────────────────────────────────────

$Root = Resolve-Path (Split-Path -Parent $MyInvocation.MyCommand.Path)
$DesktopDir = Join-Path $Root "desktop"
$TauriDir = Join-Path $DesktopDir "src-tauri"
$ReleaseDir = Join-Path $Root "release"
$PluginsDir = Join-Path $Root "plugins"
$VenvDir = Join-Path $Root ".venv-build"

# 读取版本号
$InitPy = Get-Content (Join-Path $Root "GalTransl\__init__.py") -Encoding UTF8
$VersionLine = $InitPy | Where-Object { $_ -match 'GALTRANSL_VERSION' }
$Version = if ($VersionLine -match '"([^"]+)"') { $Matches[1] } else { "0.0.0" }

$BuildName = "GalTransl-Desktop-$Version-win64"
$BuildDir = Join-Path $ReleaseDir $BuildName
$ZipName = "$BuildName.zip"

Write-Host ""
Write-Host "GalTransl v$Version Windows 发布版构建" -ForegroundColor Cyan
Write-Host "输出目录: $ReleaseDir"
Write-Host ""

# ─── 清理 ───────────────────────────────────────────────

if ($Clean) {
    Write-Host "清理旧产物..." -ForegroundColor Yellow
    if (Test-Path $ReleaseDir) { Remove-Item $ReleaseDir -Recurse -Force }
    $TauriTarget = Join-Path $TauriDir "target\release"
    if (Test-Path $TauriTarget) {
        Write-Host "  清理 Tauri release target..."
        Remove-Item $TauriTarget -Recurse -Force
    }
    $FeDist = Join-Path $DesktopDir "dist"
    if (Test-Path $FeDist) { Remove-Item $FeDist -Recurse -Force }
    $PyBuild = Join-Path $Root "build"
    $PyDist = Join-Path $Root "dist"
    if (Test-Path $PyBuild) { Remove-Item $PyBuild -Recurse -Force }
    if (Test-Path $PyDist) { Remove-Item $PyDist -Recurse -Force }
    if (Test-Path $VenvDir) {
        Write-Host "  清理构建虚拟环境: $VenvDir"
        Remove-Item $VenvDir -Recurse -Force
    }
    Write-Host "  清理完成"
}

# ─── 构建前端 ───────────────────────────────────────────

$FrontendExe = $null
$BundleDir = $null

if (-not $SkipFrontend) {
    Write-Host ""
    Write-Host "═══ 构建前端 (Tauri Desktop) ═══" -ForegroundColor Green

    # 安装前端依赖
    if (-not (Test-Path (Join-Path $DesktopDir "node_modules"))) {
        Write-Host "  安装前端依赖..."
        Push-Location $DesktopDir
        npm install
        Pop-Location
    }

    # 执行 Tauri 构建
    Write-Host "  执行 tauri build..."
    Push-Location $DesktopDir
    npx tauri build
    Pop-Location

    $FrontendExe = Join-Path $TauriDir "target\release\GalTransl Desktop.exe"
    $BundleDir = Join-Path $TauriDir "target\release\bundle"

    if (-not (Test-Path $FrontendExe)) {
        Write-Host "前端 exe 未找到: $FrontendExe" -ForegroundColor Red
        exit 1
    }
    Write-Host "  前端 exe 构建成功: $FrontendExe" -ForegroundColor Green
} else {
    $FrontendExe = Join-Path $TauriDir "target\release\GalTransl Desktop.exe"
    $BundleDir = Join-Path $TauriDir "target\release\bundle"
    if (-not (Test-Path $FrontendExe)) {
        Write-Host "跳过前端构建但未找到已有 exe: $FrontendExe" -ForegroundColor Red
        exit 1
    }
    Write-Host "跳过前端构建，使用已有 exe: $FrontendExe"
}

# ─── 构建后端 ───────────────────────────────────────────

$BackendExe = $null

if (-not $SkipBackend) {
    Write-Host ""
    Write-Host "═══ 构建后端 (PyInstaller) ═══" -ForegroundColor Green

    # 虚拟环境路径
    $VenvPython = Join-Path $VenvDir "Scripts\python.exe"
    $VenvPip = Join-Path $VenvDir "Scripts\pip.exe"

    # 创建虚拟环境（如果不存在）
    if (-not (Test-Path $VenvPython)) {
        Write-Host "  创建构建虚拟环境: $VenvDir"
        python -m venv $VenvDir
    }

    # 安装依赖
    $ReqFile = Join-Path $Root "requirements.txt"
    if (Test-Path $ReqFile) {
        Write-Host "  安装项目依赖到虚拟环境..."
        & $VenvPip install -r $ReqFile --quiet
    } else {
        Write-Host "  未找到 requirements.txt，跳过依赖安装"
    }

    # 安装 PyInstaller
    Write-Host "  安装 PyInstaller..."
    & $VenvPip install pyinstaller --quiet

    $BackendEntry = Join-Path $Root "run_backend.py"
    $PyDist = Join-Path $Root "dist"

    # PyInstaller 打包
    $pyinstallerArgs = @(
        "-m", "PyInstaller",
        "--onefile",
        "--noupx",
        "--noconfirm",
        "--clean",
        "--name", "galtransl_backend",
        "--hidden-import=GalTransl",
        "--hidden-import=GalTransl.server",
        "--hidden-import=GalTransl.Service",
        "--hidden-import=GalTransl.Runner",
        "--hidden-import=GalTransl.Cache",
        "--hidden-import=GalTransl.CSentense",
        "--hidden-import=GalTransl.CSerialize",
        "--hidden-import=GalTransl.CSplitter",
        "--hidden-import=GalTransl.Dictionary",
        "--hidden-import=GalTransl.ConfigHelper",
        "--hidden-import=GalTransl.AppSettings",
        "--hidden-import=GalTransl.COpenAI",
        "--hidden-import=GalTransl.Name",
        "--hidden-import=GalTransl.i18n",
        "--hidden-import=GalTransl.Problem",
        "--hidden-import=GalTransl.Utils",
        "--hidden-import=GalTransl.TerminalOutput",
        "--hidden-import=GalTransl.yapsy",
        "--hidden-import=GalTransl.Frontend",
        "--hidden-import=GalTransl.Frontend.LLMTranslate",
        "--collect-data=GalTransl",
        "--distpath", $PyDist,
        "--workpath", (Join-Path $Root "build"),
        $BackendEntry
    )

    Push-Location $Root
    & $VenvPython @pyinstallerArgs
    Pop-Location

    $BackendExe = Join-Path $PyDist "galtransl_backend.exe"
    if (-not (Test-Path $BackendExe)) {
        Write-Host "后端 exe 未找到: $BackendExe" -ForegroundColor Red
        exit 1
    }
    Write-Host "  后端 exe 构建成功: $BackendExe" -ForegroundColor Green
} else {
    $BackendExe = Join-Path $Root "dist\galtransl_backend.exe"
    if (-not (Test-Path $BackendExe)) {
        Write-Host "跳过后端构建但未找到已有 exe: $BackendExe" -ForegroundColor Red
        exit 1
    }
    Write-Host "跳过后端构建，使用已有 exe: $BackendExe"
}

# ─── 组装发布目录 ───────────────────────────────────────

Write-Host ""
Write-Host "═══ 组装发布包 ═══" -ForegroundColor Green

New-Item -ItemType Directory -Path $BuildDir -Force | Out-Null

# 1. 复制前端 exe
$DstExe = Join-Path $BuildDir "GalTransl Desktop.exe"
Copy-Item $FrontendExe $DstExe -Force
Write-Host "  复制前端 exe -> $DstExe"

# 2. 复制 NSIS 安装包（如果有）
$NsisDir = Join-Path $BundleDir "nsis"
if (Test-Path $NsisDir) {
    $NsisExe = Get-ChildItem (Join-Path $NsisDir "*.exe") -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($NsisExe) {
        $DstInstaller = Join-Path $BuildDir $NsisExe.Name
        Copy-Item $NsisExe.FullName $DstInstaller -Force
        Write-Host "  复制 NSIS 安装包 -> $DstInstaller"
    }
}

# 3. 复制后端
$DstBackendDir = Join-Path $BuildDir "backend"
New-Item -ItemType Directory -Path $DstBackendDir -Force | Out-Null
Copy-Item $BackendExe (Join-Path $DstBackendDir "galtransl_backend.exe") -Force
Write-Host "  复制后端 exe -> backend\galtransl_backend.exe"

# 4. 复制插件
if (Test-Path $PluginsDir) {
    $DstPlugins = Join-Path $BuildDir "plugins"
    # 排除 __pycache__ 和 .pyc
    Get-ChildItem $PluginsDir -Directory | ForEach-Object {
        $dst = Join-Path $DstPlugins $_.Name
        # 用 robocopy 复制，排除 __pycache__
        robocopy $_.FullName $dst /E /XD "__pycache__" /XF "*.pyc" /NFL /NDL /NJH /NJS /NC /NS /NP | Out-Null
    }
    Write-Host "  复制插件目录 -> plugins\"
}

# 5. 复制 CLI 入口
$CliEntry = Join-Path $Root "run_GalTransl.py"
if (Test-Path $CliEntry) {
    Copy-Item $CliEntry (Join-Path $BuildDir "run_GalTransl.py") -Force
    Write-Host "  复制 CLI 入口 -> run_GalTransl.py"
}

# 6. 生成启动脚本
$LauncherBat = Join-Path $BuildDir "启动 GalTransl Desktop.bat"
$LauncherContent = @"
@echo off
chcp 65001 >nul
echo 正在启动 GalTransl Desktop...
start "" "backend\galtransl_backend.exe"
timeout /t 2 /nobreak >nul
start "" "GalTransl Desktop.exe"
"@
Set-Content -Path $LauncherBat -Value $LauncherContent -Encoding UTF8
Write-Host "  生成启动脚本 -> 启动 GalTransl Desktop.bat"

# 7. 生成 README
$ReadmePath = Join-Path $BuildDir "README.txt"
$ReadmeContent = @"
GalTransl Desktop v$Version
========================================

启动方式:
  方式1: 双击「启动 GalTransl Desktop.bat」同时启动前后端
  方式2: 先运行 backend\galtransl_backend.exe，再运行 GalTransl Desktop.exe

CLI 模式:
  python run_GalTransl.py

插件目录: plugins\
"@
Set-Content -Path $ReadmePath -Value $ReadmeContent -Encoding UTF8
Write-Host "  生成 README -> README.txt"

Write-Host ""
Write-Host "发布包组装完成: $BuildDir" -ForegroundColor Green

# ─── 创建压缩包 ────────────────────────────────────────

if (-not $NoZip) {
    Write-Host ""
    Write-Host "═══ 创建压缩包 ═══" -ForegroundColor Green
    $ZipPath = Join-Path $ReleaseDir $ZipName
    if (Test-Path $ZipPath) { Remove-Item $ZipPath -Force }
    
    # 使用 .NET 的 ZipFile 类
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [System.IO.Compression.ZipFile]::CreateFromDirectory($BuildDir, $ZipPath)
    
    Write-Host "  压缩包已创建: $ZipPath" -ForegroundColor Green
}

# ─── 完成 ───────────────────────────────────────────────

Write-Host ""
Write-Host "✅ 构建完成！发布包位于: $BuildDir" -ForegroundColor Green
if (-not $NoZip) {
    Write-Host "   压缩包: $(Join-Path $ReleaseDir $ZipName)"
}
