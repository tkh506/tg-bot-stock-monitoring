# Stock Monitor Bot — Deployment Guide

## Overview

| File | Role |
|------|------|
| `telegram_bot_multistock.py` | Interactive bot — users configure stocks and alerts |
| `robo_monitor_multistock.py` | Background monitor — checks prices, sends alerts every 15 min |

Two-step process: **set up once** → **update with `git pull`** going forward.

---

## Part 0: Local Testing with PyCharm

Test the bot on your local machine before deploying to the VM.

### 0.1 Open Project in PyCharm

File → Open → select the project folder.

PyCharm will detect it as a Python project. If prompted to create a virtual environment, accept and skip to step 0.2.

**If PyCharm did not create a venv automatically:**

Option A — via PyCharm UI:
1. Open **PyCharm → Settings** (Mac: `Cmd+,` / Windows: `Ctrl+Alt+S`)
2. Go to **Project: \<name\> → Python Interpreter**
3. Click the interpreter dropdown → **Add Interpreter → Add Local Interpreter**
4. Select **Virtualenv Environment** → **New**
5. Leave the default location and base interpreter, click **OK**

Option B — via terminal (PyCharm's built-in terminal at the bottom):
```bash
python3 -m venv venv
source venv/bin/activate        # Mac / Linux
# .\venv\Scripts\activate       # Windows (use this line instead)
```
Then tell PyCharm to use it: **Settings → Python Interpreter → Add Interpreter → Add Local Interpreter → Existing → select `venv/bin/python`**

### 0.2 Install Dependencies

Open PyCharm's built-in terminal (bottom of screen) and run:

```bash
pip install python-telegram-bot yfinance pytz requests
```

### 0.3 Create a Local Config File

In the same terminal:

```bash
cp config.json.example config.json
```

Open `config.json` and replace `YOUR_BOT_TOKEN_HERE` with your actual Telegram bot token. Save.

> `config.json` is in `.gitignore` — it will never be accidentally committed.

### 0.4 Run the Bots

The two scripts run as separate processes. Use PyCharm's **Run** button, or open two terminal tabs:

**Terminal 1 — Config bot (handles Telegram commands):**
```bash
python telegram_bot_multistock.py
```

**Terminal 2 — Monitor bot (checks prices every 15 min):**
```bash
python robo_monitor_multistock.py
```

### 0.5 Test via Telegram

Open Telegram and send `/start` to your bot. You should see the main menu. The local data folders (`user_configs/`, `alert_states/`, `price_history/`) will be auto-created in your project folder on first run.

To stop either bot: **Ctrl+C** in its terminal.

---

## Part 1: Push Local Code to GitHub

Do this on your local machine.

### 1.1 Initialize Git

Open Terminal, navigate to your project folder, then run:

```bash
git init
git remote add origin https://github.com/<your-username>/tg-bot-stock-monitoring.git
git add .
git commit -m "Initial commit"
git push -u origin main
```

> GitHub no longer accepts your account password here. When prompted, enter your **Personal Access Token** instead.
> Create one at: **github.com → Settings → Developer Settings → Personal Access Tokens → Tokens (classic) → Generate new token** (tick the `repo` scope).

### 1.2 Verify

Open your repo on github.com. You should see the `.py` files and `.gitignore`, but **not** `config.json`.

---

## Part 2: Create a Google Cloud VM (Free Tier)

### 2.1 Create the VM

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create or select a project
3. Navigate to **Compute Engine → VM Instances**
4. Click **Create Instance** with these exact settings:

| Setting | Value |
|---------|-------|
| Name | anything, e.g. `my-bots-vm` |
| Region | `us-east1`, `us-central1`, or `us-west1` (free tier regions only) |
| Zone | any within the region |
| Machine type | `e2-micro` (free tier) |
| Boot disk OS | Ubuntu 22.04 LTS |
| Boot disk size | 30 GB, Standard persistent disk |
| Firewall | leave default |

5. Click **Create**

### 2.2 Connect to the VM

In the VM list, click the **SSH** button next to your VM. A browser terminal opens — this is your VM's command line.

> All commands in Part 3 onwards are run inside this terminal.

---

## Part 3: Set Up the VM

### 3.1 Install System Dependencies

```bash
sudo apt update && sudo apt install -y python3-pip git
```

### 3.2 Install Python Packages

```bash
pip3 install python-telegram-bot yfinance pytz requests
```

### 3.3 Clone the Repo

```bash
cd ~
git clone https://github.com/<your-username>/tg-bot-stock-monitoring.git
cd tg-bot-stock-monitoring
```

> If your repo is private, Git will ask for your GitHub username and Personal Access Token.

### 3.4 Create the Config File

```bash
cp config.json.example config.json
nano config.json
```

Replace `YOUR_BOT_TOKEN_HERE` with your actual Telegram bot token.

Save and exit: **Ctrl+X → Y → Enter**

---

## Part 4: Set Up Systemd Services

Systemd keeps the bots running in the background and auto-restarts them if they crash or if the VM reboots.

### 4.1 Find Your VM Username

```bash
whoami
```

Note the output — you'll need it in the next two steps.

### 4.2 Config Bot Service

```bash
sudo nano /etc/systemd/system/robo-config-bot.service
```

Paste the following, replacing `<your-username>` with the output from Step 4.1:

```ini
[Unit]
Description=Stock Monitor Config Bot
After=network.target

[Service]
User=<your-username>
WorkingDirectory=/home/<your-username>/tg-bot-stock-monitoring
ExecStart=/usr/bin/python3 /home/<your-username>/tg-bot-stock-monitoring/telegram_bot_multistock.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Save: **Ctrl+X → Y → Enter**

### 4.3 Monitor Bot Service

```bash
sudo nano /etc/systemd/system/robo-monitor-bot.service
```

Paste (same username substitution):

```ini
[Unit]
Description=Stock Monitor Background Bot
After=network.target

[Service]
User=<your-username>
WorkingDirectory=/home/<your-username>/tg-bot-stock-monitoring
ExecStart=/usr/bin/python3 /home/<your-username>/tg-bot-stock-monitoring/robo_monitor_multistock.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Save: **Ctrl+X → Y → Enter**

### 4.4 Enable and Start

```bash
sudo systemctl daemon-reload
sudo systemctl enable robo-config-bot.service robo-monitor-bot.service
sudo systemctl start robo-config-bot.service robo-monitor-bot.service
```

### 4.5 Verify Both Are Running

```bash
sudo systemctl status robo-config-bot.service
sudo systemctl status robo-monitor-bot.service
```

Both should show `active (running)`. Open Telegram and send `/start` to your bot to confirm.

---

## Part 5: Updating the Bot

### On your local machine

```bash
git add .
git commit -m "describe your change"
git push
```

### On the VM

```bash
cd ~/tg-bot-stock-monitoring
git pull
sudo systemctl restart robo-config-bot.service
sudo systemctl restart robo-monitor-bot.service
```

> `config.json` is excluded from git — it stays on the VM untouched across every update.

---

## Part 6: Useful Commands

```bash
# Follow live logs (Ctrl+C to exit)
sudo journalctl -u robo-monitor-bot.service -f
sudo journalctl -u robo-config-bot.service -f

# View last 100 log lines
sudo journalctl -u robo-monitor-bot.service -n 100

# Restart services
sudo systemctl restart robo-config-bot.service robo-monitor-bot.service

# Stop services
sudo systemctl stop robo-config-bot.service robo-monitor-bot.service
```

---

## File Reference

| File | In Git | On VM |
|------|--------|-------|
| `telegram_bot_multistock.py` | ✅ | ✅ via `git clone` / `git pull` |
| `robo_monitor_multistock.py` | ✅ | ✅ via `git clone` / `git pull` |
| `config.json.example` | ✅ | ✅ via `git clone` / `git pull` |
| `.gitignore` | ✅ | ✅ via `git clone` / `git pull` |
| `config.json` | ❌ never commit | ✅ created manually once |
| `user_configs/` | ❌ | ✅ auto-created at runtime |
| `alert_states/` | ❌ | ✅ auto-created at runtime |
| `price_history/` | ❌ | ✅ auto-created at runtime |
