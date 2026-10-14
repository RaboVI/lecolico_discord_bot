/**
 * LECO Bot - Threads 影片專用 Cloudflare Worker (threads.zusakvi.cc)
 * 對齊 fzthreads 標頭規範 + 嚴格鎖定 targetCode / postId + ActivityPub 協定偽裝
 */

const BASE64_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
const IG_APP_ID = "238260118697367";

function codeToPostId(code) {
  let cleanCode = code.split("?")[0].replace(/\/$/, "");
  let postId = 0n;
  for (let i = 0; i < cleanCode.length; i++) {
    const idx = BASE64_CHARS.indexOf(cleanCode[i]);
    if (idx === -1) continue;
    postId = (postId << 6n) + BigInt(idx);
  }
  return postId.toString();
}

function escapeHtml(str) {
  if (!str) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function formatCaption(rawText) {
  if (!rawText) return "";
  const lines = rawText
    .split(/\r?\n/)
    .map((l) => l.trim())
    .filter((l) => l.length > 0);
  let clean = lines.join("\n");
  if (clean.length > 100) {
    clean = clean.slice(0, 100).trimEnd() + "...";
  }
  return clean;
}

// 遞迴在 JSON 樹中尋找 code === targetCode 或 pk === targetPk 的貼文節點
function findPostInObject(obj, targetCode, targetPk) {
  if (!obj || typeof obj !== "object") return null;

  const objPk = String(obj.pk || obj.id || "").split("_")[0];
  if ((obj.code === targetCode || (targetPk && objPk === targetPk)) && obj.user && typeof obj.user === "object") {
    return obj;
  }

  if (Array.isArray(obj.thread_items)) {
    for (const item of obj.thread_items) {
      if (item && item.post) {
        const p = item.post;
        const pPk = String(p.pk || p.id || "").split("_")[0];
        if (p.code === targetCode || (targetPk && pPk === targetPk)) {
          return p;
        }
      }
    }
  }

  for (const key of Object.keys(obj)) {
    const found = findPostInObject(obj[key], targetCode, targetPk);
    if (found) return found;
  }
  return null;
}

// 從 Threads 抓取貼文結構化資料 (對齊 fzthreads 的 SSR + GraphQL 雙重機制)
async function fetchThreadsPost(code, username = "") {
  const targetUrl = username
    ? `https://www.threads.com/@${username}/post/${code}`
    : `https://www.threads.com/post/${code}`;
  const postId = codeToPostId(code);

  // 路徑 1：SSR HTML 挖掘 (不帶 Chrome UA 以避免觸發空殼 CSR，帶上 Sec-Fetch-Mode: navigate)
  try {
    const res = await fetch(targetUrl, {
      headers: {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1"
      },
      redirect: "follow"
    });

    if (res.ok) {
      const html = await res.text();
      const scriptRegex = /<script\s+type="application\/json"[^>]*>([\s\S]*?)<\/script>/gi;
      let match;
      while ((match = scriptRegex.exec(html)) !== null) {
        const content = match[1];
        if (
          (content.includes('"thread_items"') || content.includes('"text_post_app_info"')) &&
          (content.includes(code) || content.includes(postId))
        ) {
          try {
            const parsed = JSON.parse(content);
            const post = findPostInObject(parsed, code, postId);
            if (post) return post;
          } catch (e) {
            // 忽略單一區塊解析失敗
          }
        }
      }
    }
  } catch (e) {
    // 進入備援
  }

  // 路徑 2：GraphQL 端點備援 (完全對齊 fzthreads 的 Header 與 Cookie lsd 結構)
  try {
    const token = "LECOBotWorkerToken";
    const body = new URLSearchParams({
      variables: JSON.stringify({
        postID: postId,
        __relay_internal__pv__BarcelonaIsLoggedInrelayprovider: true,
        __relay_internal__pv__BarcelonaIsThreadContextHeaderEnabledrelayprovider: false,
        __relay_internal__pv__BarcelonaIsInlineReelsEnabledrelayprovider: true,
        __relay_internal__pv__BarcelonaOptionalCookiesEnabledrelayprovider: true,
        __relay_internal__pv__BarcelonaShowReshareCountrelayprovider: true
      }),
      doc_id: "7448805248491092",
      lsd: token
    });

    const gqlRes = await fetch("https://www.threads.com/api/graphql", {
      method: "POST",
      headers: {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://www.threads.com",
        "Referer": `${targetUrl}/`,
        "X-FB-Friendly-Name": "BarcelonaPostPageQuery",
        "X-IG-App-ID": IG_APP_ID,
        "X-FB-LSD": token,
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "Cookie": `lsd=${token};`
      },
      body: body.toString()
    });

    if (gqlRes.ok) {
      const gqlJson = await gqlRes.json();
      const post = findPostInObject(gqlJson, code, postId);
      if (post) return post;
    }
  } catch (e) {
    // 兩條路徑皆失敗
  }

  return null;
}

// 萃取貼文中的第一支影片與預覽縮圖 (支援主貼文、輪播、引用貼文與跨平台 IG Reels 內嵌節點)
function extractFirstVideo(node) {
  if (!node || typeof node !== "object") return null;

  // 1. 檢查輪播 (carousel_media)
  if (Array.isArray(node.carousel_media) && node.carousel_media.length > 0) {
    for (const item of node.carousel_media) {
      if (Array.isArray(item.video_versions) && item.video_versions.length > 0) {
        const vid = item.video_versions[0];
        const img = item.image_versions2?.candidates?.[0]?.url || "";
        return {
          url: vid.url,
          previewUrl: img,
          width: item.original_width || 720,
          height: item.original_height || 1280
        };
      }
    }
  }

  // 2. 檢查當前節點頂層 video_versions
  if (Array.isArray(node.video_versions) && node.video_versions.length > 0) {
    const vid = node.video_versions[0];
    const img = node.image_versions2?.candidates?.[0]?.url || "";
    return {
      url: vid.url,
      previewUrl: img,
      width: node.original_width || 720,
      height: node.original_height || 1280
    };
  }

  // 3. 遞迴往下搜尋子節點 (例如 text_post_app_info.share_info.quoted_post 或 linked_inline_media)
  for (const key of Object.keys(node)) {
    if (key === "user") continue;
    const found = extractFirstVideo(node[key]);
    if (found) return found;
  }

  return null;
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const host = url.host;
    const path = url.pathname;
    const userAgent = (request.headers.get("user-agent") || "").toLowerCase();
    const acceptHeader = (request.headers.get("accept") || "").toLowerCase();

    if (path === "/proxy") {
      const targetImg = url.searchParams.get("url");
      if (!targetImg) return new Response("Missing url", { status: 400 });
      try {
        const imgRes = await fetch(targetImg, {
          headers: {
            "User-Agent": "Mozilla/5.0 (compatible; Discordbot/2.0; +https://discordapp.com)"
          }
        });
        const newHeaders = new Headers(imgRes.headers);
        newHeaders.set("Access-Control-Allow-Origin", "*");
        newHeaders.set("Cross-Origin-Resource-Policy", "cross-origin");
        newHeaders.set("Cache-Control", "public, max-age=86400");
        return new Response(imgRes.body, { status: imgRes.status, headers: newHeaders });
      } catch (e) {
        return Response.redirect(targetImg, 302);
      }
    }

    if (path === "/oembed") {
      const site = url.searchParams.get("site") || "Threads";
      return new Response(
        JSON.stringify({
          provider_name: site,
          provider_url: "https://www.threads.com",
          type: "video",
          version: "1.0"
        }),
        { headers: { "Content-Type": "application/json; charset=utf-8" } }
      );
    }

    const userStatusMatch = path.match(/^\/users\/([^/]+)\/statuses\/([A-Za-z0-9_-]+)/);
    const apiStatusMatch = path.match(/^\/api\/v1\/statuses\/([A-Za-z0-9_-]+)/);

    if (userStatusMatch || apiStatusMatch || acceptHeader.includes("activity+json")) {
      let reqUser = "";
      let code = "";
      if (userStatusMatch) {
        reqUser = userStatusMatch[1];
        code = userStatusMatch[2];
      } else if (apiStatusMatch) {
        code = apiStatusMatch[1];
      } else {
        code = path.split("/").filter(Boolean).pop();
      }

      const post = await fetchThreadsPost(code, reqUser);
      if (!post) return new Response("Not Found", { status: 404 });

      const username = post.user?.username || reqUser || "threads";
      const fullName = post.user?.full_name || username;
      const rawAvatar = post.user?.profile_pic_url || "";
      const proxiedAvatar = rawAvatar
        ? `https://${host}/proxy?url=${encodeURIComponent(rawAvatar)}`
        : "";

      const rawCaption = post.caption?.text || "";
      const cleanCaption = formatCaption(rawCaption);
      const htmlContent = cleanCaption
        ? `<p>${escapeHtml(cleanCaption).replace(/\n/g, "<br/>")}</p>`
        : "";

      const videoInfo = extractFirstVideo(post);
      const mediaAttachments = [];
      if (videoInfo) {
        mediaAttachments.push({
          id: String(post.pk || code),
          type: "video",
          url: videoInfo.url,
          preview_url: videoInfo.previewUrl
            ? `https://${host}/proxy?url=${encodeURIComponent(videoInfo.previewUrl)}`
            : null,
          remote_url: null,
          meta: {
            original: {
              width: videoInfo.width,
              height: videoInfo.height
            }
          }
        });
      }

      const createdAtIso = post.taken_at
        ? new Date(post.taken_at * 1000).toISOString()
        : new Date().toISOString();
      const postUrl = `https://www.threads.com/@${username}/post/${code}`;

      const mastodonStatus = {
        id: code,
        url: postUrl,
        uri: postUrl,
        created_at: createdAtIso,
        content: htmlContent,
        spoiler_text: "",
        language: null,
        visibility: "public",
        application: { name: "Threads", website: "https://www.threads.com" },
        media_attachments: mediaAttachments,
        account: {
          id: username,
          display_name: `${fullName} (@${username})`,
          username: username,
          acct: username,
          url: `https://www.threads.com/@${username}`,
          uri: `https://www.threads.com/@${username}`,
          created_at: createdAtIso,
          locked: false,
          bot: false,
          discoverable: true,
          indexable: false,
          group: false,
          avatar: proxiedAvatar,
          avatar_static: proxiedAvatar,
          header: "",
          header_static: "",
          followers_count: 0,
          following_count: 0,
          statuses_count: 0,
          hide_collections: false,
          noindex: false,
          emojis: [],
          roles: [],
          fields: []
        },
        mentions: [],
        tags: [],
        emojis: [],
        card: null,
        poll: null
      };

      return new Response(JSON.stringify(mastodonStatus), {
        headers: {
          "Content-Type": "application/activity+json; charset=utf-8",
          "Cache-Control": "public, max-age=3600"
        }
      });
    }

    const userPostMatch = path.match(/^\/@([^/]+)\/post\/([A-Za-z0-9_-]+)/);
    const shortPostMatch = path.match(/^\/(?:post|t)\/([A-Za-z0-9_-]+)/);
    const targetThreadsUrl = `https://www.threads.com${path}${url.search}`;

    if (!userAgent.includes("discordbot") || (!userPostMatch && !shortPostMatch)) {
      return Response.redirect(targetThreadsUrl, 302);
    }

    const reqUser = userPostMatch ? userPostMatch[1] : "";
    const code = userPostMatch ? userPostMatch[2] : shortPostMatch[1];

    const post = await fetchThreadsPost(code, reqUser);
    if (!post) {
      return Response.redirect(targetThreadsUrl, 302);
    }

    const videoInfo = extractFirstVideo(post);
    if (!videoInfo) {
      return Response.redirect(targetThreadsUrl, 302);
    }

    const username = post.user?.username || reqUser || "threads";
    const fullName = post.user?.full_name || username;
    const likeCount = Number(post.like_count || 0).toLocaleString("en-US");
    const cleanCaption = formatCaption(post.caption?.text || "");
    const canonicalUrl = `https://www.threads.com/@${username}/post/${code}`;

    const footerSiteName = `Threads • ❤️ ${likeCount}`;
    const activityUrl = `https://${host}/users/${username}/statuses/${code}`;
    const oembedUrl = `https://${host}/oembed?site=${encodeURIComponent(footerSiteName)}`;

    const html = `<!DOCTYPE html>
<html lang="zh-TW">
<head>
  <meta charset="utf-8" />
  <meta name="theme-color" content="#E0E2E6" />
  <meta property="og:site_name" content="${escapeHtml(footerSiteName)}" />
  <meta property="og:title" content="${escapeHtml(`${fullName} (@${username})`)}" />
  <meta property="og:description" content="${escapeHtml(cleanCaption)}" />
  <meta property="og:url" content="${escapeHtml(canonicalUrl)}" />
  <meta property="og:type" content="video.other" />
  <meta property="og:video" content="${escapeHtml(videoInfo.url)}" />
  <meta property="og:video:secure_url" content="${escapeHtml(videoInfo.url)}" />
  <meta property="og:video:type" content="video/mp4" />
  <meta property="og:video:width" content="${videoInfo.width}" />
  <meta property="og:video:height" content="${videoInfo.height}" />
  <meta name="twitter:card" content="player" />
  <link rel="alternate" type="application/activity+json" href="${activityUrl}" />
  <link rel="alternate" type="application/json+oembed" href="${oembedUrl}" />
</head>
<body></body>
</html>`;

    return new Response(html, {
      headers: { "Content-Type": "text/html; charset=utf-8" }
    });
  }
};