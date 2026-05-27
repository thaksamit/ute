# UTE — Unified Theory Engine

An advanced AI reasoning engine powered by Claude. Every query is examined through four cognitive layers:

- **Bosonic** — Signal vs Noise
- **Fermionic** — Reality vs Delusion  
- **Anyonic** — Connect vs Disconnect
- **Cosmic** — Meaning vs Awareness

Supports text queries, URL reading, file attachments (images, PDFs, documents), and generates downloadable output files.

---

## Run locally

```bash
cd ute
set ANTHROPIC_API_KEY=sk-ant-...     # Windows
export ANTHROPIC_API_KEY=sk-ant-...  # Mac/Linux
python3 server.py
# → http://localhost:7532
```

No dependencies beyond Python 3.10+.

---

## Deploy to Railway (recommended — easiest)

1. Create a free account at **railway.app**
2. Push this folder to a GitHub repo:
   ```bash
   git init
   git add .
   git commit -m "UTE initial"
   git branch -M main
   git remote add origin https://github.com/YOUR_USERNAME/ute.git
   git push -u origin main
   ```
3. In Railway: **New Project → Deploy from GitHub repo** → select your repo
4. Railway auto-detects the Dockerfile and builds
5. Go to **Variables** tab → add `ANTHROPIC_API_KEY` = your key
6. Go to **Settings → Networking** → Generate Domain
7. Your app is live at `https://ute-xxxx.up.railway.app`

Cost: Free tier gives 500 hours/month. Paid is $5/month for always-on.

---

## Deploy to Render (free tier available)

1. Create account at **render.com**
2. Push to GitHub (same steps as above)
3. **New → Web Service → Connect GitHub repo**
4. Settings:
   - Runtime: **Python 3**
   - Build Command: `echo done`
   - Start Command: `python3 server.py`
5. **Environment Variables** → add `ANTHROPIC_API_KEY`
6. Click **Create Web Service**
7. Live at `https://ute.onrender.com`

Note: Render free tier spins down after 15 min of inactivity (first request is slow). Paid ($7/month) keeps it always on.

---

## Deploy to a VPS (DigitalOcean, AWS, Azure, Hetzner)

SSH into your server, then:

```bash
# Install Python if needed
sudo apt update && sudo apt install python3 -y

# Copy files
scp -r ./ute user@your-server-ip:/home/user/ute

# On the server
cd /home/user/ute
export ANTHROPIC_API_KEY=sk-ant-...

# Run with nohup so it keeps going after you close SSH
nohup python3 server.py > ute.log 2>&1 &

# Or install as a systemd service (runs on boot):
sudo nano /etc/systemd/system/ute.service
```

Systemd service file:
```ini
[Unit]
Description=UTE Unified Theory Engine
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/ute
Environment=ANTHROPIC_API_KEY=sk-ant-YOUR_KEY_HERE
Environment=PORT=7532
ExecStart=/usr/bin/python3 server.py
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable ute
sudo systemctl start ute
# Check it's running:
sudo systemctl status ute
```

To expose port 80/443, put nginx in front:
```bash
sudo apt install nginx -y
sudo nano /etc/nginx/sites-available/ute
```

```nginx
server {
    listen 80;
    server_name yourdomain.com;

    location / {
        proxy_pass http://localhost:7532;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 60s;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/ute /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
# Add SSL with Let's Encrypt:
sudo apt install certbot python3-certbot-nginx -y
sudo certbot --nginx -d yourdomain.com
```

---

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | Yes | Your Anthropic API key (sk-ant-...) |
| `PORT` | No | Port to listen on (default: 7532) |

---

## Architecture

Three files:

- **`engine.py`** — The entire UTE engine. Claude API calls, four-layer parsing, microsite storage, attachment handling, URL fetching.
- **`server.py`** — Minimal HTTP server. Serves the UI and API endpoints.
- **`static/index.html`** — The complete UI. Dark theme, eight-dimension cards, Smart Refine with auto-drafted queries, attachment upload, file download.

Microsites are stored in `~/ute_knowledge/microsites.db` (SQLite). On Railway/Render, mount a persistent volume at `/data` to keep them across deploys.
