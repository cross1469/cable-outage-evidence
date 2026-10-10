# cable-outage-evidence

繁體中文 | English below

> **開發中。** 目前只有規格文件，程式還沒完成。

## 這是什麼

你是不是遇過這種情況：網路突然斷了幾秒到幾十秒，數據機的燈號卻一切正常；打給客服，
對方說「線路正常，請檢查您的路由器」。

這類瞬斷常常太短，ISP 的監控看不到：數據機一直保持上線，ISP 每幾分鐘做一次的檢查
都顯示正常。用戶手上又只有幾張測速截圖，很難要求 ISP 處理。

這個工具在你的電腦（Windows、macOS、Linux）上持續記錄：

- 每 0.5 秒探測家用路由器、數據機、ISP 第一跳和公共目標，找出斷在哪一層；
- 讀取數據機的 DOCSIS 日誌和上下行頻道狀態，斷線時改成每秒讀一次；
- 產生給 ISP 的報告：每次斷線的時間、長度、各層的狀態，以及數據機日誌的原文。

報告只寫觀察到的事實，不替 ISP 下結論。這樣的報告比較難被反駁，也讓 ISP 的工程師
能直接判斷該查哪一段。

這類「數據機在線、下行鎖定，卻整段斷網」的現象，符合上行雜訊干擾（例如 burst
ingress）的特徵。工具量不到 ingress 本身，但能把證據整理好，請 ISP 用他們的儀器
檢查上行。

第一版支援 Hitron CODA 系列數據機。其他型號歡迎提供 API 資料或 PR。

詳細規則見 [docs/spec.md](docs/spec.md)。

## 目前可以試用的部分

```
py -3 -m cable_outage_evidence init            # 建立設定檔（會詢問數據機帳密）
py -3 -m cable_outage_evidence modem-check     # 確認讀得到數據機
py -3 -m cable_outage_evidence discover-path   # 找出預設閘道與 ISP 第一跳
py -3 -m cable_outage_evidence once            # 取樣一次並印出
py -3 -m cable_outage_evidence run             # 持續監控，Ctrl+C 停止
py -3 -m unittest discover -s tests -t .       # 測試
```

macOS / Linux 用 `python3` 取代 `py -3`。

讀取數據機前先執行 `init` 設定數據機位址與帳密（密碼只存在本機的設定檔），
再用 `modem-check` 確認連得上。事件分類、時間軸和給 ISP 的報告還在開發中。

## English

> **Work in progress.** Only the specification exists so far.

Short cable (DOCSIS) dropouts of a few seconds often never show up in ISP
monitoring: the modem stays registered and the ISP's periodic checks see nothing.
This tool runs on a PC (Windows, macOS, Linux). It probes every layer of the path
every 0.5 s, records the cable modem's DOCSIS event log and channel state, and
builds an evidence report that states what was observed without guessing a cause.

The first version supports Hitron CODA modems. See [docs/spec.md](docs/spec.md)
(Traditional Chinese).

## License

MIT
