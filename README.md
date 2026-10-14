# LINE 官方帳號客服輔助系統

## 專案說明

這個專案是給補習班內部使用的 LINE 官方帳號客服輔助工具。
主要是把家長傳來的訊息整理到後台，再依照 FAQ 找出可能的答案，讓櫃檯或老師可以更快處理常見問題。

系統預設以「人工確認後再回覆」為主，不會直接把模型產生的內容自動傳給家長。只有經過確認、符合條件的
FAQ，才可以由 Super User 開啟自動回覆功能。

目前使用 FastAPI 作為後端，透過 LINE Messaging API 接收訊息
FAQ 檢索使用 Ollama 的 `bge-m3`，語言模型名稱則可在 `.env` 裡設定。

## 一、執行環境

-   作業系統：Windows
-   Python：3.9 以上
-   後端：FastAPI、Uvicorn
-   LINE 串接：LINE Messaging API、line-bot-sdk v3
-   FAQ 檢索：Ollama、`bge-m3`
-   語言模型：依 `.env` 的 `LLM_MODEL` 設定
-   資料儲存：SQLite，資料庫檔案為 `data/app.db`

## 二、安裝與啟動

### 1. 安裝套件與模型

在專案目錄開啟終端機，執行：

``` bash
pip install -r requirements.txt
ollama pull bge-m3
ollama list
```

`bge-m3` 用於 FAQ 檢索，程式會設定成使用 CPU。
接著確認 Ollama 裡有專案要使用的語言模型，例如本專案使用的`qwen3.5:9b`。
如果實際安裝的模型名稱不同，記得修改 `.env` 的`LLM_MODEL`。

### 2. 設定 LINE 與環境變數

先到 LINE Official Account Manager，確認已啟用 Messaging API。再到 LINE
Developers Console 的 Channel 頁面取得：

-   Channel secret
-   Channel access token（long-lived）

將 `.env.example` 複製成 `.env`，填入上述設定，並設定後台登入密碼：

-   `ADMIN_PASS`：一般管理員使用
-   `SUPER_PASS`：Super User 使用，需與一般管理員密碼不同

請勿把正式的 Token、Secret 或密碼提交到 Git，也不要把 `.env` 分享出去。

在 LINE Official Account Manager
的回應設定中，將「聊天」與「Webhook」開啟。原有的自動回應設定可以先維持不變，避免影響原本的圖文選單或關鍵字回覆。

### 3. 編輯 FAQ

使用 Excel 開啟 `data/faq.csv`。修改後請選擇「CSV
UTF-8」格式儲存，避免中文亂碼。

欄位編輯時要注意：

-   `question` 可以用 `|`
    分隔多種問法，例如同一個問題可能會被問成「單堂課多少錢」或「一堂課的費用是多少」。
-   答案中的價格、地址與營業時間，應先向補習班確認最新資料。
-   `【請填入…】` 是尚未補齊的占位文字，不能當成正式答案使用。
-   若要允許某一筆 FAQ 自動回覆，需確認答案正確後，再將該列的 `auto`
    欄設為 `Y`。

範例中的營業時間與地址是依 2026 年 3
月的既有自動回覆整理，正式使用前仍要再次核對。

### 4. 啟動後端

在專案目錄執行：

``` bash
python -m uvicorn main:app --host 127.0.0.1 --port 8000
```
或使用對外服務
``` bash
python -m uvicorn main:app --host 0.0.0.0. --port 8000
```

終端機出現「FAQ 載入完成」後，就可以在瀏覽器開啟：

<http://localhost:8000>
<https://domainName:8000>

使用後台帳號與密碼登入。

### 5. 設定 LINE Webhook

LINE 的 Webhook 需要使用外部可以連線的 HTTPS 網址。開發測試時，可以使用
Cloudflare Tunnel：

``` bash
cloudflared tunnel --url http://localhost:8000
```

啟動後會取得一個 `https://xxxx.trycloudflare.com` 網址。將它填入 LINE
Developers Console 的 Webhook URL，完整路徑如下：

``` text
https://linebot.shuweis.com/callback 
```

接著按下 Verify，並確認 Use webhook 已開啟。

<img width="1892" height="608" alt="image" src="https://github.com/user-attachments/assets/ef538a50-7623-42d0-bd46-2972f6406c40" />

如未購買 Domain Name 或未使用 SSL 可使用 ngrok 服務作為臨時用途。

對外開放通道時，後台需要保留登入保護 LINE 的 `/callback`也會驗證請求簽章。
不要因為是測試環境就把密碼或 Token 寫在程式裡。

## 三、基本測試流程

1.  確認後端程式正在執行，FAQ 已成功載入。
2.  確認 LINE Webhook Verify 通過。
3.  使用自己的 LINE 傳送「單堂課多少錢」給官方帳號。
4.  等待約 20 秒，讓系統收完同一段連續訊息後，再到後台確認草稿是否出現。
5.  檢查草稿內容是否符合 FAQ 。確認無誤後，先複製到 LINE Official Account
    Manager，再由人工送出並按下「已回覆」。

若需要取得自己的 LINE userId，可以傳送 `#myid`。只有在需要使用
`PUSH_TO_OWNER` 通知時才需要這項資訊。

## 四、訊息處理方式

### 預設流程：人工確認

系統收到家長訊息後，會先判斷是否需要處理，再整理成後台草稿。一般情況下，操作人員複製草稿，到
LINE Official Account Manager
的聊天室手動回覆，最後回到後台按「已回覆」。

在這個流程下，系統不會自動把草稿傳給家長。由於透過 OA Manager
手動送出的訊息不會回傳到
Webhook，後台主要記錄的是家長傳入的訊息，不一定能完整呈現雙方對話。

### 會略過或特別處理的訊息

-   圖文選單或關鍵字按鈕產生的固定文字，可設定在
    `data/ignore_texts.txt`，避免建立不必要的草稿。
-   「好的」、「謝謝」等通常不需要回覆的訊息，會被略過或放到「無須回覆（已讀）」頁面。
-   退費、投訴、孩子受傷等敏感內容只會顯示紅色警示，不產生一般草稿，交由人工處理。

這樣做是為了避免系統在需要判斷情境、安撫家長或涉及學生安全的情況下，直接提供不合適的回覆。

## 五、FAQ 自動回覆機制

自動回覆功能預設為關閉。只有 Super User
可以在後台「設定」頁開啟，並調整相似度門檻。
預設門檻為`0.80` 門檻越高，系統就越保守。

一筆訊息必須同時符合以下條件，才會進入自動回覆流程：

1.  FAQ 相似度達到設定門檻。
2.  對應的 `faq.csv` 資料列中，`auto` 欄為 `Y`。
3.  答案沒有 `【】` 占位文字。
4.  `updated` 欄不是「待確認」。
5.  家長訊息不超過 100 字。
6.  訊息不包含敏感關鍵字。

自動回覆使用的是 FAQ
裡已經確認過的原文答案，不是讓語言模型臨時生成一段內容。由歷史對話整理出的
FAQ（例如透過 `make_faq.py`
產生）預設不會開啟自動回覆，必須先人工檢查並確認後，才可以自行將 `auto`
設成 `Y`。

### 傳送與額度

系統會先嘗試標記已讀，再使用 LINE 的 reply 方式回覆。如果超過
`REPLY_WINDOW_SECONDS`（預設以 55 秒為判斷值）或 reply
失敗，才會考慮改用 PUSH 補送。

只有 Super User 開啟推播功能時，系統才會使用 PUSH補送。
如果推播未開啟或訊息額度不足，就會回到人工處理流程。後台也提供自動回覆紀錄，可以查看回覆內容。
若答案不正確，可以標記有誤，再修正 FAQ 或提高相似度門檻。

手動在 OA Manager 回覆不會消耗這套系統的 PUSH 訊息額度。
透過後台 PUSH 傳送或設定 `PUSH_TO_OWNER=1` 發送通知，則會使用 LINE
官方帳號訊息額度。實際方案與額度以 LINE 官方公告及後台顯示為準。

## 六、已讀與登入管理

### 已讀處理

「謝謝」、「好的」等不需要回覆的訊息，可以在「無須回覆（已讀）」頁面逐則或一次全部標記已讀。系統使用
LINE 的 Mark as read API，因此需要符合 SDK 版本需求（`line-bot-sdk` 3.21
以上），而且 OA Manager 的「聊天」設定必須開啟。

標記某一則訊息時，LINE
可能會一併將它之前的訊息標記為已讀。自動回覆與後台 PUSH
傳送前也會先嘗試標記已讀。若標記失敗，系統會顯示提示，但不會因此卡住整個清單。

更新功能前就已存在的舊訊息可能沒有已讀 token，這類訊息無法透過 API
標記已讀，只能從後台清單移除。

### 登入與登出

後台使用帳號密碼登入，並以伺服器端 Session 維持登入狀態。

-   預設登入期限為 `SESSION_HOURS` 設定的 12 小時。
-   同一個 IP 連續輸入錯誤密碼 5 次，會鎖定 5 分鐘。
-   右上角的「登出」會讓伺服器端的登入狀態失效。
-   Super User 可以在「設定」頁執行「登出所有裝置」。
-   若要切換一般管理員與 Super User，先登出，再用另一組帳號密碼登入。

## 七、導師、時段與學生管理

後台的「導師／學生管理」可以新增導師、上課時段與學生，也能把學生綁定到家長的
LINE 帳號。家長必須先傳過訊息，帳號才會出現在可選清單中。

預設時段如下：

  時段          時間
  ------------- --------------
  週六第 1 節   09:00--10:30
  週六第 2 節   10:30--12:00
  週六第 3 節   13:40--15:10
  週六第 4 節   15:10--16:40

「總覽」會依照導師與時段顯示學生人數及待處理訊息。點選對應格子後，可以進一步篩選需要處理的項目。

## 八、常見問題排查

### FAQ 沒有載入

先檢查終端機是否出現「FAQ 載入完成」，再確認 `data/faq.csv`
的欄位名稱、CSV UTF-8 編碼及檔案路徑。

### LINE 訊息沒有進入後台

依序檢查：

1.  FastAPI 是否仍在執行。
2.  Cloudflare Tunnel 是否正常，以及目前的網址是否仍有效。
3.  LINE Developers Console 的 Webhook URL 是否為目前的 HTTPS 網址加上
    `/callback`。
4.  Verify 是否通過，Use webhook 是否開啟。
5.  Channel secret 與 Channel access token 是否填寫正確。

### 自動回覆沒有觸發

到後台的「自動回覆紀錄」頁使用「自動回覆測試」，輸入測試句子。畫面會顯示最相近的
FAQ、實際相似度，以及未觸發的原因。

也可以查看草稿卡片的備註，或觀察終端機中以 `[auto]` 開頭的紀錄。
若相似度不足，可檢查 FAQ 問法是否完整，若答案尚未確認，應先修正資料，而不是直接降低門檻。

### LINE 已讀標記失敗

確認 `line-bot-sdk` 版本至少為 3.21，並檢查 OA Manager
的「聊天」是否開啟。若是更新前就收到的舊訊息，也可能因缺少已讀 token
而無法標記。

## 九、資料與安全注意事項

-   `data/app.db`
    會保存家長訊息與草稿，包含個人資料，不可直接上傳到公開平台或分享給無關人員。
-   `.env` 含有 LINE Token、Secret 及管理員密碼，不應提交到版本控制。
-   正式使用前，應確認 FAQ 中的價格、地址、營業時間與課程規則。
-   自動回覆應先以少量、低風險的 FAQ 測試，並保留人工處理方式。
-   涉及退費、投訴、學生受傷或其他需要個別判斷的內容，應交由人工回覆。
-   若要長期使用，應設定固定 HTTPS
    網址，並規劃資料庫備份、權限管理及更新流程。

## 十、後續可以改善的方向

1.  導入整理過的歷史問答資料，例如 `qa_clean.jsonl`，補足 FAQ
    沒有涵蓋的問法。
2.  記錄操作人員修改草稿的內容，後續再用這些紀錄評估 FAQ
    命中率與草稿品質。
3.  定期檢查自動回覆紀錄，整理錯誤案例並更新 FAQ。
4.  正式部署前，補上資料備份、例外紀錄與部署更新流程。


