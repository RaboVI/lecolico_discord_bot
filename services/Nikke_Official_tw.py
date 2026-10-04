import re
import asyncio
import requests
import discord
from bs4 import BeautifulSoup

# 偽裝一般桌面瀏覽器請求標頭
REQUEST_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
}


class NewsButtonView(discord.ui.View):
    """查看全文跳轉按鈕"""
    def __init__(self, news_url: str):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(
            label="📢 查看全文",
            url=news_url,
            style=discord.ButtonStyle.link
        ))


def _fetch_nikke_data_sync(raw_nikke_url: str) -> dict | None:
    """
    同步爬取與解析妮姬官方公告（在背景獨立執行緒執行）
    """
    try:
        res = requests.get(raw_nikke_url, headers=REQUEST_HEADERS, timeout=10)
        if res.status_code != 200:
            print(f"[Nikke] 請求失敗，狀態碼: {res.status_code}")
            return None

        res.encoding = 'utf-8'
        soup = BeautifulSoup(res.text, 'html.parser')

        # 1. 提取標題
        title_div = soup.find('div', class_='title')
        title = title_div.text.strip() if title_div else "《勝利女神：妮姬》最新公告"

        # 2. 提取日期與分類標籤
        date_str = ""
        tag_str = ""
        time_container = soup.find('div', class_=re.compile(r'\btime\b'))
        if time_container:
            spans = time_container.find_all('span')
            if len(spans) > 0:
                raw_date = spans[0].text.strip()
                date_str = raw_date.replace('.', '/')
            if len(spans) > 1:
                tag_str = spans[1].text.strip()

        # 3. 提取並清洗內文（包含懲處/封鎖名單表格摘要處理）
        clean_text = ""
        content_div = soup.find('div', id='content')
        if content_div:
            c_soup = BeautifulSoup(str(content_div), 'html.parser')

            # 處理公告內的處置名單表格
            table = c_soup.find('table')
            if table:
                rows = table.find_all('tr')
                formatted_rows = []
                for tr in rows[1:]:
                    cols = [td.get_text().strip() for td in tr.find_all(['td', 'th'])]
                    cols = [c for c in cols if c]
                    if cols:
                        if len(cols) >= 4:
                            formatted_rows.append(f"`• {cols[0]} | {cols[1]} | {cols[3]}`")
                        else:
                            formatted_rows.append(f"`• {' | '.join(cols)}`")

                preview_table_text = "\n".join(formatted_rows[:5])
                total_count = len(rows) - 1
                if total_count > 5:
                    preview_table_text += f"\n`... 等共 {total_count} 筆名單 (請點擊按鈕查看全文)`"

                table.replace_with(
                    BeautifulSoup(f"\n\n📋 **處置名單摘要**：\n{preview_table_text}\n\n", 'html.parser')
                )

            # 去除空 div 與多餘空白
            for div in c_soup.find_all('div'):
                if not div.text.replace('\xa0', '').strip():
                    div.decompose()

            raw_lines = c_soup.get_text(separator='\n').splitlines()
            clean_lines = [line.strip() for line in raw_lines if line.strip()]
            clean_text = "\n".join(clean_lines)

            # 內文上限 400 字截斷
            if len(clean_text) > 400:
                clean_text = clean_text[:400].rstrip() + "..."

        return {
            "title": title,
            "url": raw_nikke_url,
            "description": clean_text if clean_text else "點擊標題前往官方網站查看公告全文。",
            "date": date_str,
            "tag": tag_str
        }

    except Exception as e:
        print(f"[Nikke] 解析官網公告時發生錯誤: {e}")
        return None


async def process_nikke_embed(target_url: str, message: discord.Message, pending_suppress_ids: set):
    """
    非同步處理妮姬公告預覽發送與原訊息壓抑
    """
    # 透過 asyncio.to_thread 丟至背景執行緒，避免阻塞 Discord Bot 主事件迴圈
    data = await asyncio.to_thread(_fetch_nikke_data_sync, target_url)
    if not data:
        return

    # 組裝 Embed 卡片
    embed = discord.Embed(
        title=data["title"],
        url=data["url"],
        description=data["description"],
        color=0xF34A1B  # 妮姬經典橘紅色
    )

    # Footer 格式：勝利女神:妮姬 • 最新消息 • 2026/09/24
    footer_parts = ["勝利女神:妮姬"]
    if data["tag"]:
        footer_parts.append(data["tag"])
    if data["date"]:
        footer_parts.append(data["date"])
    embed.set_footer(text=" • ".join(footer_parts))

    # 發送帶有按鈕的卡片
    view = NewsButtonView(news_url=data["url"])
    await message.channel.send(embed=embed, view=view)

    # 登記並安全隱藏原始訊息預覽
    pending_suppress_ids.add(message.id)
    try:
        await message.edit(suppress=True)
    except Exception as e:
        print(f"[Nikke] 無法隱藏原始訊息預覽: {e}")