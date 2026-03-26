#Pager2gotify

After Pagermon became deprecated and with PM2 clunky at best i wanted to integrate systemd,  bypass SQLite and forward messages straight to my phone from multimon-ng. This is a Minimal, reliable bridge from **multimon-ng / rtl_fm** pager decoding to **Gotify push notifications**.You can install the open source gotify app via Obtainium.

Designed for **clean, real-world pager monitoring** with mild to aggressive customisable filtering to remove junk traffic.

---

✨ Features

* Customisable Filters setting **POCSAG only** (ignores FLEX completely or whatever your choosing)
* Supports **baud filtering (e.g. 512 only)**
* Capcode filtering (e.g. **LPC Birdwatch = ends in `450`**)
* Removes:

  * test pages
  * control-character garbage
  * numeric-only junk
* Deduplication (prevents spam bursts)
* Clean notification formatting
* Runs as a **systemd service**

---

📡 Example Output

```
Mode: POCSAG
Baud: 512
Capcode: 993671

Wiedehopf spotted in shrubs on lake canterton.
```

---

⚙️ Requirements

* Linux (Raspberry Pi in this example)
* `rtl_fm` or equivalent SDR input
* `multimon-ng`
* Python 3.8+

---

🚀 Installation

1. Clone repo

```
git clone https://github.com/bertonumber1/pager2gotify.git
cd pager2gotify
```

---

### 2. Configure script

Edit:

```
pager2gotify.py
```

Set your Gotify server:

```python
GOTIFY_URL = "http://YOUR_SERVER_IP:8088"
GOTIFY_TOKEN = "YOUR_GOTIFY_APP_TOKEN"
```

---

3. Make executable

```
chmod +x pager2gotify.py
```

---

4. Create reader script

Example:

```bash
rtl_fm -f 153.050M -M fm -s 22050 -g 40 - | \
multimon-ng -a POCSAG512 -f alpha - | \
/path/to/pager2gotify.py
```

---

5. Create systemd service

```
sudo nano /etc/systemd/system/pager2gotify.service
```

```ini
[Unit]
Description=Pager2Gotify
After=network.target

[Service]
User=YOUR_USERNAME
ExecStart=/bin/bash /path/to/reader.sh
Restart=always

[Install]
WantedBy=multi-user.target
```

---

6. Enable + start

```
sudo systemctl daemon-reload
sudo systemctl enable pager2gotify
sudo systemctl start pager2gotify
```

---

7. Check logs

```
journalctl -u pager2gotify -n 50 --no-pager
```

---

🔧 Default Filtering Logic

Allowed:

* POCSAG
* Baud: 512
* Capcode ends with `450`

Blocked:

* FLEX
* POCSAG1200 / 2400
* test pages
* control-character garbage
* numeric-only messages

---

🧠 Customisation

You can modify:

```python
ALLOW_BAUD = "512"
ALLOW_SUFFIX = "450"
```

Examples:

Allow multiple suffixes:

```python
ALLOW_SUFFIXES = {"450", "451", "452"}
```

---

📱 Why Gotify?

* Self-hosted
* No Google dependency
* Instant push
* Simple HTTP API

---

⚠️ Disclaimer

Use only on frequencies you are licensed or permitted to monitor. This is a simple python based project i wrote myself. I thought i would upload it with the help of chatgpt as i have never used github before. Full kudos is with multimon-ng and the gotify devs. Thanks 

---

📜 License

MIT
Pager
