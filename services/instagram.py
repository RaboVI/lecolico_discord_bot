import re
import random
import discord

# 預設可用之 Instagram 代理網域清單 (隨機負載平衡，降低單一伺服器被封鎖或失效機率)
DEFAULT_IG_PROXIES = [
    "oginstagram.com",
    "hhinstagram.com",
    "kkinstagram.com"
]


async def process_instagram_embed(raw_ig_url: str, message: discord.Message, pending_suppress_ids: set, proxies: list[str] | None = None):
    """
    非同步處理 Instagram 貼文轉址預覽
    - 隨機選擇代理網域
    - 發送隱藏網址文字之 Markdown 超連結
    - 登記並壓抑原始訊息預覽
    """
    proxy_list = proxies or DEFAULT_IG_PROXIES
    chosen_proxy = random.choice(proxy_list)

    # 將 instagram.com 替換為隨機選中的代理網域
    fix_ig_url = re.sub(r"instagram\.com", chosen_proxy, raw_ig_url)

    # 發送帶有超連結標題之代理訊息
    await message.channel.send(f"[⠀]({fix_ig_url})")

    # 登記訊息交由 on_message_edit 補刀壓抑
    pending_suppress_ids.add(message.id)
    try:
        await message.edit(suppress=True)
    except Exception as e:
        print(f"[Instagram] 無法隱藏原始訊息預覽: {e}")