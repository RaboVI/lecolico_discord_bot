# Cloudflare Workers

各平台的 Worker 放在 `worker/<平台>/`，每個資料夾有獨立的 `worker.js` 與 `wrangler.toml`，彼此部署互不影響。

## 部署

```
cd worker/<平台>
npx wrangler deploy
```

部署前可先用 `npx wrangler deploy --dry-run` 試跑，不會動到線上版本。

## 目前的 Worker

| 資料夾 | Worker 名稱 | 自訂網域 |
| --- | --- | --- |
| `worker/facebook` | `lecolico` | `zusakvi.cc` |

- 自訂網域需與 `services/facebook.py` 的 `CUSTOM_WORKER_DOMAIN` 一致。
- `services/facebook.py` 會讀取 Worker 回傳的診斷標頭（`X-FB-Status`、`X-FB-Blocked`、`X-Video-Found`），修改其中一邊時請同步檢查另一邊。

## 注意事項

- 已存在的 Worker 不要改 `name`，否則會建立新的 Worker，舊的變成孤兒。
- 不要再直接在 Cloudflare 後台編輯程式，以此資料夾為唯一來源。
- 機密資料請用 `npx wrangler secret put <名稱>`，不要寫進程式碼或 `wrangler.toml`。
