# What this is, and why you can check that it's safe

You are being asked to run a program on your computer by someone you met online. You should
be suspicious of that. Here is everything you need to decide, including the parts that are
not in our favour.

## What it does

It reads public Clash Royale battle pages from RoyaleAPI and sends the battle data to a
shared collection point. That data trains an AI to play Clash Royale.

It does this from your computer because RoyaleAPI limits how fast any single internet
connection can read pages. Many people reading a little each is the only way to collect a
useful amount. **Your internet connection and your IP address are the thing being
contributed.** That is the honest description of what you are giving.

## What it never does

* **It never asks for a password.** Not for RoyaleAPI, not for Google, not for anything. You
  log into RoyaleAPI yourself, in a browser window the program opens. The program keeps that
  session in its own folder.
* **It never reads your other browsers.** Not your Chrome cookies, not your saved passwords,
  not your history, not your files. It only uses the browser profile it created for itself,
  in its own directory.
* **It never needs administrator or root.** If something asks you for an admin password to
  install this, that is not this.
* **It never uploads anything about you.** What goes out is Clash Royale battle records -
  player tags, decks, card placements. Public game data. No filenames, no personal
  information, nothing about your machine.
* **It does not update itself.** The code you read is the code that runs. If it changes, you
  will be asked to download it again.

## How to verify all of that yourself

1. **Read it.** It is plain Python, not a compiled `.exe`. Nothing is obfuscated or packed.
   The whole client is a few hundred lines. You do not need to be a programmer to search it
   for the word `password` and find nothing.
2. **Watch what it talks to.** It contacts exactly two hosts: `royaleapi.com`, and the
   collection point whose address is written in your config file in plain text. Any network
   monitor will confirm that. If you ever see it contacting a third place, stop it and tell us.
3. **Check the folder it uses.** It writes only inside its own directory. Nothing anywhere else.
4. **Run it in a virtual machine first** if you want to be careful. It works fine in one.

If a program cannot survive you doing all four of those, do not run it. This one can.

## What we are asking you to accept, stated plainly

* You need a free RoyaleAPI account, and you will log into it.
* Your IP address will be making automated requests to RoyaleAPI. RoyaleAPI's terms do not
  permit automated collection. The realistic worst case is that RoyaleAPI rate-limits or
  blocks your account. We think that is unlikely at the volume each person contributes, but
  it is not zero, and you should know it before you start rather than after.
* You will not be paid.

## What you get to control

* Stop it whenever you like. Close the window. There is nothing running in the background
  afterwards, nothing installed as a service, nothing scheduled.
* Delete it by deleting its folder.

## What we control, and what we don't

The operator decides which decks are worth collecting, and your client asks for that list
when it starts. That is the only instruction it takes. The collection point cannot make your
computer run anything - it can only hand out a list of public pages to read.
