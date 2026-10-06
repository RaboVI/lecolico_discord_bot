import re
import discord

# vxBilibili 官方明訂之支援路由路徑與二級網域 (排除 /toy/ 等非支援網頁小遊戲)
BILIBILI_SUPPORTED_PATTERN = r"""(?x)
https?://(?:www\.|m\.)?bilibili\.com/(?:
    video/[Bb][Vv][a-zA-Z0-9]+ |           # 一般影片 (BV)
    video/av\d+ |                          # 一般影片 (av)
    bangumi/play/(?:ep|ss)\d+ |            # 番劇 / 影劇 (ep/ss)
    opus/\d+ |                             # 新版動態 (opus)
    read/cv\d+                             # 專欄文章 (cv)
)
| https?://t\.bilibili\.com/\d+            # 舊版動態 (t.bilibili.com)
| https?://live\.bilibili\.com/\d+         # 實況直播 (live.bilibili.com)
| https?://space\.bilibili\.com/\d+        # 個人空間 (space.bilibili.com)
| https?://manga\.bilibili\.com/detail/mc\d+ # 漫畫作品 (manga.bilibili.com)
| https?://mall\.bilibili\.com/\S+         # 會員購 (mall.bilibili.com)
| https?://b23\.tv/[a-zA-Z0-9]+            # 短網址 (b23.tv 全面放行)
"""


def is_vxbilibili_supported(url: str) -> bool:
    """檢查 B 站網址是否符合 vxBilibili 的支援清單"""
    return bool(re.search(BILIBILI_SUPPORTED_PATTERN, url))


async def process_bilibili_embed(raw_bili_url: str, message: discord.Message, pending_suppress_ids: set):
    """
    處理 Bilibili 轉址預覽
    - 僅在符合官方支援範圍時進行代理
    - b23.tv 轉為 vxb23.tv，bilibili.com 轉為 vxbilibili.com
    - 使用不可見字元超連結 [⠀] 維持畫面純淨
    - 登記並壓抑原始訊息預覽
    """
    # 嚴格校驗：若不屬於 vxBilibili 支援範圍 (例如小遊戲 /toy/) 則直接跳過，保留原生行為
    if not is_vxbilibili_supported(raw_bili_url):
        return

    # 依網域精準替換
    if "b23.tv" in raw_bili_url:
        fix_url = raw_bili_url.replace("b23.tv", "vxb23.tv")
    else:
        # 使用正規表達式只替換主網域名稱，避免誤傷路徑或參數
        fix_url = re.sub(r"(www\.|m\.)?bilibili\.com", "vxbilibili.com", raw_bili_url)

    # 發送帶有零寬空白之隱形超連結，版面極致純淨
    await message.channel.send(f"[⠀]({fix_url})")

    # 登記訊息交由 on_message_edit 補刀壓抑
    pending_suppress_ids.add(message.id)
    try:
        await message.edit(suppress=True)
    except Exception as e:
        print(f"[Bilibili] 無法隱藏原始預覽: {e}")