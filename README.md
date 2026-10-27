# Video_Replication

视频号内容复刻项目。素材拆解、抓取工具、参数库。

## 换机器后怎么接着干

```bash
git clone https://github.com/sumukeio/Video_Replication.git
cd Video_Replication
```

**然后重拉素材**（视频和原图不进仓库，因为体积大且有版权）：

```bash
# 用 erchuang 的虚拟环境跑（复用其元宝登录凭据）
E:/AIProject/erchuang/.venv/Scripts/python.exe wx_fetch.py \
  https://weixin.qq.com/sph/xxxxx -o 对标
```

链接都在各对标目录的 `meta.json` 里，照着贴就行。第一次需要扫码登录：

```bash
E:/AIProject/erchuang/bin/wx-downloader.exe login --browser chrome
```

## 目录

```
对标/
  00_横向拆解.md          ← 先看这个，八条样本的完整规格对比
  <账号>_<主题>/           ← 每条对标一个目录
      meta.json           作者、标题、链接、mediaType
      原片.mp4            不进仓库
      时间线.jpg / 分镜表.jpg   画面参考
      拆解.md             单条的结构分析
      images/             图集原图，不进仓库
```

## 工具

| 脚本 | 干什么 | 依赖 |
|---|---|---|
| `wx_fetch.py` | 抓视频号视频和图集原图（图集是 wx-downloader 不支持的） | erchuang 的 venv |
| `extract_shots.py` | 视频 → 镜头清单（关键帧 + 时长 + 联系表） | ffmpeg + Pillow + numpy |
| `carousel-studio.html` | 18 屏轮播排版工具，浏览器直接打开 | 无 |
| `sync_push.py` | 归档会话记录 + 提交 + 推送 | git |

## 约定

- **大文件不进仓库**：`.gitignore` 挡住了 `*.mp4` 和 `对标/**/images/`
- **会话记录**：`.gitignore` 里默认也挡住了 `会话记录/`。仓库改为 private 后删掉那一行，对话全文才会一起同步
- **推送时机**：一轮工作收尾时推一次，不在中间过程反复推

## 当前结论速查

八条对标的形态、节奏、设计规律，见 `对标/00_横向拆解.md`。核心三条：

1. 顶部固定主题条，每 2–5 秒重复一次
2. 字幕位置固定，白字黑边，永不移动
3. 画面是「配图」不是「作品」——零转场、零运镜、零动效，全是硬切
