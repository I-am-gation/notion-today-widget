# 截图待补清单

README 引用了四张图，目前**都还不存在**。缺图不影响代码运行，但在 GitHub 上对应的图片会显示为破图——发布前请补齐，或先把 README 里对应的 `<img>` / `![]()` 注释掉。

| 文件 | 用在哪 | 要展示什么 | 优先级 |
|---|---|---|---|
| `widget.png` | README 首图 | 挂件贴在桌面右上角的整体效果。毛玻璃要能透出壁纸，标题栏「今日任务 09/13 周六」和三条任务清晰可读。**这张决定别人要不要点进来** | 最高 |
| `sync.gif` | 「效果」第 1 张 | 勾选 → 状态栏出现「已同步 Notion」→ 任务名划线变灰。3-5 秒循环即可 | 高 |
| `zorder-compare.png` | 「效果」第 2 张 | 左右分屏：「顶」模式按 Win+D 后挂件仍在；「桌」模式被资源管理器窗口盖住 | 中 |
| `tray-menu.png` | 「效果」第 3 张 | 托盘右键菜单，四个菜单项可读，「开机自启」带勾选状态 | 中 |

## 录制注意

- **必须用测试数据**。Notion 里如果是真实日程，录屏/截图前先换一批假任务，或把任务文字打码。
- **`config.json` 绝不能入镜**。它含明文令牌；截图前关掉编辑器里打开该文件的窗口。
- 壁纸建议用中性图片，避免暴露个人信息（相册、聊天窗口等）。
- GIF 控制在 2MB 以内，GitHub 上加载才顺畅。

## 快速生成占位图（可选）

如果只想先把破图问题解决，可以生成同尺寸的纯色占位图，之后再替换。
**在仓库根目录执行**（脚本会把文件写进 `docs/images/`，与 README 的引用路径一致）：

```bash
pip install pillow
python -c "
import os
from PIL import Image, ImageDraw
out = 'docs/images'
os.makedirs(out, exist_ok=True)
for name, size, text in [('widget.png',(380,260),'widget'), ('zorder-compare.png',(760,300),'zorder'), ('tray-menu.png',(320,220),'tray'), ('sync.gif',(380,260),'sync')]:
    img = Image.new('RGB', size, (32,32,40)); d = ImageDraw.Draw(img)
    d.text((12,12), 'PLACEHOLDER: '+text, fill=(200,200,210))
    img.save(os.path.join(out, name))
print('已写入', out)
"
```

> 占位图只是为了不显示破图，**不要**当成效果图发布。发布前必须换成真实截图——README 首图是别人判断这个项目值不值得看的唯一依据。
