# 壁纸提取器

把 Wallpaper Engine 壁纸包里的图片提取出来，存成普通图片文件（JPG、PNG 等），方便查看、收藏或当素材用。

Windows 桌面程序，有图形界面，也有命令行版本。

## 能做什么

- 支持 Wallpaper Engine 的 `scene.pkg`、`scene.mpkg`、`.mkg` 壁纸包，也支持单独的 `.tex` 纹理文件
- 网页型壁纸会直接收集里面的图片
- 普通图片、视频、动图（GIF、动图 WebP、APNG、AVIF）会原样提取，动画不会被拆掉
- 游戏用的 GPU 压缩纹理（DXT、LZ4 等）会自动转成正常图片
- 提取结果按壁纸分文件夹存放；重复提取会自动跳过，不会存出一堆重复文件

## 怎么用

### 图形界面（推荐）

1. 双击 `壁纸提取器.exe`
2. 把壁纸包（或整个壁纸文件夹）拖进窗口，自动开始提取
3. 提取完的图片按壁纸分文件夹放在 `已提取图片\` 里

界面里可以直接预览、全屏看、设置成桌面壁纸，也支持多选批量删除。

第一次打开如果 Windows 弹出 SmartScreen 提示，点「更多信息 → 仍要运行」就行，程序本身没问题。

### 源码版

需要先装 Python 和界面库 `PySide6-Essentials`，然后双击 `启动壁纸提取器.bat`，或者：

```
python wall_app.py
```

功能和 exe 版完全一样。如果系统开了「智能应用控制」（Smart App Control），exe 会被系统直接拦下，用这个方式运行即可。

### 命令行

把壁纸包拖到 `提取壁纸图片.bat` 上，或者在 PowerShell 里：

```powershell
powershell -ExecutionPolicy Bypass -File extract-wallpaper.ps1 "壁纸包路径"
```

常用参数：

- `-Force`：重新提取（默认跳过已经提取过的壁纸）
- `-OutDir "D:\输出目录"`：换个输出位置（默认是程序目录下的 `已提取图片`）
- `-NoOpen`：提取完不自动打开文件夹

## 文件说明

| 文件 / 文件夹 | 作用 |
| --- | --- |
| `壁纸提取器.exe` | 图形界面程序，双击就用（未签名） |
| `启动壁纸提取器.bat`、`wall_app.py` | 源码版入口，需要 Python + PySide6 |
| `提取壁纸图片.bat`、`extract-wallpaper.ps1` | 命令行提取入口 |
| `repkg\` | 提取引擎，exe 和脚本都靠它 |
| `壁纸提取器.spec`、`icon.ico` | 重新打包 exe 时用 |

这些文件需要放在同一个文件夹里（现在这样整体复制就行），不要单独挪走某一个。

## 自己重新打包（可选）

```
python -m PyInstaller --noconfirm --distpath . 壁纸提取器.spec
```

## 致谢

提取引擎用到了 [RePKG](https://github.com/notnotnotswipez/RePKG)，第三方组件说明见 `repkg\THIRD-PARTY-NOTICES.txt`。
