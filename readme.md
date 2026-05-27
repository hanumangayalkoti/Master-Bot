DealsKoti Master Bot
Admin-only Telegram bot jo deal messages sunke automatically sahi category channels pe post kar deta hai — Amazon Creators API ke saath full product enrichment ke saath.

Bot kya karta hai
Admin koi deal ka message ya link bhejta hai → bot:

Amazon link detect karta hai
Single Amazon product → Creators API se title, price, discount, rating, reviews aur image fetch karta hai
AI (GPT-4o-mini) se catchy Hinglish title banata hai
Duplicate check karta hai (24 ghante tak same title dobara post nahi hogi)
Category detect karta hai (AI ya keywords se)
Sahi Telegram channels pe post karta hai
Admin ko har baar reply karta hai — post hua ya nahi, kyun nahi hua
Environment Variables (Railway pe set karo)
Variable	Required	Description
BOT_TOKEN	✅	Telegram bot token (@BotFather se)
ADMIN_ID	✅	Admin ka Telegram user ID (number)
CREDENTIAL_ID	✅	Amazon Creators API Client ID
CREDENTIAL_SECRET	✅	Amazon Creators API Client Secret
CREDENTIAL_VERSION	❌	API version — default: 3.2 (India ke liye)
PARTNER_TAG	❌	Affiliate tag — default: dealskoti-21
MARKETPLACE	❌	Marketplace — default: www.amazon.in
OPENAI_API_KEY	❌	AI title generation ke liye (optional, fallback hai)
Amazon Creators API Credentials kahan se milenge?
Amazon Associates account chahiye
Amazon Creators portal pe jaao
API credentials section mein Client ID aur Client Secret milega
CREDENTIAL_VERSION India ke liye 3.2 hai
Admin Commands
Command	Kaam
/start	Bot ki info
/help	Sare commands
/status	Groups, channels, categories aur buttons ka status
/manage	Groups ON/OFF karo
/addgroup	Naya group banao (group = channel collection)
/addchannel	Group mein channel add karo
/editgroup	Channel ki categories badlo
/rename	Group ka naam badlo
/deletegroup	Group delete karo
/deletechannel	Channel hatao
/setbutton	Har post ke neeche 2 customisable buttons set karo
/testai	OpenAI API test karo
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
    │               → AI se: catchy Hinglish title
    │               → Rich caption banata hai
    │               → Category detect karta hai
    │               → Sahi channels pe post karta hai
    │               → Admin ko clickable channels ke saath reply karta hai
    │
    ├── Multiple Amazon links? → Normal post (affiliate link replace hoti hai)
    │
    └── Non-Amazon link? → Normal post (existing behavior)

/setbutton — Har Post ke Neeche Buttons
Har post ke neeche 2 fully customisable inline buttons add kar sakte ho:

Rename — button ka naam badlo (jaise "Join Channel", "More Deals")
Set Link — koi bhi URL ya Telegram link set karo
ON/OFF toggle — button show karo ya hide karo
Agar dono ON hain → dono dikhe
Agar ek ON → sirf ek dikhe
Agar dono OFF → koi button nahi

/exportconfig — Backup & Restore
/exportconfig command se config ka poora JSON milega. Isko save karke rakho.

Agar kabhi bot delete ho gaya ya naya deploy karna pada:

Naya bot banao
config.json file mein export ka JSON paste karo
Bot waise hi kaam karega jaise pehle tha
Config Structure (config.json)
{
  "groups": [
    {
      "name": "Main Group",
      "enabled": true,
      "channels": [
        {
          "channel": "@mychannel",
          "categories": ["electronics", "home"]
        }
      ]
    }
  ],
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
🔥 [AI-generated catchy title]
💰 Actual Price:      ₹X,XXX  (strikethrough)
🏷️ Deal Price:        ₹X,XXX  (bold)
💵 You Save:          ₹XXX
📉 Discount:          XX% OFF
⭐ Rating: X.X/5  |  👥 X,XXX reviews
🛒 Buy Now →  [affiliate link]

Duplicate Detection
Title-based (scraping nahi)
Same title 24 ghante mein dobara aaye → post nahi hoti
Admin ko clearly bataya jaata hai ki "X ghante pehle post ho chuki hai"
database.json file mein stored
Project Structure
master-bot-upgraded/
├── main.py          — Bot ka core logic, commands, handlers
├── amazon_api.py    — Amazon Creators API integration (async)
├── caption.py       — Amazon + non-Amazon caption builder
├── classifier.py    — AI + keyword category detection
├── keywords.py      — Category keyword lists
├── database.py      — Duplicate detection (24hr title cache)
├── scraper.py       — Non-Amazon URL scraper
├── utils.py         — URL helpers, platform detection
├── config.json      — Groups, channels, buttons config
├── database.json    — Duplicate title history (auto-created)
├── requirements.txt
└── runtime.txt

Changes from Original Bot
Feature	Pehle	Abhi
Amazon API	PA API v5 (band)	Creators API (kaam karta hai)
Product data	Title only	Title + Price + MRP + Discount + Rating + Reviews + Image
AI Title	Nahi tha	GPT-4o-mini se short Hinglish title
Duplicate check	Nahi tha	Title-based, 24hr window
Search page	Post ho jaata tha	Detect hota hai, post nahi hota
Buttons	/setfolder (folder only)	/setbutton (2 fully custom buttons)
Admin reply	Channels as copyable text	Channels as clickable Telegram links
Credentials	AMAZON_CLIENT_ID hardcoded	CREDENTIAL_ID env var, PARTNER_TAG env var
Config backup	Nahi tha	/exportconfig command
Railway Deployment
GitHub repo pe push karo
Railway pe new project → Connect GitHub repo
Environment variables set karo (upar table dekho)
Deploy
Start command: python main.py
