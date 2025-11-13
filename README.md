
# Ambot Telegram Converter Bot (Windows RDP Installation Guide)

This bot converts YouTube titles into MP3 or MP4 with a coin system, daily rewards, admin tools, queueing, refunds, and async database using SQLite.

This README is specifically for **Windows RDP hosting**.

---

## 🚀 Requirements

- Windows RDP machine
- Python 3.10+ installed
- FFmpeg installed
- Internet connection
- Telegram Bot Token (from @BotFather)

---

## 📦 Installation Steps (Windows RDP)

### 1. Install Python
Download from:
https://www.python.org/downloads/windows/

Check **Add Python to PATH** during installation.

---

### 2. Install FFmpeg
1. Download Windows FFmpeg build:
   https://www.gyan.dev/ffmpeg/builds/

2. Extract to:
   ```
   C:\ffmpeg
   ```

3. Add to PATH:
   - Search “Environment Variables”
   - Edit PATH
   - Add:
     ```
     C:\ffmpeg\bin
     ```

4. Test:
   ```
   ffmpeg -version
   ```

---

### 3. Prepare the Project Directory

Create folder:
```
C:\ambot
```

Place these files inside:
- ambot_aiosqlite.py
- requirements.txt
- start.bat
- README.md (this file)

---

### 4. Create and Activate the Virtual Environment

Open **PowerShell**:

```powershell
cd C:\ambot
python -m venv venv
.env\Scriptsctivate
pip install --upgrade pip
pip install -r requirements.txt
```

---

## 🔐 5. Set Environment Variables (Permanent)

Search:
```
Edit the system environment variables
```

Click **Environment Variables…**

Under **User variables**, click **New**:

### Add:
**Variable name:**
```
BOT_TOKEN
```
**Variable value:**
```
123456:ABC-REPLACE-WITH-YOUR-TOKEN
```

---

### Add admin ID:
**Variable name:**
```
ADMIN_TELEGRAM_ID
```

**Value:**
```
123456789
```

---

### Add database path:
**Variable name:**
```
DB_PATH
```

**Value:**
```
music.db
```

Press **OK** → **OK** again.

Restart PowerShell.

---

## ▶️ 6. Run the Bot

```powershell
cd C:\ambot
.env\Scriptsctivate
python ambot_aiosqlite.py
```

You should see:
```
Bot running...
```

---

## 🔄 Optional: Auto-Launch on Login

Place a shortcut to start.bat inside:

```
shell:startup
```

This makes the bot start automatically when you log in.

---

## 💾 Database File

The SQLite database is stored as:

```
music.db
```

Back this file up occasionally.

---

## 👑 Admin Commands

```
/admin_status
/addcoins @username 100
/history
/export_logs
```

---

## 🛠 Features

- User registration (+500 coins)
- Daily reward (+20 coins)
- MP3/MP4 conversion with selectable quality
- Coin-based system
- Refund automatically if sending fails
- Per-user queueing (prevents spam)
- Rate limits (10 conversions/day)
- Cooldown: 60 seconds
- Admin coin editing
- Export logs to CSV
- SQLite async storage

---

## 🎉 Support

If you'd like:
- an auto-installer
- a .env version
- a web dashboard
- a Linux/VPS edition

Just ask!
