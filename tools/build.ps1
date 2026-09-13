# 打包 notion-today-widget（Windows PowerShell）
# 用法：在仓库根目录执行  .\tools\build.ps1
#
#   .\tools\build.ps1                      安全模式：dist 里放占位配置，可直接分发
#   .\tools\build.ps1 -IncludeLocalConfig  本机自用：把真实 config.json 拷进 dist（切勿分发）
#
# 为什么默认不放真实 config.json：dist\notion-today-widget\ 是要整体分发的产物，
# 里面塞进含明文令牌的配置，等于每次分发都把自己的 Notion 令牌一起送出去。
# 本机日常使用（含开机自启）确实需要 dist 里有真实配置，所以留了显式开关——
# 但默认必须是安全的那个。

param(
    [switch]$IncludeLocalConfig
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not (Get-Command pyinstaller -ErrorAction SilentlyContinue)) {
    Write-Error 'pyinstaller 未安装。先执行： pip install pyinstaller'
}

Write-Host '==> 清理旧产物'
Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue

# 用仓库里提交的 spec，而不是让 PyInstaller 现场重新生成一个。
# 现场生成的那个 spec 会把本机绝对路径写进去，换台机器就失效。
Write-Host '==> PyInstaller 打包（使用 notion-today-widget.spec）'
pyinstaller --noconfirm --clean notion-today-widget.spec

$out = Join-Path $root 'dist\notion-today-widget'
$realCfg = Join-Path $root 'config.json'
$placeholder = 'ntn_在此填入你的Notion集成令牌'

Write-Host '==> 补拷运行时文件'
Copy-Item app.ico $out -Force

if ($IncludeLocalConfig) {
    if (-not (Test-Path $realCfg)) {
        Write-Error "指定了 -IncludeLocalConfig，但找不到 $realCfg"
    }
    Copy-Item $realCfg (Join-Path $out 'config.json') -Force
    Write-Host ''
    Write-Warning '已把真实 config.json 拷进 dist —— 该目录现在含有你的 Notion 令牌'
    Write-Warning '本目录仅供本机使用，【绝对不要】打包发给别人或上传'
} else {
    Copy-Item (Join-Path $root 'config.example.json') (Join-Path $out 'config.json') -Force
    Write-Host '    已放入占位配置（首次运行需自行填写令牌）'

    # 失败即中止：万一以后有人改回「拷真实配置」，这里会拦下而不是把令牌发出去
    $shipped = Get-Content (Join-Path $out 'config.json') -Raw | ConvertFrom-Json
    if ($shipped.token -ne $placeholder) {
        Write-Error '发布目录里的 config.json 不是占位模板，可能含真实令牌。已中止。'
        exit 1
    }
    if (Test-Path $realCfg) {
        Write-Host '    检测到本机 config.json（含令牌），已按安全模式跳过，未拷入 dist'
        Write-Host '    本机自用请改用： .\tools\build.ps1 -IncludeLocalConfig'
    }
}

Write-Host ''
Write-Host "完成 -> $out"
Write-Host '注意：dist\notion-today-widget\ 整个文件夹是一个整体，分发时要一起搬。'
