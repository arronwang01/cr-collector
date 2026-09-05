# Clash Royale match collector

Collects public Clash Royale battle data from RoyaleAPI to train a bot. It runs on your
machine because RoyaleAPI limits how fast any single internet connection can read pages, so
a few people each doing a little is the only way to gather a useful amount.

**Read [FOR-VOLUNTEERS.md](FOR-VOLUNTEERS.md) before running this.** You are being asked to
run software by someone you met online, and you should check it first. It is a few hundred
lines of plain Python - not a compiled binary - and you can read all of it in this browser
tab right now.

## What it does not do

- Never asks for a password. You log into RoyaleAPI yourself, in a window it opens.
- Never reads your other browsers, cookies, files or accounts. It uses its own browser
  profile in `~/.cr-scraper`, which it creates.
- Never needs administrator or root.
- Never installs anything outside its own folder. Delete the folder and it is gone.
- Never updates itself. The code you read here is the code that runs.

It contacts exactly two hosts: `royaleapi.com`, and the collection server whose address is
in `coordinator/settings.txt` in plain text.

## Setup

1. Install Python 3 - https://www.python.org/downloads/
   On Windows, tick **Add Python to PATH** in the installer.
2. Make a free RoyaleAPI account - https://royaleapi.com
3. Download this repo (green **Code** button, then **Download ZIP**) and unzip it.
4. Ask the admin for the access token and put it in `coordinator/settings.txt`.
5. Double-click **`start-scraping.command`** on Mac, or **`start-scraping.bat`** on Windows.

First run takes about five minutes: it builds a virtual environment and downloads a browser.
Then a window opens and you log into RoyaleAPI. That window may sit on a loading page for a
minute or two while Cloudflare clears - that is normal, not a freeze.

Close it whenever you like. Progress is saved after every player, so it resumes where it
stopped.

## Honest caveats

- What you are contributing is your internet connection, not spare CPU.
- RoyaleAPI's terms do not permit automated collection. The realistic worst case is that
  RoyaleAPI rate-limits or blocks your account there. Unlikely at this volume, but you
  should know it going in.
- Expect setup to need a hand. Message the admin.

## What is in here

    coordinator/client.py    what runs on your machine
    coordinator/server.py    what runs on the admin's - included so you can see both halves
    royale/                  the RoyaleAPI reading code
    FOR-VOLUNTEERS.md        the full safety explanation
