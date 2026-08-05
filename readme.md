DealsKoti Master Bot
Admin-only Telegram bot jo deal messages sunke automatically ek channel pe post kar deta hai — Amazon Creators API ke saath full product enrichment ke saath.

Bot kya karta hai
Admin koi deal ka message ya link bhejta hai → bot:

Amazon link detect karta hai
Single Amazon product → Creators API se title, price, discount, rating, reviews aur image fetch karta hai
Clean product title banata hai (bina AI ke)
Duplicate check karta hai (24 ghante tak same title dobara post nahi hogi)
Configured channel pe post karta hai
Admin ko reply karta hai — post hua ya nahi, kyun nahi hua

Environment Variables (Railway pe set karo)
Variable	Required	Description
BOT_TOKEN	✅	Telegram bot token (@BotFather se)
ADMIN_ID	✅	Admin ka Telegram user ID (number)
DATABASE_URL	✅	PostgreSQL connection string
CREDENTIAL_ID	✅	Amazon Creators API Client ID
CREDENTIAL_SECRET	✅	Amazon Creators API Client Secret
CREDENTIAL_VERSION	❌	API version — default: 3.2 (India ke liye)
PARTNER_TAG	❌	Affiliate tag — default: dealskoti-21
MARKETPLACE	❌	Marketplace — default: www.amazon.in

Amazon Creators API Credentials kahan se milenge?
Amazon Associates account chahiye
Amazon Creators portal pe jaao
API credentials section mein Client ID aur Client Secret milega
CREDENTIAL_VERSION India ke liye 3.2 hai

Admin Commands
Command	Kaam
/start	Bot ki info
/help	Sare commands
/status	Channel aur buttons ka status
/setchannel	Post karne wala channel set karo
/setbutton	Har post ke neeche 2 customisable buttons set karo
/testamz	Amazon Creators API test karo
/exportconfig	Config ka JSON export karo (backup ke liye)

Message Flow
Admin message bhejta hai
    │
    ├── Amazon search page link? → ❌ Post nahi kiya (reply bhejta hai)
    │
    ├── Single Amazon product link?
    │       ├── Duplicate (24hr mein)? → ❌ Skip, reply bhejta hai
    │       └── Fresh product:
    │               → Creators API se: title, price, MRP, discount, rating, reviews, image
    │               → Clean title banata hai
    │               → Rich caption banata hai
    │               → Channel pe post karta hai
    │               → Admin ko reply karta hai
    │
    ├── Multiple Amazon links? → Normal post (affiliate link replace hoti hai)
    │
    └── Non-Amazon link/text? → Normal post

/setchannel — Channel Set Karo
/setchannel command se channel ID set karo jahan post karna hai.
Channel format: @mychannel ya -100123456789

Bot ko us channel mein admin banana zaroori hai posting ke liye.

/setbutton — Har Post ke Neeche Buttons
Har post ke neeche 2 fully customisable inline buttons add kar sakte ho:

Rename — button ka naam badlo (jaise "Join Channel", "More Deals")
Set Link — koi bhi URL ya Telegram link set karo
ON/OFF toggle — button show karo ya hide karo

/exportconfig — Backup & Restore
/exportconfig command se config ka poora JSON milega.

Config Structure (config.json)
{
  "channel": "@mychannel",
  "buttons": {
    "btn1": {
      "label": "Join Channel",
      "url": "https://t.me/mychannel",
      "enabled": true
    },
    "btn2": {
      "label": "More Deals",
      "url": "https://t.me/moredeals",
      "enabled": false
    }
  }
}

Amazon Caption Format
🙏Jai Shree Ram Dosto🙏

🔥 [Product Title]
💰 MRP:    ₹X,XXX (strikethrough)
🏷️ Buy At: ₹X,XXX (bold)
💵 You Save: ₹XXX
📉 Discount: XX% OFF
⭐ Rating: X.X/5
👥 X,XXX reviews

🔗 [affiliate link]

Duplicate Detection
Title-based
Same title 24 ghante mein dobara aaye → post nahi hoti
Admin ko clearly bataya jaata hai ki "X ghante pehle post ho chuki hai"
PostgreSQL database mein stored

Project Structure
master-bot/
├── main.py          — Bot ka core logic, commands, handlers
├── amazon_api.py    — Amazon Creators API integration (async)
├── caption.py       — Amazon + non-Amazon caption builder
├── database.py      — Duplicate detection (24hr title cache)
├── storage.py       — PostgreSQL config storage
├── requirements.txt
└── runtime.txt

Railway Deployment
GitHub repo pe push karo
Railway pe new project → Connect GitHub repo
Environment variables set karo (upar table dekho)
Deploy
Start command: python main.py
