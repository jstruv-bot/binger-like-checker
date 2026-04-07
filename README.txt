================================================================================
                        BINGER LIKE CHECKER
                     GroupMe Like Analysis Toolkit
================================================================================

A Windows desktop application that connects to GroupMe and tells you exactly
who didn't like a message. Also includes leaderboards, group analytics, like
history tracking, member exclusions, and notification alerts.


================================================================================
  TABLE OF CONTENTS
================================================================================

  1. Getting Started
  2. Getting Your API Token
  3. Connecting to GroupMe
  4. Tab 1 - Like Checker
  5. Tab 2 - Leaderboard
  6. Tab 3 - History
  7. Tab 4 - Analytics
  8. Tab 5 - Notifications
  9. Member Exclusions
  10. Data Storage & Privacy
  11. Building from Source
  12. Troubleshooting
  13. FAQ


================================================================================
  1. GETTING STARTED
================================================================================

Option A: Run the .exe (recommended)
  - Double-click "BingerLikeChecker.exe" in the dist/ folder.
  - No Python installation required. It's fully self-contained.

Option B: Run the Python script directly
  - Requires Python 3.10+ installed.
  - Open a terminal in the project folder and run:
      pip install -r requirements.txt
      python like_checker.py


================================================================================
  2. GETTING YOUR API TOKEN
================================================================================

You need a GroupMe API token to use this app. It's free and takes 30 seconds:

  1. Go to https://dev.groupme.com in your browser.
  2. Log in with your GroupMe account (Microsoft account).
  3. Once logged in, click "Access Token" in the top-right corner of the page.
  4. Copy the long string of letters and numbers. That's your token.

IMPORTANT:
  - Your token gives full access to your GroupMe account. Treat it like a
    password. Do not share it with anyone.
  - The "Remember" checkbox saves your token locally on your computer in a
    config file so you don't have to paste it every time. If you're on a
    shared computer, uncheck this.


================================================================================
  3. CONNECTING TO GROUPME
================================================================================

  1. Paste your API token into the "API Token" field at the top.
  2. Check "Remember" if you want the app to save your token for next time.
  3. Click "Connect".
  4. If successful, you'll see "Connected: [Your Name]" in the top-right and
     your groups will populate in the dropdown.
  5. Select a group from the "SELECT GROUP" dropdown. The member count is shown
     next to it.

The authentication and group selection are always visible at the top, no matter
which tab you're on.


================================================================================
  4. TAB 1 - LIKE CHECKER
================================================================================

This is the core feature. It checks a specific message and tells you who liked
it and who didn't.

STEP BY STEP:

  1. Select a group (at the top).
  2. Set how many messages to load (20-500, default 100).
  3. Click "Load Messages". Messages appear in the list with this format:
       [MM/DD HH:MM] Name           +##L  Message text...
     The "+" means the message has at least one like. "##L" is the like count.
  4. Use the "Search" box to filter messages by sender name or text content.
  5. Click on a message to select it.
  6. Click "Check Who Didn't Like It" (the big green button).

RESULTS BREAKDOWN:

  MESSAGE     - Shows the sender, date, and full text of the message.

  STATS       - Like rate as a percentage, fraction, and visual progress bar.
                Example: 73%  (11 of 15 members)
                         [======================--------]

  DID NOT LIKE - Red list of every member who has NOT liked the message.
                 Sorted alphabetically.

  LIKED        - Green list of every member who HAS liked the message.

  EXCLUDED     - Gray list of members you've excluded (see Section 9).

ACTION BUTTONS (right side):

  Copy         - Copies the full results text to your clipboard.
  Export       - Saves results to a .txt file (you pick the location).
  Send Shame   - Sends a message to the GroupMe group calling out everyone
    List         who didn't like it. A confirmation dialog appears first.
                 The message format is:
                   "BINGER LIKE CHECKER REPORT
                    The following X member(s) did NOT like: "message"
                    1. Name
                    2. Name
                    ...
                    Like the message. You've been warned."


================================================================================
  5. TAB 2 - LEADERBOARD
================================================================================

Scans a range of messages and ranks all group members by their like activity.

HOW TO USE:

  1. Set how many messages to scan (50-2000, default 200). Higher numbers give
     more accurate results but take longer to fetch.
  2. Click "Build Leaderboard".
  3. Wait while messages are fetched (progress shown in the status area).

LEADERBOARD SECTIONS:

  MOST LIKES GIVEN       - Members who like other people's messages the most.
  (generous likers)        Shows total likes given and percentage of messages
                           liked. Top 3 get gold/silver/bronze highlighting.

  MOST LIKES RECEIVED    - Members whose messages get liked the most.
  (popular posters)        Shows total likes received and average likes per
                           message they sent.

  LEAST LIKES GIVEN      - Members who rarely like anyone's messages.
  (stingiest members)      The "shame" ranking. Top 3 are highlighted in red.
                           Excluded members are filtered out of this list.

  MOST MESSAGES SENT     - Most active members by message volume.


================================================================================
  6. TAB 3 - HISTORY
================================================================================

Every time you run a like check (Tab 1), the result is automatically saved to a
local database. This tab lets you review past checks and spot patterns.

BUTTONS:

  Refresh History   - Loads the last 50 checks for the selected group.
                      Each entry shows:
                        - Timestamp of when you ran the check
                        - The message text
                        - Like rate (X/Y liked, percentage)
                        - List of non-likers (first 8 shown, with "+N more")

  Repeat Offenders  - Ranks members by how many times they've appeared as a
                      non-liker across ALL your past checks for this group.
                      This is the cumulative "who never likes anything" list.
                      Top 3 get gold/silver/bronze, next 2 get red.

History is stored locally and persists across app restarts. See Section 10 for
the file location.


================================================================================
  7. TAB 4 - ANALYTICS
================================================================================

Deep-dive group statistics. Scans a range of messages and computes various
metrics about group activity.

HOW TO USE:

  1. Set how many messages to analyze (50-2000, default 300).
  2. Click "Run Analytics".

SECTIONS:

  OVERVIEW
    - Messages Analyzed: total count scanned
    - Time Span: date range and number of days covered
    - Active Members: how many unique people sent messages
    - Total Likes: sum of all likes across all messages
    - Avg Likes/Message: mean likes per message
    - Zero-Like Messages: count and percentage of messages nobody liked
                          (highlighted red if over 30%)

  MOST LIKED MESSAGE
    - The single message with the highest like count in the scanned range.
    - Shows the text, sender, date, and like count.

  ACTIVITY BY HOUR
    - ASCII bar chart (00:00 through 23:00) showing when messages are sent.
    - The peak hour is highlighted in orange. Useful for knowing when the
      group is most active.

  ACTIVITY BY DAY
    - ASCII bar chart (Monday through Sunday) showing message volume per day.
    - Peak day highlighted in orange.

  MOST WORDS WRITTEN
    - Ranks members by total word count across all their messages.
    - Shows message count and average words per message.
    - Top 3 get gold/silver/bronze.


================================================================================
  8. TAB 5 - NOTIFICATIONS
================================================================================

Get a Windows desktop toast notification when a like check reveals a low like
rate.

SETTINGS:

  Alert threshold   - Set the percentage (5-100%, default 50%). If the like rate
                      on a checked message falls below this number, a
                      notification fires.

  Enable checkbox   - Turn notifications on or off. When enabled, notifications
                      trigger automatically after each like check on Tab 1.

  Test button       - Sends a test notification so you can verify they work on
                      your system.

HOW NOTIFICATIONS WORK:

  The app tries two methods, in order:
    1. The "winotify" Python library (best experience, install with:
       pip install winotify)
    2. PowerShell-based Windows toast notifications (built into Windows 10/11,
       no extra install needed)

  If neither method works, the notification log on this tab will say
  "(toast failed)" and the app continues working normally -- it's just the
  desktop popup that won't appear.

  All notification events are logged in the NOTIFICATION LOG area on this tab
  with timestamps.

  Notification settings are saved to the config file and persist across
  app restarts.


================================================================================
  9. MEMBER EXCLUSIONS
================================================================================

You can exclude specific members from like checks. This is useful for:
  - Bots that can't like messages
  - Inactive members who never use GroupMe
  - Yourself (if you don't want to show up in the "didn't like" list)

HOW TO USE:

  1. Select a group from the dropdown.
  2. Click the "Exclusions" button (next to the group dropdown).
  3. A dialog opens with a checkbox for every group member, sorted
     alphabetically.
  4. Check the members you want to EXCLUDE.
  5. Use the "All" button to check everyone, "None" to uncheck everyone.
  6. Click "Apply".

WHAT EXCLUSIONS AFFECT:

  - Like Checker (Tab 1): Excluded members won't appear in the "DID NOT LIKE"
    or "LIKED" lists. They appear separately under "EXCLUDED". The like rate
    percentage is calculated against active (non-excluded) members only.

  - Leaderboard (Tab 2): Excluded members are filtered out of the "LEAST LIKES
    GIVEN" (stingiest) ranking.

  - Other tabs are not affected by exclusions.

Exclusions are saved per-app (not per-group) and persist across restarts.


================================================================================
  10. DATA STORAGE & PRIVACY
================================================================================

All data is stored locally on your computer. Nothing is sent to any server
other than GroupMe's official API.

FILE LOCATIONS (in your home directory):

  ~/.binger/config.json    - Saved settings: API token (if "Remember" is
                             checked), excluded member IDs, notification
                             preferences.

  ~/.binger/history.db     - SQLite database of past like check results.
                             Contains: timestamps, group/message IDs, like
                             counts, and member names from each check.

On Windows, "~" means C:\Users\YourUsername, so the full paths are:
  C:\Users\YourUsername\.binger\config.json
  C:\Users\YourUsername\.binger\history.db

TO CLEAR ALL DATA:
  Delete the .binger folder from your home directory. The app will recreate
  it with defaults next time you run it.

TO REMOVE SAVED TOKEN ONLY:
  Uncheck "Remember" in the app and click Connect, or delete config.json.

SECURITY NOTE:
  Your API token is stored in plain text in config.json. If you're on a shared
  computer, do NOT check "Remember" -- or delete config.json when you're done.


================================================================================
  11. BUILDING FROM SOURCE
================================================================================

If you want to rebuild the .exe yourself:

PREREQUISITES:
  - Python 3.10 or higher
  - pip (comes with Python)

STEPS:

  1. Open a terminal/command prompt in the project folder.

  2. Install dependencies:
       pip install -r requirements.txt

  3. Build the .exe:
       pyinstaller --onefile --windowed --name "BingerLikeChecker" like_checker.py

     Or simply double-click build.bat.

  4. The .exe will be at: dist\BingerLikeChecker.exe

BUILD NOTES:
  - The --onefile flag bundles everything into a single .exe (~16 MB).
  - The --windowed flag prevents a console window from appearing.
  - Build artifacts go into build/ and dist/ folders. You can delete build/
    after building to save space.
  - The .spec file is auto-generated by PyInstaller. You can delete and
    regenerate it.

PROJECT FILES:

  like_checker.py       - Main application source code (Python + tkinter)
  requirements.txt      - Python package dependencies
  build.bat             - One-click Windows build script
  BingerLikeChecker.spec - PyInstaller build configuration (auto-generated)
  README.txt            - This file
  dist/                 - Contains the built .exe
  build/                - Temporary build files (safe to delete)


================================================================================
  12. TROUBLESHOOTING
================================================================================

PROBLEM: "Invalid API token" error when connecting.
  - Make sure you copied the full token from dev.groupme.com.
  - Tokens are long (30+ characters). Check for leading/trailing spaces.
  - Try generating a new token by logging out and back in at dev.groupme.com.

PROBLEM: No groups appear after connecting.
  - You must be a member of at least one GroupMe group.
  - The app fetches up to 50 groups per page. If you have many groups, they
    should all load automatically.

PROBLEM: Messages won't load.
  - Check your internet connection.
  - The group may have no messages.
  - Try a smaller message count (e.g. 20).

PROBLEM: The .exe won't start / Windows blocks it.
  - Windows SmartScreen may block unsigned .exe files. Click "More info" then
    "Run anyway".
  - Some antivirus software flags PyInstaller executables. Add an exception
    for BingerLikeChecker.exe.

PROBLEM: Notifications don't appear.
  - Go to the Notifications tab and click "Send Test Notification".
  - For best results, install winotify: pip install winotify
  - Make sure Windows notifications are enabled in Settings > System >
    Notifications.
  - Focus Assist / Do Not Disturb mode will suppress notifications.

PROBLEM: "Send Shame List" fails.
  - You need to be a member of the group (not just viewing it).
  - GroupMe has a 1,000 character message limit. If the shame list is very
    long (many non-likers), the message may be truncated.

PROBLEM: Like counts seem wrong.
  - The app fetches the latest member list each time you check likes.
  - If someone left the group after liking a message, they'll still appear
    in the "favorited_by" data but not in the member list.
  - GroupMe's API may have slight delays in updating like data.

PROBLEM: The app looks blurry on a high-DPI display.
  - This is a known limitation of tkinter on Windows. The app uses the system
    DPI settings. Try adjusting Windows display scaling.


================================================================================
  13. FAQ
================================================================================

Q: Is this free?
A: Yes. The app is free and uses GroupMe's free public API.

Q: Does this work on Mac or Linux?
A: The Python script (like_checker.py) works on any OS with Python 3.10+ and
   tkinter. The .exe is Windows-only. Toast notifications are Windows-only;
   on other platforms that feature simply won't fire.

Q: Can people tell I'm using this?
A: No, unless you click "Send Shame List" which sends a visible message to the
   group. Everything else is read-only API calls that are invisible to other
   members.

Q: Does this use my GroupMe login/password?
A: No. It uses an API token, which is a separate credential. Your password is
   never entered into or stored by this app.

Q: How far back can I scan messages?
A: As far as GroupMe's API allows, which is the entire group history. The app
   lets you scan up to 2,000 messages at a time in the leaderboard and
   analytics tabs. For the like checker, up to 500 at a time.

Q: Can I check direct messages (DMs)?
A: No, only group messages. The GroupMe API handles DM likes differently and
   they don't have a member list to compare against.

Q: What happens if I exclude everyone?
A: The like checker will show 0 active members and 100% like rate with an
   empty "DID NOT LIKE" list. All members will appear under "EXCLUDED".

Q: Can I use multiple GroupMe accounts?
A: Not simultaneously. To switch accounts, paste a different API token and
   click Connect. Your history database will contain data from all accounts.


================================================================================

Binger Like Checker
Built with Python, tkinter, and the GroupMe Public API.
