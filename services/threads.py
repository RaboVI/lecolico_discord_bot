import re
import random
import discord

# Threads 代理網域清單 (隨機負載平衡，降低單一伺服器當機或被風控的風險)
DEFAULT_THREADS_PROXIES = [
    "fzthreads.com",             # 支援內部 GraphQL 與 App 端點解析
    "fixthreads.seria.moe"       # 既有的社群穩定代理
]


async def process_threads_embed(raw_threads_url: str, message: discord.Message, pending_suppress_ids: set, proxies: list[str] | None = None):
    """
    非同步處理 Threads 貼文轉址預覽
    - 支援 threads.net 與 threads.com
    - 隨機負載平衡選取 fzthreads.com 或 fixthreads.seria.moe
    - 使用不可見字元超連結 [⠀] 避免破壞 Discord Markdown 語法
    - 登記並壓抑原始訊息預覽
    """
    proxy_list = proxies or DEFAULT_THREADS_PROXIES
    chosen_proxy = random.choice(proxy_list)

    # 精確替換網域名稱 (threads.net 或 threads.com) 為代理網域
    fix_threads_url = re.sub(r"(www\.)?threads\.(net|com)", chosen_proxy, raw_threads_url)

    # 發送帶有零寬空白之隱形超連結，版面極致純淨，只展開下方卡片/播放器
    await message.channel.send(f"[⠀]({fix_threads_url})")

    # 登記訊息交由 on_message_edit 補刀壓抑
    pending_suppress_ids.add(message.id)
    try:
        await message.edit(suppress=True)
    except Exception as e:
        print(f"[Threads] 無法隱藏原始訊息預覽: {e}")