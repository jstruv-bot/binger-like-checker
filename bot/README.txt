================================================================================
                      BINGER BOT - SETUP GUIDE
================================================================================

A GroupMe bot that checks who didn't like messages from Sirs.
Same features as the desktop app, but runs in your group chat.


================================================================================
  STEP 1: CREATE THE BOT
================================================================================

1. Go to https://dev.groupme.com/bots
2. Click "Create Bot"
3. Select the group you want the bot in
4. Name it "Binger" (or whatever you want)
5. For Callback URL, enter your server URL + /callback
   Example: https://your-server.com/callback
   (You'll set this up in Step 3)
6. Click "Submit" -- you'll get a Bot ID
7. Copy the Bot ID


================================================================================
  STEP 2: GET YOUR CREDENTIALS
================================================================================

You need 3 things:

  GROUPME_TOKEN  - Your API token from https://dev.groupme.com
                   (click "Access Token" in the top right)

  BOT_ID         - The Bot ID from Step 1

  GROUP_ID       - Your group's ID. Find it by:
                   - Go to https://web.groupme.com
                   - Open your group
                   - The URL will be: web.groupme.com/chats/XXXXX
                   - XXXXX is your Group ID


================================================================================
  STEP 3: DEPLOY THE BOT
================================================================================

Option A: Run Locally (for testing)
------------------------------------
  1. Install Python 3.10+
  2. cd into the bot/ folder
  3. pip install -r requirements.txt
  4. Set environment variables:
       Windows:
         set GROUPME_TOKEN=your_token
         set BOT_ID=your_bot_id
         set GROUP_ID=your_group_id
       Mac/Linux:
         export GROUPME_TOKEN=your_token
         export BOT_ID=your_bot_id
         export GROUP_ID=your_group_id
  5. python bot.py
  6. Use ngrok to expose your local server:
       ngrok http 5000
  7. Copy the ngrok HTTPS URL and set it as your bot's Callback URL
     at dev.groupme.com/bots (add /callback at the end)


Option B: Deploy to Railway (free, recommended)
-------------------------------------------------
  1. Go to https://railway.app and sign up
  2. Create a new project from GitHub repo
  3. Set the root directory to /bot
  4. Add environment variables in Railway dashboard:
       GROUPME_TOKEN, BOT_ID, GROUP_ID
  5. Railway auto-deploys and gives you a URL
  6. Set that URL + /callback as your bot's Callback URL


Option C: Deploy to Render (free)
----------------------------------
  1. Go to https://render.com and sign up
  2. Create a new Web Service from your GitHub repo
  3. Set root directory to /bot
  4. Build command: pip install -r requirements.txt
  5. Start command: gunicorn bot:app --bind 0.0.0.0:$PORT
  6. Add environment variables: GROUPME_TOKEN, BOT_ID, GROUP_ID
  7. Use the Render URL + /callback as your bot's Callback URL


Option D: Deploy to a VPS
--------------------------
  1. SSH into your server
  2. Clone the repo: git clone https://github.com/jstruv-bot/binger-like-checker
  3. cd binger-like-checker/bot
  4. pip install -r requirements.txt
  5. Set environment variables in your shell profile or use a .env file
  6. Run with: gunicorn bot:app --bind 0.0.0.0:5000 --daemon
  7. Set up a reverse proxy (nginx) for HTTPS


================================================================================
  STEP 4: USE THE BOT
================================================================================

In your GroupMe group, type any of these commands:

  !help               Show all commands
  !ping               Check if bot is alive

  !addsir Jordan      Add "Jordan" as a Sir
  !addsir Mike        Add "Mike" as a Sir
  !sirs               List all Sirs
  !removesir Jordan   Remove a Sir

  !check              Check the last Sir message for non-likers
  !check 5            Check the last 5 Sir messages
  !shame              Re-send the shame list from last check

  !leaderboard        Who misses Sir messages most (last 200 msgs)
  !leaderboard 500    Scan more messages for accuracy

  !report Jordan      Full report card for a member

  !exclude Bot        Exclude a member from checks
  !unexclude Bot      Remove exclusion


================================================================================
  HOW SIR CHECKING WORKS
================================================================================

1. You designate certain people as "Sirs" with !addsir
2. When a Sir posts a message, everyone is expected to like it
3. !check finds the most recent Sir message(s) and reports who didn't like
4. !leaderboard scans many messages and ranks who misses the most
5. !shame sends the shame list to call out non-likers

The bot only checks messages from Sirs. Non-Sir messages are ignored.
Excluded members are also ignored (useful for bots or inactive people).

All settings (Sirs, exclusions) are saved to a local database and persist
across bot restarts.


================================================================================
  TROUBLESHOOTING
================================================================================

Bot doesn't respond:
  - Check that the callback URL is correct (must end with /callback)
  - Make sure the server is running and publicly accessible
  - Check logs for errors
  - Try !ping -- if no response, the callback isn't reaching your server

"No Sirs set" error:
  - You need to add at least one Sir with !addsir Name

Can't find member:
  - Use the member's GroupMe nickname (not their real name)
  - Partial matches work: "Jor" will match "Jordan"

Bot posts twice:
  - Make sure you only have one bot registered for this group


================================================================================

Binger Bot - Part of the Binger Like Checker project
https://github.com/jstruv-bot/binger-like-checker
