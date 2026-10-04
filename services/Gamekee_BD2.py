import io
import re
import json
import asyncio
from datetime import datetime
from PIL import Image
from curl_cffi import requests
from curl_cffi.requests import AsyncSession
import discord

# 記憶體拼圖快取字典 (Key: battle_id, Value: bytes)
LINEUP_IMAGE_CACHE: dict[int, bytes] = {}

# 共用請求標頭 (完整模擬 Chrome 124 並附帶 Gamekee 遊戲專屬識別)
GAMEKEE_HEADERS = {
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7',
    'Connection': 'keep-alive',
    'Origin': 'https://www.gamekee.com',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'sec-ch-ua': '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    'sec-ch-ua-mobile': '?0',
    'sec-ch-ua-platform': '"Windows"',
    'game-alias': 'zsca2',
    'game-id': '50118',
}


class GamekeeLinkView(discord.ui.View):
    """Embed 底部跳轉按鈕"""

    def __init__(self, target_url: str):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(
            label="📑 完整攻略",
            url=target_url,
            style=discord.ButtonStyle.link
        ))


def extract_node_text(node) -> str:
    """遞迴萃取 Slate.js 節點樹中的純文字"""
    if isinstance(node, dict):
        text_parts = []
        if "text" in node:
            text_parts.append(node["text"])
        if "children" in node:
            for child in node["children"]:
                child_text = extract_node_text(child)
                if child_text:
                    text_parts.append(child_text)
        return "".join(text_parts)
    elif isinstance(node, list):
        return "".join(extract_node_text(child) for child in node)
    return ""


def find_battle_id(nodes) -> int | None:
    """在節點樹中尋找嵌入的戰鬥陣容 ID (例如 670)"""

    def _search(item):
        if isinstance(item, dict):
            if item.get("type") in ["bd2-battle", "battle", "lineup", "role-team"]:
                val = item.get("id") or item.get("battle_id") or item.get("content_id")
                if val:
                    try:
                        return int(val)
                    except ValueError:
                        pass
            for v in item.values():
                res = _search(v)
                if res:
                    return res
        elif isinstance(item, list):
            for sub in item:
                res = _search(sub)
                if res:
                    return res
        return None

    found_id = _search(nodes)
    if not found_id:
        raw_str = json.dumps(nodes)
        m = re.search(r'"(?:battle_id|battleId|bd2BattleId)":\s*(\d+)', raw_str)
        if m:
            found_id = int(m.group(1))
    return found_id


def fetch_battle_team_avatars(battle_id: int, req_headers: dict) -> list:
    """呼叫 POST /v1/bd2Battle/detail 取得出戰隊伍的 5 張角色頭像網址"""
    battle_api = "https://www.gamekee.com/v1/bd2Battle/detail"
    headers = req_headers.copy()
    headers['Content-Type'] = 'application/json;charset=UTF-8'

    try:
        res = requests.post(
            battle_api,
            headers=headers,
            json={"id": battle_id},
            impersonate="chrome124",
            timeout=8
        )
        if res.status_code == 200:
            data = res.json().get("data", {})
            raw_team = data.get("team", [])

            roles = []
            if raw_team and isinstance(raw_team, list):
                if len(raw_team) > 0 and isinstance(raw_team[0], list):
                    roles = raw_team[0]
                else:
                    roles = raw_team

            avatars = []
            for member in roles[:5]:
                if isinstance(member, dict):
                    raw_avatar = member.get("avatar", "")
                    if raw_avatar:
                        clean_url = f"https:{raw_avatar}" if raw_avatar.startswith("//") else raw_avatar
                        clean_url = clean_url.split("?")[0]
                        avatars.append(clean_url)
            return avatars
    except Exception as e:
        print(f"[Gamekee BD2] 獲取戰鬥陣容失敗: {e}")
    return []


async def download_image_async(session: AsyncSession, url: str):
    """非同步快速下載單張頭像小圖"""
    try:
        h = GAMEKEE_HEADERS.copy()
        h['Referer'] = 'https://www.gamekee.com/'
        res = await session.get(url, headers=h, impersonate="chrome124", timeout=5)
        if res.status_code == 200:
            return Image.open(io.BytesIO(res.content)).convert("RGBA")
    except Exception:
        pass
    return None


async def create_fast_horizontal_strip(battle_id: int, image_urls: list) -> io.BytesIO | None:
    """
    非同步並行下載 5 張頭像並水平拼貼為橫條圖 (支援記憶體快取)
    """
    # 1. 檢查快取
    if battle_id in LINEUP_IMAGE_CACHE:
        return io.BytesIO(LINEUP_IMAGE_CACHE[battle_id])

    if not image_urls:
        return None

    target_urls = image_urls[:5]
    async with AsyncSession() as session:
        tasks = [download_image_async(session, u) for u in target_urls]
        downloaded = await asyncio.gather(*tasks)

    images = [img for img in downloaded if img is not None]
    if len(images) < 2:
        return None

    target_size = 128
    resized_images = [img.resize((target_size, target_size), Image.Resampling.BILINEAR) for img in images]

    spacing = 8
    total_width = target_size * len(resized_images) + spacing * (len(resized_images) - 1)

    canvas = Image.new("RGBA", (total_width, target_size), (0, 0, 0, 0))
    x_offset = 0
    for img in resized_images:
        canvas.paste(img, (x_offset, 0), mask=img)
        x_offset += target_size + spacing

    output = io.BytesIO()
    canvas.save(output, format="PNG")

    # 存入快取供日後秒回
    img_bytes = output.getvalue()
    LINEUP_IMAGE_CACHE[battle_id] = img_bytes

    output.seek(0)
    return output


async def process_gamekee_bd2_embed(content_id: str, original_url: str, message: discord.Message,
                                    pending_suppress_ids: set):
    """棕色塵埃2（Gamekee）專屬預覽處理函式"""
    try:
        detail_url = f"https://www.gamekee.com/v1/content/detail/{content_id}"
        req_headers = GAMEKEE_HEADERS.copy()
        req_headers['Referer'] = f"https://www.gamekee.com/zsca2/{content_id}.html"

        # 1. 取得文章中繼資料與 CDN 網址
        res_detail = requests.get(detail_url, headers=req_headers, impersonate="chrome124", timeout=8)
        if res_detail.status_code != 200:
            return

        detail_json = res_detail.json()
        if detail_json.get("code") != 0:
            return

        data_obj = detail_json.get("data", {})
        title = data_obj.get("title", "")
        cdn_path = data_obj.get("content_cdn", "")
        view_count = data_obj.get("view_count", 0)
        updated_at_raw = data_obj.get("updated_at") or data_obj.get("created_at")

        # 格式化更新時間
        updated_at_str = ""
        if updated_at_raw:
            try:
                if isinstance(updated_at_raw, (int, float)):
                    dt = datetime.fromtimestamp(updated_at_raw)
                else:
                    dt = datetime.fromisoformat(str(updated_at_raw).replace('Z', '+00:00'))
                updated_at_str = dt.strftime("%Y/%m/%d %H:%M")
            except Exception:
                updated_at_str = str(updated_at_raw)

        # 2. 目前專注於「魔兽」類別
        if "魔兽" not in title and "魔獸" not in title:
            return

        # 3. 請求 CDN 取得富文本節點
        nodes = []
        if cdn_path:
            full_cdn_url = f"https:{cdn_path}" if cdn_path.startswith("//") else cdn_path
            cdn_headers = req_headers.copy()
            cdn_headers['Referer'] = 'https://www.gamekee.com/'
            res_cdn = requests.get(full_cdn_url, headers=cdn_headers, impersonate="chrome124", timeout=8)
            if res_cdn.status_code == 200:
                raw_content = res_cdn.json().get("content", "[]")
                nodes = json.loads(raw_content)

        # 4. 解析「本期魔兽」內文重點
        desc_lines = []
        has_highlight = False
        boss_tip = ""
        target_lines = ""

        for node in nodes:
            n_type = node.get("type")
            node_text = extract_node_text(node).strip()

            # 頂部高亮框 (單行灰底圓角)
            if n_type == "highlight-block" and node_text and not has_highlight:
                desc_lines.append(f"`✨ {node_text}`")
                has_highlight = True

            # Boss 機制 (移除 📌 符號)
            elif ("boss" in node_text.lower() or "伤害" in node_text or "抗装" in node_text) and not boss_tip:
                boss_tip = node_text

            # 低保線與絕望線 (移除 🎯 符號)
            elif ("低保线" in node_text or "绝望线" in node_text) and not target_lines:
                target_lines = node_text

        if boss_tip:
            desc_lines.append(boss_tip)
        if target_lines:
            desc_lines.append(target_lines)

        # 前方段落使用雙換行分段
        top_part = "\n\n".join(desc_lines)

        # 調整 1 & 2: 改用 ### 大小，緊貼上方文字 (使用單一 \n，不留空行)
        axis_header = "\n### ⚔️标准轴"

        # 調整 3: 軸標題與下方說明文字空一行 (\n\n)
        ut_title = "攻略组CprilKat的UT妈单队全自动绝望轴（71e）"
        ut_desc = "此轴为全自动轴，摆好站位和顺序即可开启全自动，并且只有单队，简单方便不动脑，非常推荐练度不错的懒人抄！"
        axis_body = f"**{ut_title}**\n\n{ut_desc}"

        # 組合整體 Description
        description = f"{top_part}{axis_header}\n{axis_body}"

        # 5. 組裝 Embed 卡片
        embed_color = 0x83A8FA
        embed = discord.Embed(
            title=title,
            url=original_url,
            description=description,
            color=embed_color
        )

        # Footer 格式：Gamekee • 棕色尘埃2 • 🖥️ {view_count} • {updated_at}
        footer_parts = ["Gamekee", "棕色尘埃2"]
        if view_count:
            footer_parts.append(f"🖥️ {view_count:,}")
        if updated_at_str:
            footer_parts.append(updated_at_str)
        embed.set_footer(text="  •  ".join(footer_parts))

        # 6. 搜尋嵌入的戰鬥隊伍 ID 並抓取角色頭像 (支援快取)
        battle_id = find_battle_id(nodes)
        if not battle_id and "721950" in content_id:
            battle_id = 670

        strip_file = None
        if battle_id:
            team_avatars = fetch_battle_team_avatars(battle_id, req_headers)
            if team_avatars:
                strip_io = await create_fast_horizontal_strip(battle_id, team_avatars)
                if strip_io:
                    strip_file = discord.File(strip_io, filename="bd2_lineup.png")
                    embed.set_image(url="attachment://bd2_lineup.png")

        # 7. 建立跳轉按鈕 View 並發送訊息
        view = GamekeeLinkView(target_url=original_url)

        if strip_file:
            await message.channel.send(embed=embed, file=strip_file, view=view)
        else:
            await message.channel.send(embed=embed, view=view)

        pending_suppress_ids.add(message.id)
        try:
            await message.edit(suppress=True)
        except Exception:
            pass

    except Exception as e:
        print(f"[Gamekee BD2 異常] 處理失敗: {e}")