import io
import re
import json
import time
import asyncio
from datetime import datetime
from PIL import Image
from curl_cffi import requests
from curl_cffi.requests import AsyncSession
import discord

# --- 輕量級 TTL 記憶體快取 (Key: battle_id, Value: (timestamp, image_bytes)) ---
LINEUP_IMAGE_CACHE: dict[int, tuple[float, bytes]] = {}
CACHE_TTL = 86400  # 快取存活時間：24小時 (86400秒)

# 共用請求標頭
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


def extract_node_images(node) -> list:
    """備用方案：遞迴尋找一般靜態圖片網址"""
    images = []
    if isinstance(node, dict):
        src = node.get("src") or node.get("url") or node.get("img")
        if src and isinstance(src, str) and any(
                ext in src.lower() for ext in [".png", ".jpg", ".jpeg", ".webp", ".gif"]):
            clean_url = f"https:{src}" if src.startswith("//") else src
            images.append(clean_url.split("?")[0])
        for v in node.values():
            if isinstance(v, (dict, list)):
                images.extend(extract_node_images(v))
    elif isinstance(node, list):
        for sub in node:
            images.extend(extract_node_images(sub))
    return images


def find_battle_id(nodes) -> int | None:
    """尋找陣容 ID (深度防呆挖掘版，支援 tempId)"""

    def _search(item):
        if isinstance(item, dict):
            n_type = item.get("type", "").lower()

            # 1. 檢查是否為自訂陣容模組
            if any(k in n_type for k in ["battle", "team", "lineup", "guild", "role", "module", "component"]):
                val = item.get("id") or item.get("battle_id") or item.get("content_id") or item.get(
                    "team_id") or item.get("tempId")
                if val:
                    try:
                        v_int = int(val)
                        if 10 < v_int < 100000:
                            return v_int
                    except ValueError:
                        pass

                # 深層尋找：Gamekee 經常把真實 ID 藏在 "data" 字典裡
                if "data" in item and isinstance(item["data"], dict):
                    val = item["data"].get("id") or item["data"].get("tempId")
                    if val:
                        try:
                            v_int = int(val)
                            if 10 < v_int < 100000:
                                return v_int
                        except ValueError:
                            pass

            for v in item.values():
                res = _search(v)
                if res: return res
        elif isinstance(item, list):
            for sub in item:
                res = _search(sub)
                if res: return res
        return None

    found_id = _search(nodes)
    if found_id:
        return found_id

    # 2. 終極備案：直接對 JSON 字串進行正則檢索 (限 2~5 位數的安全 ID，加入 tempId)
    raw_str = json.dumps(nodes)
    m = re.search(r'"(?:id|battle_id|team_id|tempId)":\s*"?(\d{2,5})"?\b', raw_str, re.IGNORECASE)
    if m:
        return int(m.group(1))

    return None


def fetch_battle_team_data(battle_id: int, req_headers: dict) -> dict:
    """呼叫 API 取得出戰隊伍資料"""
    battle_api = "https://www.gamekee.com/v1/bd2Battle/detail"
    headers = req_headers.copy()
    headers['Content-Type'] = 'application/json;charset=UTF-8'

    result = {"avatars": [], "title": "", "desc": ""}
    try:
        res = requests.post(battle_api, headers=headers, json={"id": battle_id}, impersonate="chrome124", timeout=8)
        if res.status_code == 200:
            data = res.json().get("data", {})
            raw_team = data.get("team", [])

            result["title"] = data.get("title", "")
            result["desc"] = data.get("desc", "")

            team_groups = []
            if isinstance(raw_team, list):
                if len(raw_team) > 0 and isinstance(raw_team[0], list):
                    team_groups = raw_team
                else:
                    team_groups = [raw_team]

            avatar_groups = []
            for group in team_groups:
                current_row_avatars = []
                for member in group:
                    if isinstance(member, dict):
                        raw_avatar = member.get("avatar", "")
                        if raw_avatar:
                            clean_url = f"https:{raw_avatar}" if raw_avatar.startswith("//") else raw_avatar
                            clean_url = clean_url.split("?")[0]
                            if clean_url not in current_row_avatars:
                                current_row_avatars.append(clean_url)
                if current_row_avatars:
                    avatar_groups.append(current_row_avatars)

            result["avatars"] = avatar_groups
    except Exception as e:
        print(f"[Gamekee BD2] 獲取戰鬥陣容失敗: {e}")
    return result


async def download_image_async(session: AsyncSession, url: str):
    """非同步下載單張頭像小圖"""
    try:
        h = GAMEKEE_HEADERS.copy()
        h['Referer'] = 'https://www.gamekee.com/'
        res = await session.get(url, headers=h, impersonate="chrome124", timeout=5)
        if res.status_code == 200:
            return url, Image.open(io.BytesIO(res.content)).convert("RGBA")
    except Exception:
        pass
    return url, None


async def create_multi_row_lineup_strip(battle_id: int, avatar_groups: list) -> io.BytesIO | None:
    """動態多排陣容圖合成器 (支援 TTL 快取)"""
    if battle_id in LINEUP_IMAGE_CACHE:
        ts, img_bytes = LINEUP_IMAGE_CACHE[battle_id]
        if time.time() - ts < CACHE_TTL:
            return io.BytesIO(img_bytes)

    if not avatar_groups:
        return None

    unique_urls = list(set([url for group in avatar_groups for url in group]))
    if not unique_urls:
        return None

    async with AsyncSession() as session:
        tasks = [download_image_async(session, u) for u in unique_urls]
        downloaded_results = await asyncio.gather(*tasks)

    img_dict = {url: img for url, img in downloaded_results if img is not None}
    if not img_dict:
        return None

    target_size = 128
    spacing_x = 8
    spacing_y = 12

    rows = []
    max_row_width = 0

    for group in avatar_groups:
        row_imgs = []
        for url in group:
            if url in img_dict:
                resized_img = img_dict[url].resize((target_size, target_size), Image.Resampling.BILINEAR)
                row_imgs.append(resized_img)

        if row_imgs:
            row_width = len(row_imgs) * target_size + (len(row_imgs) - 1) * spacing_x
            rows.append({"images": row_imgs, "width": row_width})
            if row_width > max_row_width:
                max_row_width = row_width

    if not rows:
        return None

    total_width = max_row_width
    total_height = len(rows) * target_size + (len(rows) - 1) * spacing_y

    canvas = Image.new("RGBA", (total_width, total_height), (0, 0, 0, 0))

    # 逐排靠左貼圖 (同網頁排版)
    y_offset = 0
    for row in rows:
        x_offset = 0  # 起始點固定在最左側
        for img in row["images"]:
            canvas.paste(img, (x_offset, y_offset), mask=img)
            x_offset += target_size + spacing_x
        y_offset += target_size + spacing_y

    output = io.BytesIO()
    canvas.save(output, format="PNG")
    img_bytes = output.getvalue()

    LINEUP_IMAGE_CACHE[battle_id] = (time.time(), img_bytes)

    output.seek(0)
    return output


async def process_gamekee_bd2_embed(content_id: str, original_url: str, message: discord.Message,
                                    pending_suppress_ids: set):
    """棕色塵埃2（Gamekee）專屬預覽處理函式"""
    try:
        detail_url = f"https://www.gamekee.com/v1/content/detail/{content_id}"
        req_headers = GAMEKEE_HEADERS.copy()
        req_headers['Referer'] = f"https://www.gamekee.com/zsca2/{content_id}.html"

        res_detail = requests.get(detail_url, headers=req_headers, impersonate="chrome124", timeout=8)
        if res_detail.status_code != 200 or res_detail.json().get("code") != 0:
            return

        data_obj = res_detail.json().get("data", {})
        title = data_obj.get("title", "")
        cdn_path = data_obj.get("content_cdn", "")
        view_count = data_obj.get("view_count", 0)
        updated_at_raw = data_obj.get("updated_at") or data_obj.get("created_at")

        updated_at_str = ""
        if updated_at_raw:
            try:
                dt = datetime.fromtimestamp(updated_at_raw) if isinstance(updated_at_raw,
                                                                          (int, float)) else datetime.fromisoformat(
                    str(updated_at_raw).replace('Z', '+00:00'))
                updated_at_str = dt.strftime("%Y/%m/%d %H:%M")
            except Exception:
                pass

        if not any(k in title for k in ["魔兽", "魔獸", "会战", "公会战"]):
            return

        nodes = []
        if cdn_path:
            full_cdn = f"https:{cdn_path}" if cdn_path.startswith("//") else cdn_path
            cdn_headers = req_headers.copy()
            cdn_headers['Referer'] = 'https://www.gamekee.com/'
            res_cdn = requests.get(full_cdn, headers=cdn_headers, impersonate="chrome124", timeout=8)
            if res_cdn.status_code == 200:
                nodes = json.loads(res_cdn.json().get("content", "[]"))

        desc_lines = []
        has_highlight = False
        boss_tips = []
        target_lines = ""
        fallback_axis_title = ""

        for node in nodes:
            n_type = node.get("type")
            node_text = extract_node_text(node).strip()

            if not node_text:
                continue

            # 排除垃圾訊息
            if "注：" in node_text or "萌新" in node_text or "配置" in node_text:
                continue

            # 頂部高亮
            if n_type == "highlight-block" and not has_highlight:
                desc_lines.append(f"`✨ {node_text}`")
                has_highlight = True
                continue

            # 嚴格過濾 Boss 機制
            lower_text = node_text.lower()
            is_boss_tip = (
                    ("boss" in lower_text and ("伤害" in lower_text or "抗" in lower_text)) or
                    "本期魔兽伤害" in node_text or
                    "本期主力" in node_text or
                    "全员穿" in node_text
            )
            # 最多只抓 2 條核心機制，且限制字數小於 120 字
            if is_boss_tip and len(node_text) < 120 and len(boss_tips) < 2 and node_text not in boss_tips:
                boss_tips.append(node_text)
                continue

            # 低保線與絕望線
            if ("低保线" in node_text or "绝望线" in node_text) and not target_lines:
                target_lines = node_text
                continue

            # 備用抓取：捕捉作者手打的標題
            if ("标准轴" in node_text or "高配轴" in node_text or "全自动" in node_text) and len(node_text) < 40:
                if not fallback_axis_title:
                    fallback_axis_title = node_text.lstrip(" |｜").strip()

        if boss_tips:
            desc_lines.append("\n".join(boss_tips))
        if target_lines:
            desc_lines.append(target_lines)

        top_part = "\n\n".join(desc_lines)

        battle_id = find_battle_id(nodes)

        axis_header_text = fallback_axis_title if fallback_axis_title else "标准轴"
        axis_header = f"\n### ⚔️{axis_header_text}"

        axis_body = ""
        strip_file = None
        embed_image_url = None

        if battle_id:
            team_data = fetch_battle_team_data(battle_id, req_headers)

            if team_data["title"]:
                axis_body = f"__**{team_data['title']}**__"
                if team_data["desc"]:
                    axis_body += f"\n\n{team_data['desc']}"

            if team_data["avatars"]:
                strip_io = await create_multi_row_lineup_strip(battle_id, team_data["avatars"])
                if strip_io:
                    strip_file = discord.File(strip_io, filename="bd2_lineup.png")
                    embed_image_url = "attachment://bd2_lineup.png"

        # 終極防呆：如果 API 沒抓到陣容大圖，去內文撈靜態圖片頂替
        if not embed_image_url:
            static_imgs = extract_node_images(nodes)
            seen = set()
            static_imgs = [x for x in static_imgs if not (x in seen or seen.add(x))]
            if static_imgs:
                embed_image_url = static_imgs[1] if len(static_imgs) > 1 else static_imgs[0]

        description = f"{top_part}{axis_header}\n{axis_body}" if axis_body else f"{top_part}{axis_header}"

        embed = discord.Embed(
            title=title,
            url=original_url,
            description=description,
            color=0x83A8FA
        )

        footer_parts = ["Gamekee", "棕色尘埃2"]
        if view_count: footer_parts.append(f"🖥️ {view_count:,}")
        if updated_at_str: footer_parts.append(updated_at_str)
        embed.set_footer(text="  •  ".join(footer_parts))

        if embed_image_url:
            embed.set_image(url=embed_image_url)

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