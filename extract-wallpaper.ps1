# ============================================================
#  Wallpaper Engine 壁纸图片提取工具
#  支持: scene.pkg / scene.mpkg / .mkg (PKG 容器格式) 中的全部纹理
#        (JPEG/PNG 内嵌 + DXT/LZ4 等 GPU 压缩格式,由内置 RePKG 引擎转换)
#        以及包内直接存放的普通图片、单独拖入的 .tex 纹理文件
#        动图: GIF/动图 WebP/APNG/AVIF(散放与包内条目原样提取,.tex 内嵌按签名识别)
#  用法:
#    1. 把壁纸文件夹(或 scene.pkg / scene.mpkg 文件)拖到 提取壁纸图片.bat 上
#    2. 双击 提取壁纸图片.bat (自动在本文件夹及子文件夹中查找包文件)
#    3. 命令行: powershell -ExecutionPolicy Bypass -File extract-wallpaper.ps1 "路径"
#  可选参数:
#    -Force     输出文件夹已存在时也重新提取(默认跳过已提取过的壁纸)
#    -NoOpen    提取结束后不自动打开资源管理器窗口
#    -OutDir X  自定义输出根目录(默认: 本工具目录下的 "已提取图片")
#  说明: 图片数量自动识别,无需预先告知。scene 型壁纸从包文件中提取全部
#        纹理图片;若文件夹里没有包文件(网页型壁纸),则直接收集散放的
#        jpg/png/bmp/gif/webp/avif 图片(GIF 等动图原样保留动画)。内置解析采用
#        流式读取,不会把整个大包读入内存,超大包也能稳定处理。
#  输出: 统一保存到输出根目录下的 <壁纸文件夹名> 文件夹中
#        (重复运行会自动跳过已提取过的壁纸,不会产生重复图片)
# ============================================================

param(
    [Parameter(Position = 0, ValueFromRemainingArguments = $true)]
    [string[]]$InputArgs = @(),
    [switch]$Force,
    [switch]$NoOpen,
    # -Fast: App 并行提取模式。RePKG 不降优先级（并行时前台干扰可忽略），
    # 且退出时不清扫全局临时目录（会删掉其他并行进程正在用的目录）。
    # 双击 bat 的命令行用法不带 -Fast，行为与旧版完全一致。
    [switch]$Fast,
    [string]$OutDir = ''
)

$ErrorActionPreference = 'Continue'
$script:PkgCount = 0
$script:ImageCount = 0
$script:VideoCount = 0
$script:AudioCount = 0
$script:SkippedCount = 0
$script:DupSkipCount = 0
$script:OutDirs = New-Object System.Collections.ArrayList

# 输出根目录: 默认在脚本所在目录下,可用 -OutDir 覆盖
$script:OutRoot = if ($OutDir) {
    if ([System.IO.Path]::IsPathRooted($OutDir)) { $OutDir } else { Join-Path $PSScriptRoot $OutDir }
} else {
    Join-Path $PSScriptRoot '已提取图片'
}

# 输出根目录标记文件: 有这个标记时,App 才允许自动清理其中的空文件夹。
# 自定义 -OutDir 首次使用时在这里留下标记,避免 App 把无关目录里的空文件夹清掉。
function Initialize-OutRoot {
    if (-not (Test-Path -LiteralPath $script:OutRoot)) {
        [void](New-Item -ItemType Directory -Path $script:OutRoot -Force)
    }
    $mark = Join-Path $script:OutRoot '.wall_extractor_out'
    if (-not (Test-Path -LiteralPath $mark)) {
        try {
            [System.IO.File]::WriteAllText($mark, "壁纸提取器输出目录标记`n",
                (New-Object System.Text.UTF8Encoding($false)))
        }
        catch { }
    }
}

# 清理以前崩溃残留的临时目录。只删超过 6 小时的,避免误删其他并行引擎进程正在用的目录。
function Remove-StaleTempDirs {
    if (-not (Test-Path -LiteralPath $script:OutRoot)) { return }
    $cutoff = (Get-Date).AddHours(-6)
    Get-ChildItem -LiteralPath $script:OutRoot -Directory -Filter '.wall_tmp_*' -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -lt $cutoff } |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }
}

function Format-Size([long]$bytes) {
    if ($bytes -ge 1MB) { return ('{0:N1} MB' -f ($bytes / 1MB)) }
    if ($bytes -ge 1KB) { return ('{0:N0} KB' -f ($bytes / 1KB)) }
    return ('{0} B' -f $bytes)
}

function Read-UInt32BE([byte[]]$b, [int]$pos) {
    # 用 [long] 计算,避免字节 >= 0x80 时 int 溢出(旧版对 PNG 大块长度可能算错)
    return (([long]$b[$pos] -shl 24) -bor ([long]$b[$pos + 1] -shl 16) -bor ([long]$b[$pos + 2] -shl 8) -bor ([long]$b[$pos + 3]))
}

# 从 JPEG 头开始遍历到 EOI (FFD9),处理熵编码段中填充的 FF00 与 RSTn 标记
function Get-JpegEnd([byte[]]$b, [int]$start) {
    $n = $b.Length
    $i = $start + 2
    $inScan = $false
    while ($i -lt $n - 1) {
        if ($inScan) {
            while ($i -lt $n -and $b[$i] -ne 255) { $i++ }
            if ($i -ge $n - 1) { return -1 }
            $m = $b[$i + 1]
            if ($m -eq 0 -or ($m -ge 208 -and $m -le 215)) { $i += 2; continue }
            if ($m -eq 217) { return $i + 2 }
            if ($m -eq 216) { $i += 2; continue }
            if ($i + 3 -ge $n) { return -1 }
            $len = ($b[$i + 2] -shl 8) -bor $b[$i + 3]
            $i += 2 + $len
            if ($m -ne 218) { $inScan = $false }
            continue
        }
        if ($b[$i] -ne 255) { return -1 }
        $m = $b[$i + 1]
        if ($m -eq 217) { return $i + 2 }
        if ($m -eq 216 -or $m -eq 1 -or ($m -ge 208 -and $m -le 215)) { $i += 2; continue }
        if ($m -eq 0) { $i += 2; continue }
        if ($i + 3 -ge $n) { return -1 }
        $len = ($b[$i + 2] -shl 8) -bor $b[$i + 3]
        if ($m -eq 218) { $inScan = $true }
        $i += 2 + $len
    }
    return -1
}

# PNG: 从签名开始按块遍历,到 IEND 结束
function Get-PngEnd([byte[]]$b, [int]$start) {
    $n = $b.Length
    $i = $start + 8
    while ($i + 8 -le $n) {
        $len = Read-UInt32BE $b $i
        if ($i + 12 + $len -gt $n) { return -1 }
        $type = [System.Text.Encoding]::ASCII.GetString($b, $i + 4, 4)
        $i += 12 + $len
        if ($type -eq 'IEND') { return $i }
    }
    return -1
}

# GIF: 从签名开始按块遍历(GIF87a/GIF89a),跳过扩展块与图像数据子块,到 0x3B 结束符
function Get-GifEnd([byte[]]$b, [int]$start) {
    $n = $b.Length
    $i = $start + 6                      # 跳过 6 字节头 GIF87a/GIF89a
    if ($i + 7 -gt $n) { return -1 }
    $flags = $b[$i + 4]                  # 逻辑屏幕描述符的全局颜色表标志
    $i += 7
    if ($flags -band 0x80) { $i += 3 -shl (($flags -band 0x07) + 1) }
    while ($i -lt $n) {
        $block = $b[$i]
        if ($block -eq 0x3B) { return $i + 1 }
        if ($block -eq 0x21) {           # 扩展块(图形控制/注释/应用等)
            $i += 2
            while ($i -lt $n) {
                $sub = $b[$i]
                $i += 1 + $sub
                if ($sub -eq 0) { break }
            }
            continue
        }
        if ($block -eq 0x2C) {           # 图像描述符 + LZW 数据子块
            $i += 10
            if ($i -gt $n) { return -1 }
            $lflags = $b[$i - 1]
            if ($lflags -band 0x80) { $i += 3 -shl (($lflags -band 0x07) + 1) }
            if ($i -ge $n) { return -1 }
            $i += 1                      # LZW 最小编码大小
            while ($i -lt $n) {
                $sub = $b[$i]
                $i += 1 + $sub
                if ($sub -eq 0) { break }
            }
            continue
        }
        return -1                        # 未知块类型,不当作完整 GIF
    }
    return -1
}

# WebP: RIFF 容器,RIFF 长度字段 + 8 即文件总长(动图 WebP 与静态 WebP 签名相同)
function Get-WebpEnd([byte[]]$b, [int]$start) {
    $n = $b.Length
    if ($start + 12 -gt $n) { return -1 }
    if ($b[$start + 8] -ne 0x57 -or $b[$start + 9] -ne 0x45 -or
        $b[$start + 10] -ne 0x42 -or $b[$start + 11] -ne 0x50) { return -1 }
    $riffLen = ([long]$b[$start + 4]) -bor ([long]$b[$start + 5] -shl 8) -bor
               ([long]$b[$start + 6] -shl 16) -bor ([long]$b[$start + 7] -shl 24)
    if ($riffLen -le 0 -or $riffLen -gt $n) { $riffLen = $n - $start - 8 }
    $end = $start + 8 + $riffLen
    if ($end -gt $n) { $end = $n }
    return $end
}

# 在 .tex 纹理容器中寻找内嵌图片(JPEG > PNG > GIF > WebP > AVIF;GIF/WebP/AVIF 含动图),
# 返回 @{Start;Length;Ext} 或 $null
function Get-EmbeddedImage([byte[]]$tex) {
    $best = $null
    # JPEG: 扫描 FF D8 并完整遍历
    $i = [Array]::IndexOf($tex, [byte]255, 0)
    while ($i -ge 0 -and $i -lt $tex.Length - 1) {
        if ($tex[$i + 1] -eq 216) {
            $end = Get-JpegEnd $tex $i
            if ($end -gt 0) {
                $len = $end - $i
                if ($null -eq $best -or $len -gt $best.Length) {
                    $best = @{ Start = $i; Length = $len; Ext = 'jpg' }
                }
            }
        }
        $i = [Array]::IndexOf($tex, [byte]255, $i + 1)
    }
    if ($best) { return $best }
    # PNG: 扫描 89 50 4E 47 0D 0A 1A 0A 签名
    $i = [Array]::IndexOf($tex, [byte]137, 0)
    while ($i -ge 0 -and $i -lt $tex.Length - 8) {
        if ($tex[$i + 1] -eq 80 -and $tex[$i + 2] -eq 78 -and $tex[$i + 3] -eq 71 -and
            $tex[$i + 4] -eq 13 -and $tex[$i + 5] -eq 10 -and $tex[$i + 6] -eq 26 -and $tex[$i + 7] -eq 10) {
            $end = Get-PngEnd $tex $i
            if ($end -gt 0) {
                $len = $end - $i
                if ($null -eq $best -or $len -gt $best.Length) {
                    $best = @{ Start = $i; Length = $len; Ext = 'png' }
                }
            }
        }
        $i = [Array]::IndexOf($tex, [byte]137, $i + 1)
    }
    if ($best) { return $best }
    # GIF: 扫描 GIF87a/GIF89a 签名(动图)
    $i = [Array]::IndexOf($tex, [byte]0x47, 0)
    while ($i -ge 0 -and $i -lt $tex.Length - 6) {
        if ($tex[$i + 1] -eq 0x49 -and $tex[$i + 2] -eq 0x46 -and $tex[$i + 3] -eq 0x38 -and
            ($tex[$i + 4] -eq 0x37 -or $tex[$i + 4] -eq 0x39) -and $tex[$i + 5] -eq 0x61) {
            $end = Get-GifEnd $tex $i
            if ($end -gt 0) {
                $len = $end - $i
                if ($null -eq $best -or $len -gt $best.Length) {
                    $best = @{ Start = $i; Length = $len; Ext = 'gif' }
                }
            }
        }
        $i = [Array]::IndexOf($tex, [byte]0x47, $i + 1)
    }
    if ($best) { return $best }
    # WebP: 扫描 RIFF....WEBP 签名(动图 WebP 与静态 WebP 同签名)
    $i = [Array]::IndexOf($tex, [byte]0x52, 0)
    while ($i -ge 0 -and $i -lt $tex.Length - 12) {
        if ($tex[$i + 1] -eq 0x49 -and $tex[$i + 2] -eq 0x46 -and $tex[$i + 3] -eq 0x46) {
            $end = Get-WebpEnd $tex $i
            if ($end -gt 0) {
                $len = $end - $i
                if ($null -eq $best -or $len -gt $best.Length) {
                    $best = @{ Start = $i; Length = $len; Ext = 'webp' }
                }
            }
        }
        $i = [Array]::IndexOf($tex, [byte]0x52, $i + 1)
    }
    if ($best) { return $best }
    # AVIF: ftyp 盒主品牌为 avif/avis(avis 为动图序列);数据取到文件尾,与视频处理方式一致
    $i = [Array]::IndexOf($tex, [byte]0x66, 0)
    while ($i -ge 0 -and $i -lt $tex.Length - 8) {
        if ($i -ge 4 -and $tex[$i + 1] -eq 0x74 -and $tex[$i + 2] -eq 0x79 -and $tex[$i + 3] -eq 0x70) {
            $brand = [System.Text.Encoding]::ASCII.GetString($tex, $i + 4, 4)
            if ($brand -eq 'avif' -or $brand -eq 'avis') {
                $best = @{ Start = $i - 4; Length = $tex.Length - ($i - 4); Ext = 'avif' }
                break
            }
        }
        $i = [Array]::IndexOf($tex, [byte]0x66, $i + 1)
    }
    return $best
}

# 在 .tex 纹理容器中寻找内嵌视频(MP4/WEBM等;AVIF 归图片逻辑处理),返回 @{Start;Length;Ext} 或 $null
function Get-EmbeddedVideo([byte[]]$tex) {
    # MP4: 扫描 ftyp 签名 (66 74 79 70)
    $ftypPattern = [byte[]]@(0x66, 0x74, 0x79, 0x70)
    $i = [Array]::IndexOf($tex, $ftypPattern[0], 0)
    while ($i -ge 0 -and $i -lt $tex.Length - 4) {
        if ($tex[$i + 1] -eq $ftypPattern[1] -and $tex[$i + 2] -eq $ftypPattern[2] -and $tex[$i + 3] -eq $ftypPattern[3]) {
            # AVIF/AVIS(动图 AVIF)同为 ftyp 盒,由图片逻辑提取,这里跳过避免误存成 .mp4
            if ($i + 8 -le $tex.Length) {
                $brand = [System.Text.Encoding]::ASCII.GetString($tex, $i + 4, 4)
                if ($brand -eq 'avif' -or $brand -eq 'avis') {
                    $i = [Array]::IndexOf($tex, $ftypPattern[0], $i + 1)
                    continue
                }
            }
            # 检查前面是否有长度前缀(4字节,大端序)
            $start = $i
            if ($i -ge 4) {
                $lenBytes = New-Object byte[] 4
                [Array]::Copy($tex, $i - 4, $lenBytes, 0, 4)
                $prefixLen = Read-UInt32BE $lenBytes 0
                if ($prefixLen -gt 0 -and $prefixLen -lt ($tex.Length - $i + 4)) {
                    $start = $i - 4
                }
            }
            $len = $tex.Length - $start
            return @{ Start = $start; Length = $len; Ext = 'mp4' }
        }
        $i = [Array]::IndexOf($tex, $ftypPattern[0], $i + 1)
    }
    # WEBM: 扫描 EBML 签名 (1A 45 DF A3)
    $ebmlPattern = [byte[]]@(0x1A, 0x45, 0xDF, 0xA3)
    $i = [Array]::IndexOf($tex, $ebmlPattern[0], 0)
    while ($i -ge 0 -and $i -lt $tex.Length - 4) {
        if ($tex[$i + 1] -eq $ebmlPattern[1] -and $tex[$i + 2] -eq $ebmlPattern[2] -and $tex[$i + 3] -eq $ebmlPattern[3]) {
            $start = $i
            if ($i -ge 4) {
                $lenBytes = New-Object byte[] 4
                [Array]::Copy($tex, $i - 4, $lenBytes, 0, 4)
                $prefixLen = Read-UInt32BE $lenBytes 0
                if ($prefixLen -gt 0 -and $prefixLen -lt ($tex.Length - $i + 4)) {
                    $start = $i - 4
                }
            }
            $len = $tex.Length - $start
            return @{ Start = $start; Length = $len; Ext = 'webm' }
        }
        $i = [Array]::IndexOf($tex, $ebmlPattern[0], $i + 1)
    }
    return $null
}

function Test-ImageExt([string]$name) {
    return $name.EndsWith('.jpg') -or $name.EndsWith('.jpeg') -or $name.EndsWith('.png') -or
           $name.EndsWith('.bmp') -or $name.EndsWith('.gif') -or $name.EndsWith('.webp') -or
           $name.EndsWith('.avif') -or $name.EndsWith('.avifs')
}

function Test-MediaExt([string]$name) {
    return (Test-ImageExt $name) -or
           $name.EndsWith('.mp4') -or $name.EndsWith('.webm') -or $name.EndsWith('.avi') -or
           $name.EndsWith('.mov') -or $name.EndsWith('.mkv') -or $name.EndsWith('.flv') -or
           $name.EndsWith('.wmv') -or $name.EndsWith('.m4v') -or $name.EndsWith('.mp3') -or
           $name.EndsWith('.wav') -or $name.EndsWith('.ogg') -or $name.EndsWith('.flac') -or
           $name.EndsWith('.aac') -or $name.EndsWith('.wma') -or $name.EndsWith('.m4a')
}

# 打开资源管理器(失败不报错,配合 -NoOpen 可完全禁用)
function Open-Folder([string]$path) {
    if ($NoOpen) { return }
    try {
        ([System.Diagnostics.Process]::Start('explorer.exe', ('"{0}"' -f $path))).Dispose()
    }
    catch {
        Write-Host ('[提示] 打开资源管理器失败: ' + $_.Exception.Message) -ForegroundColor DarkGray
    }
}

# 生成不冲突的输出文件名: 若目标已存在,追加 _2 _3 ... (旧版会静默覆盖)
function Get-UniqueDest([string]$dir, [string]$name) {
    $dest = Join-Path $dir $name
    if (-not (Test-Path -LiteralPath $dest)) { return $dest }
    $base = [System.IO.Path]::GetFileNameWithoutExtension($name)
    $ext = [System.IO.Path]::GetExtension($name)
    for ($n = 2; $n -lt 100000; $n++) {
        $cand = Join-Path $dir ($base + '_' + $n + $ext)
        if (-not (Test-Path -LiteralPath $cand)) { return $cand }
    }
    return $dest
}

# 输出文件夹里真的有文件才算"已提取过": 上次提取失败或图片被删光会留下空文件夹,
# 空文件夹不能算提取过,要照常重新提取(递归查任意一个文件,含 masks 子目录)
function Test-ExtractedBefore([string]$dir) {
    if (-not (Test-Path -LiteralPath $dir)) { return $false }
    $any = Get-ChildItem -LiteralPath $dir -Recurse -File -ErrorAction SilentlyContinue |
        Select-Object -First 1
    return ($null -ne $any)
}

# 文件夹里没有 scene.pkg 时(例如网页型壁纸),直接收集散放的图片文件
function Copy-LooseImages([string]$folder) {
    # 与包/纹理处理保持一致,开头输出 "处理: 路径",供 App 批处理模式归属进度
    Write-Host ('处理: ' + $folder)
    $imgs = @(Get-ChildItem -LiteralPath $folder -Recurse -File -ErrorAction SilentlyContinue |
        Where-Object {
            ($_.Name.ToLowerInvariant() -match '\.(jpg|jpeg|png|bmp|gif|webp|avifs?)$') -and
            ($_.FullName -notlike ($script:OutRoot + '*'))
        } | Sort-Object FullName)
    if ($imgs.Count -eq 0) {
        Write-Host ('  该文件夹中没有找到可提取的图片: ' + $folder) -ForegroundColor Yellow
        return
    }
    $wallName = Split-Path -Leaf $folder
    Initialize-OutRoot
    $outDir = Join-Path $script:OutRoot $wallName

    # 防止重复提取: 已提取图片里已有同名文件夹则提示已提取过,并打开该文件夹
    if ((Test-ExtractedBefore $outDir) -and -not $Force) {
        $script:DupSkipCount++
        Write-Host ('  [跳过] 已提取过了,本次跳过: ' + $wallName) -ForegroundColor Yellow
        Write-Host ('         如需重新提取,请使用 -Force 参数或删除该文件夹: ' + $outDir) -ForegroundColor DarkGray
        Open-Folder $outDir
        return
    }

    if (-not (Test-Path -LiteralPath $outDir)) { [void](New-Item -ItemType Directory -Path $outDir -Force) }
    [void]$script:OutDirs.Add($outDir)
    foreach ($f in $imgs) {
        $rel = $f.FullName.Substring($folder.Length).TrimStart('\')
        $rel = $rel.Replace('\', '_')
        $dest = Get-UniqueDest $outDir $rel
        if ($dest -eq (Join-Path $outDir $rel) -and (Test-Path -LiteralPath $dest)) { continue }
        Copy-Item -LiteralPath $f.FullName -Destination $dest -Force
        $script:ImageCount++
        Write-Host ('  [图片] ' + $rel + '  (' + (Format-Size $f.Length) + ')') -ForegroundColor Green
    }
    Write-Host ('输出目录: ' + $outDir) -ForegroundColor Cyan
}

# 使用内置 RePKG 引擎提取(支持 DXT/LZ4 等 GPU 压缩纹理,以及 JPEG/PNG 纹理)
# 返回成功提取的图片数量;失败或引擎缺失时返回 0
function Extract-PkgWithRepkg([string]$pkgPath, [string]$outDir) {
    $repkgExe = Join-Path $PSScriptRoot 'repkg\RePKG.exe'
    if (-not (Test-Path -LiteralPath $repkgExe)) { Write-Host '[提示] 未找到 RePKG 引擎,使用内置解析(支持 JPEG/PNG/GIF/WebP/AVIF 图片)。' -ForegroundColor Yellow; return 0 }
    # 临时目录放在输出根目录下(隐藏名),保证与输出同卷:
    # 之后把图片 Move 到输出目录就是一次改名,不再整包复制(输出到 D: 之类跨盘时尤其明显)。
    Initialize-OutRoot
    $tempDir = Join-Path $script:OutRoot ('.wall_tmp_' + [System.Guid]::NewGuid().ToString('N'))
    try {
        [void](New-Item -ItemType Directory -Path $tempDir -Force)
        # RePKG 引擎只认 .pkg 扩展名;mpkg/mkg 等新格式结构相同,先用硬链接(同盘)或复制(跨盘)成 .pkg
        $repkgInput = $pkgPath
        if ([System.IO.Path]::GetExtension($pkgPath).ToLowerInvariant() -ne '.pkg') {
            $repkgInput = Join-Path $tempDir 'scene.pkg'
            try {
                [void](New-Item -ItemType HardLink -Path $repkgInput -Target $pkgPath -ErrorAction Stop)
                Write-Host '  [加速] 已用硬链接复用原文件,避免复制大包。' -ForegroundColor DarkGray
            }
            catch {
                Copy-Item -LiteralPath $pkgPath -Destination $repkgInput -Force
            }
        }
        Write-Host '  [引擎] 正在调用 RePKG 提取纹理...' -ForegroundColor DarkGray
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = $repkgExe
        # -e 只解出图片类条目: 视频/音频/json/着色器等不再解到临时目录, 省掉最占磁盘的一步
        # (.tex 转图片不受影响, 已实测解出结果与全量解包完全一致)
        $psi.Arguments = 'extract "' + $repkgInput + '" -o "' + $tempDir + '" -e "png,jpg,jpeg,bmp,gif,webp,avif,avifs,tex"'
        $psi.UseShellExecute = $false
        $psi.CreateNoWindow = $true
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $repkgProc = [System.Diagnostics.Process]::Start($psi)
        # 低优先级后台跑, 不抢前台应用的 CPU; -Fast(App 并行模式)时放开
        $pri = if ($Fast) { [System.Diagnostics.ProcessPriorityClass]::Normal }
               else { [System.Diagnostics.ProcessPriorityClass]::BelowNormal }
        try { $repkgProc.PriorityClass = $pri } catch { }
        $errTask = $repkgProc.StandardError.ReadToEndAsync()
        [void]$repkgProc.StandardOutput.ReadToEnd()
        [void]$errTask.Result
        $repkgProc.WaitForExit()
        $exitCode = $repkgProc.ExitCode
        $repkgProc.Dispose()
        if ($exitCode -ne 0) { return 0 }
        $imgs = @(Get-ChildItem -LiteralPath $tempDir -Recurse -File |
            Where-Object {
                ($_.Name.ToLowerInvariant() -match '\.(png|jpg|jpeg|bmp|gif|webp|avifs?)$') -and
                ($_.FullName -notmatch '(?i)mask')
            } | Sort-Object FullName)
        if ($imgs.Count -eq 0) { return 0 }
        foreach ($f in $imgs) {
            $rel = $f.FullName.Substring($tempDir.Length).TrimStart('\')
            $rel = $rel.Replace('\', '/')
            if ($rel.StartsWith('materials/')) { $rel = $rel.Substring(10) }
            elseif ($rel.StartsWith('images/')) { $rel = $rel.Substring(7) }
            elseif ($rel.StartsWith('textures/')) { $rel = $rel.Substring(9) }
            $rel = $rel.Replace('/', '_')
            $rel = $rel -replace '[<>:"|?*]', '_'
            $dest = Get-UniqueDest $outDir $rel
            try {
                # 临时目录与输出目录通常同盘: 改名移动省一次完整写入; 跨盘时退回复制
                try { [System.IO.File]::Move($f.FullName, $dest) }
                catch { Copy-Item -LiteralPath $f.FullName -Destination $dest -Force }
                $script:ImageCount++
                Write-Host ('  [图片] ' + (Split-Path -Leaf $dest) + '  (' + (Format-Size $f.Length) + ')') -ForegroundColor Green
            }
            catch {
                Write-Host ('[警告] 复制失败: ' + $_.Exception.Message) -ForegroundColor Yellow
            }
        }
        return $imgs.Count
    }
    catch {
        Write-Host ('[警告] RePKG 引擎运行异常,改用内置解析: ' + $_.Exception.Message) -ForegroundColor Yellow
        return 0
    }
    finally {
        if (Test-Path -LiteralPath $tempDir) { Remove-Item -LiteralPath $tempDir -Recurse -Force -ErrorAction SilentlyContinue }
    }
}

# 直接解析单个 .tex 纹理文件(不依赖包容器)
function Extract-TexFile([string]$texPath) {
    $script:PkgCount++
    Write-Host ''
    Write-Host ('* ' * 30) -ForegroundColor DarkGray
    Write-Host ('处理: ' + $texPath) -ForegroundColor Cyan

    $baseName = [System.IO.Path]::GetFileNameWithoutExtension($texPath)
    Initialize-OutRoot
    $outDir = Join-Path $script:OutRoot $baseName

    if ((Test-ExtractedBefore $outDir) -and -not $Force) {
        $script:DupSkipCount++
        Write-Host ('[跳过] 已提取过了,本次跳过: ' + $baseName) -ForegroundColor Yellow
        Write-Host ('       如需重新提取,请使用 -Force 参数或删除该文件夹: ' + $outDir) -ForegroundColor DarkGray
        Open-Folder $outDir
        return
    }
    if (-not (Test-Path -LiteralPath $outDir)) { [void](New-Item -ItemType Directory -Path $outDir -Force) }
    [void]$script:OutDirs.Add($outDir)

    $texBytes = [System.IO.File]::ReadAllBytes($texPath)
    $img = Get-EmbeddedImage $texBytes
    $video = Get-EmbeddedVideo $texBytes
    if ($null -ne $img) {
        $outName = $baseName + '.' + $img.Ext
        $dest = Get-UniqueDest $outDir $outName
        $outBytes = New-Object byte[] $img.Length
        [Array]::Copy($texBytes, $img.Start, $outBytes, 0, $img.Length)
        [System.IO.File]::WriteAllBytes($dest, $outBytes)
        $script:ImageCount++
        Write-Host ('  [图片] ' + $outName + '  (' + $img.Ext.ToUpper() + ', ' + (Format-Size $img.Length) + ')') -ForegroundColor Green
    }
    elseif ($null -ne $video) {
        $outName = $baseName + '.' + $video.Ext
        $dest = Get-UniqueDest $outDir $outName
        $outBytes = New-Object byte[] $video.Length
        [Array]::Copy($texBytes, $video.Start, $outBytes, 0, $video.Length)
        [System.IO.File]::WriteAllBytes($dest, $outBytes)
        $script:VideoCount++
        Write-Host ('  [视频] ' + $outName + '  (' + $video.Ext.ToUpper() + ', ' + (Format-Size $video.Length) + ')') -ForegroundColor Green
    }
    else {
        Write-Host ('[跳过] 纹理中未找到图片或视频数据: ' + $texPath) -ForegroundColor Yellow
        $script:SkippedCount++
    }
    Write-Host ('输出目录: ' + $outDir) -ForegroundColor Cyan
}

function Extract-Pkg([string]$pkgPath) {
    $script:PkgCount++
    Write-Host ''
    Write-Host ('* ' * 30) -ForegroundColor DarkGray
    Write-Host ('处理: ' + $pkgPath) -ForegroundColor Cyan

    $wallName = Split-Path -Leaf (Split-Path -Parent $pkgPath)
    Initialize-OutRoot
    $outDir = Join-Path $script:OutRoot $wallName

    # 防止重复提取: 已提取图片里已有同名文件夹则提示已提取过,并打开该文件夹
    if ((Test-ExtractedBefore $outDir) -and -not $Force) {
        $script:DupSkipCount++
        Write-Host ('[跳过] 已提取过了,本次跳过: ' + $wallName) -ForegroundColor Yellow
        Write-Host ('       如需重新提取,请使用 -Force 参数或删除该文件夹: ' + $outDir) -ForegroundColor DarkGray
        Open-Folder $outDir
        return
    }
    if (-not (Test-Path -LiteralPath $outDir)) { [void](New-Item -ItemType Directory -Path $outDir -Force) }
    [void]$script:OutDirs.Add($outDir)

    # 优先使用内置 RePKG 引擎(支持 DXT/LZ4 等 GPU 压缩纹理)
    $repkgCount = Extract-PkgWithRepkg $pkgPath $outDir
    if ($repkgCount -gt 0) {
        Write-Host ('输出目录: ' + $outDir) -ForegroundColor Cyan
        return
    }

    # ---- 以下为内置解析(RePKG 引擎不可用时的后备,支持 JPEG/PNG/GIF/WebP/AVIF 内嵌图片) ----
    # 流式读取: 只读文件头和条目表,再按需定位读取单个条目,避免把整个大包读入内存
    $fs = [System.IO.File]::Open($pkgPath, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
    $reader = New-Object System.IO.BinaryReader($fs)
    try {
        $fileLen = $fs.Length
        if ($fileLen -lt 16) {
            Write-Host '[错误] 文件太小,不是有效的 Wallpaper Engine 包文件。' -ForegroundColor Red
            return
        }
        # 文件头: [长度][魔数字符串][条目数]
        $len0 = $reader.ReadUInt32()
        if ($len0 -le 0 -or $len0 -gt 16 -or (8 + $len0) -gt $fileLen) {
            Write-Host '[错误] 文件头不合法,不是 Wallpaper Engine 包文件。' -ForegroundColor Red
            return
        }
        $magicBytes = $reader.ReadBytes([int]$len0)
        $magic = [System.Text.Encoding]::ASCII.GetString($magicBytes)
        if (-not $magic.StartsWith('PKG')) {
            Write-Host ('[错误] 无法识别的包格式: ' + $magic + '(仅支持 PKG 系列包文件)') -ForegroundColor Red
            return
        }

        # 解析条目表: [名称长度][名称(UTF8)][数据偏移][数据大小]
        $entryCount = $reader.ReadUInt32()
        if ($entryCount -le 0 -or $entryCount -gt 200000) {
            Write-Host '[错误] 条目数量异常,解析失败。' -ForegroundColor Red
            return
        }
        $entries = New-Object System.Collections.ArrayList
        for ($k = 0; $k -lt $entryCount; $k++) {
            if (($fs.Position + 8) -gt $fileLen) { Write-Host '[错误] 条目表越界,解析失败。' -ForegroundColor Red; return }
            $nameLen = $reader.ReadUInt32()
            if ($nameLen -le 0 -or ($fs.Position + $nameLen + 8) -gt $fileLen) { Write-Host '[错误] 条目名称异常,解析失败。' -ForegroundColor Red; return }
            $nameBytes = $reader.ReadBytes([int]$nameLen)
            $name = [System.Text.Encoding]::UTF8.GetString($nameBytes)
            $offset = $reader.ReadUInt32()
            $size = $reader.ReadUInt32()
            [void]$entries.Add([pscustomobject]@{ Name = $name; Offset = [int64]$offset; Size = [int64]$size })
        }

        # 数据区起点 = 文件大小 - 最后一个条目的结束位置
        $maxEnd = [int64]0
        foreach ($e in $entries) {
            $end = $e.Offset + $e.Size
            if ($end -gt $maxEnd) { $maxEnd = $end }
        }
        $dataBase = [int64]$fileLen - $maxEnd
        $tableEnd = $fs.Position
        if ($dataBase -lt $tableEnd) { $dataBase = $tableEnd }

        Write-Host ('格式 ' + $magic + ' | 共 ' + $entryCount + ' 个条目')

        foreach ($e in $entries) {
            $lower = $e.Name.ToLowerInvariant()
            $isTex = $lower.EndsWith('.tex')
            $isPlain = (Test-MediaExt $lower)
            if (-not $isTex -and -not $isPlain) { continue }

            $dataStart = [int64]$dataBase + $e.Offset
            if (($dataStart + $e.Size) -gt $fileLen -or $dataStart -lt 0) {
                Write-Host ('[警告] 条目数据越界,跳过: ' + $e.Name) -ForegroundColor Yellow
                $script:SkippedCount++
                continue
            }

            # 整理输出路径: 去掉 materials/ 等前缀, masks 放到子目录
            $outName = $e.Name
            $subDir = ''
            if ($outName.StartsWith('materials/')) { $outName = $outName.Substring(10) }
            elseif ($outName.StartsWith('images/')) { $outName = $outName.Substring(7) }
            elseif ($outName.StartsWith('textures/')) { $outName = $outName.Substring(9) }
            if ($outName.StartsWith('masks/')) { $subDir = 'masks'; $outName = $outName.Substring(6) }
            $outName = $outName.Replace('/', '_')
            $outName = $outName -replace '[<>:"|?*]', '_'

            [void]$fs.Seek($dataStart, [System.IO.SeekOrigin]::Begin)
            if ($isTex) {
                $texBytes = $reader.ReadBytes([int]$e.Size)
                $img = Get-EmbeddedImage $texBytes
                $video = Get-EmbeddedVideo $texBytes
                if ($null -ne $img) {
                    $outName = $outName -replace '\.tex$', ('.' + $img.Ext)
                    $destDir = $outDir
                    if ($subDir) {
                        $destDir = Join-Path $outDir $subDir
                        if (-not (Test-Path -LiteralPath $destDir)) { [void](New-Item -ItemType Directory -Path $destDir) }
                    }
                    $dest = Get-UniqueDest $destDir $outName
                    $outBytes = New-Object byte[] $img.Length
                    [Array]::Copy($texBytes, $img.Start, $outBytes, 0, $img.Length)
                    [System.IO.File]::WriteAllBytes($dest, $outBytes)
                    $script:ImageCount++
                    Write-Host ('  [图片] ' + $outName + '  (' + $img.Ext.ToUpper() + ', ' + (Format-Size $img.Length) + ')') -ForegroundColor Green
                }
                elseif ($null -ne $video) {
                    $outName = $outName -replace '\.tex$', ('.' + $video.Ext)
                    $destDir = $outDir
                    if ($subDir) {
                        $destDir = Join-Path $outDir $subDir
                        if (-not (Test-Path -LiteralPath $destDir)) { [void](New-Item -ItemType Directory -Path $destDir) }
                    }
                    $dest = Get-UniqueDest $destDir $outName
                    $outBytes = New-Object byte[] $video.Length
                    [Array]::Copy($texBytes, $video.Start, $outBytes, 0, $video.Length)
                    [System.IO.File]::WriteAllBytes($dest, $outBytes)
                    $script:VideoCount++
                    Write-Host ('  [视频] ' + $outName + '  (' + $video.Ext.ToUpper() + ', ' + (Format-Size $video.Length) + ')') -ForegroundColor Green
                }
                else {
                    Write-Host ('[跳过] 纹理中未找到图片或视频数据: ' + $e.Name) -ForegroundColor Yellow
                    $script:SkippedCount++
                    continue
                }
            }
            else {
                $destDir = $outDir
                if ($subDir) {
                    $destDir = Join-Path $outDir $subDir
                    if (-not (Test-Path -LiteralPath $destDir)) { [void](New-Item -ItemType Directory -Path $destDir) }
                }
                $dest = Get-UniqueDest $destDir $outName
                $outBytes = $reader.ReadBytes([int]$e.Size)
                [System.IO.File]::WriteAllBytes($dest, $outBytes)
                if (Test-ImageExt $lower) {
                    $script:ImageCount++
                    Write-Host ('  [图片] ' + $outName + '  (' + (Format-Size $e.Size) + ')') -ForegroundColor Green
                }
                elseif ($lower.EndsWith('.mp3') -or $lower.EndsWith('.wav') -or $lower.EndsWith('.ogg') -or
                        $lower.EndsWith('.flac') -or $lower.EndsWith('.aac') -or $lower.EndsWith('.wma') -or
                        $lower.EndsWith('.m4a')) {
                    $script:AudioCount++
                    Write-Host ('  [音频] ' + $outName + '  (' + (Format-Size $e.Size) + ')') -ForegroundColor Green
                }
                else {
                    $script:VideoCount++
                    Write-Host ('  [视频] ' + $outName + '  (' + (Format-Size $e.Size) + ')') -ForegroundColor Green
                }
            }
        }
        Write-Host ('输出目录: ' + $outDir) -ForegroundColor Cyan
    }
    finally {
        if ($reader) { $reader.Dispose() }
        if ($fs) { $fs.Dispose() }
    }
}

# ---------- 主流程 ----------
Write-Host ''
Write-Host '============================================================' -ForegroundColor DarkGray
Write-Host '   Wallpaper Engine 壁纸图片提取工具' -ForegroundColor Cyan
Write-Host '============================================================' -ForegroundColor DarkGray

# 先清理上次崩溃残留的临时目录(只删 6 小时前的,不会影响正在并行的其他进程)
Remove-StaleTempDirs

$pkgs = New-Object System.Collections.ArrayList
if ($InputArgs -and $InputArgs.Count -gt 0) {
    foreach ($arg in $InputArgs) {
        $item = Get-Item -LiteralPath $arg -ErrorAction SilentlyContinue
        if (-not $item) {
            Write-Host ('[错误] 找不到路径: ' + $arg) -ForegroundColor Red
            continue
        }
        if ($item.PSIsContainer) {
            $foundPkgs = @(Get-ChildItem -LiteralPath $item.FullName -Recurse -File -ErrorAction SilentlyContinue |
                Where-Object { $_.Name.ToLowerInvariant() -match '\.(pkg|mpkg|mkg)$' })
            if ($foundPkgs.Count -gt 0) {
                foreach ($p in $foundPkgs) { [void]$pkgs.Add($p.FullName) }
            }
            else {
                Write-Host ''
                Write-Host ('该文件夹没有 scene.pkg / scene.mpkg,尝试直接收集散放的图片: ' + $item.FullName) -ForegroundColor Cyan
                Copy-LooseImages $item.FullName
            }
        }
        else {
            if ($item.Name.ToLowerInvariant() -match '\.(pkg|mpkg|mkg)$') {
                [void]$pkgs.Add($item.FullName)
            }
            elseif ($item.Name.ToLowerInvariant() -match '\.tex$') {
                Extract-TexFile $item.FullName
            }
            else {
                Write-Host ('[提示] 不是 scene.pkg / scene.mpkg / .tex 文件,已忽略(请拖入壁纸文件夹、包文件或 .tex 纹理): ' + $arg) -ForegroundColor Yellow
            }
        }
    }
}
else {
    $here = Split-Path -Parent $MyInvocation.MyCommand.Path
    $foundPkgs = @(Get-ChildItem -LiteralPath $here -Recurse -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Name.ToLowerInvariant() -match '\.(pkg|mpkg|mkg)$' })
    foreach ($p in $foundPkgs) { [void]$pkgs.Add($p.FullName) }
    # 对不含包文件的直接子文件夹,尝试收集其中散放的图片(网页型壁纸)
    foreach ($sub in Get-ChildItem -LiteralPath $here -Directory -ErrorAction SilentlyContinue) {
        if ($sub.Name -eq '已提取图片') { continue }
        if ($sub.Name -eq 'repkg') { continue }
        if (Get-ChildItem -LiteralPath $sub.FullName -Recurse -File -ErrorAction SilentlyContinue |
            Where-Object { $_.Name.ToLowerInvariant() -match '\.(pkg|mpkg|mkg)$' } | Select-Object -First 1) { continue }
        Copy-LooseImages $sub.FullName
    }
}

if ($pkgs.Count -eq 0) {
    # 只有当确实什么都没处理到(既没有包/纹理,也没有散放图片)才提示用法
    $nothingDone = ($script:PkgCount -eq 0) -and ($script:ImageCount -eq 0) -and
                   ($script:VideoCount -eq 0) -and ($script:AudioCount -eq 0)
    if ($nothingDone) {
        Write-Host ''
        Write-Host '没有找到 scene.pkg / scene.mpkg 文件。' -ForegroundColor Red
        Write-Host '请把 Wallpaper Engine 壁纸文件夹(或里面的 scene.pkg / scene.mpkg 文件)拖到 提取壁纸图片.bat 上。' -ForegroundColor Yellow
        Write-Host '或者把本工具(两个文件)复制到壁纸文件夹旁边,然后双击 bat。' -ForegroundColor Yellow
    }
}
else {
    $pkgs = $pkgs | Sort-Object -Unique
    foreach ($p in $pkgs) { Extract-Pkg $p }
}

$totalMedia = $script:ImageCount + $script:VideoCount + $script:AudioCount
if ($totalMedia -gt 0) {
    if (-not $NoOpen) {
        Write-Host ''
        Write-Host '正在打开输出文件夹...' -ForegroundColor Cyan
        foreach ($d in @($script:OutDirs | Sort-Object -Unique)) {
            Write-Host ('  ' + $d) -ForegroundColor White
            Open-Folder $d
        }
    }
}

Write-Host ''
Write-Host '------------------------------------------------------------' -ForegroundColor DarkGray
$summary = '完成: 共处理 ' + $script:PkgCount + ' 个包/纹理'
$detail = @()
if ($script:ImageCount -gt 0) { $detail += ($script:ImageCount.ToString() + ' 张图片') }
if ($script:VideoCount -gt 0) { $detail += ($script:VideoCount.ToString() + ' 个视频') }
if ($script:AudioCount -gt 0) { $detail += ($script:AudioCount.ToString() + ' 个音频') }
if ($detail.Count -gt 0) { $summary += ',提取 ' + ($detail -join '、') }
if ($script:SkippedCount -gt 0) { $summary += ',跳过 ' + $script:SkippedCount + ' 项' }
if ($script:DupSkipCount -gt 0) { $summary += ',重复跳过 ' + $script:DupSkipCount + ' 个(已提取过)' }
Write-Host ($summary + '。') -ForegroundColor White
if ($totalMedia -gt 0) {
    Write-Host ('媒体文件已保存到: ' + $script:OutRoot) -ForegroundColor White
}
Write-Host '------------------------------------------------------------' -ForegroundColor DarkGray

# 释放句柄,防止资源泄漏
[System.GC]::Collect()
[System.GC]::WaitForPendingFinalizers()
# 清理可能残留的临时文件夹
# (-Fast 并行模式下跳过全局清扫: 多个进程同时在跑, 会删掉别的进程正在用的目录;
#  每个进程自己的 finally 已负责清理自己的目录; 新版临时目录放在输出根目录下,
#  这里只兜底清理旧版本留在 %TEMP% 里的 wallpaper_extract_* 目录)
if (-not $Fast) {
    Get-ChildItem ([System.IO.Path]::GetTempPath()) -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -like 'wallpaper_extract_*' } |
        ForEach-Object { Remove-Item $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }
    Remove-StaleTempDirs
}
